// Builds the dashboard snapshot by READING Postgres. The dashboard never writes
// the training tables and never coordinates training. The chain's membership is
// whatever the nodes report about themselves (nodes.epoch / chain / in_chain);
// the directory that coordinates joins and leaves keeps its own separate store,
// which the dashboard never reads.
//
// buildSnapshot() only runs the queries; every number shown is computed by the
// pure functions below, which are unit-tested (snapshot.test.js).

export const STALE_AFTER_S = Number(process.env.HEARTBEAT_STALE_S || 7);
const AUDIT_ROWS = Number(process.env.AUDIT_ROWS || 80);

const edgeKey = (a, b) => `${a}|${b}`;

function runState(run, nodes) {
  if (!run) return "idle";
  const inRun = nodes.filter((n) => n.status === "alive" && n.run_id === run.run_id);
  const states = new Set(inRun.map((n) => n.run_state));
  if (states.has("running")) return "running";
  if (states.has("paused")) return "paused";
  if (states.has("failed")) return "failed";
  if (states.has("completed")) return "completed";
  return inRun.length ? [...states][0] : "stalled";
}

// The current chain: the newest membership view (highest epoch) any member reports. Nodes that left,
// or that the current chain no longer lists, are reported in `left`. Rows from before membership
// epochs existed (NULL epoch / chain) fall back to every registered node, ordered by id, linked by
// the neighbours each node reports.
export function currentTopology(nodes) {
  const members = nodes.filter((n) => n.in_chain !== false && Array.isArray(n.chain) && n.chain.length);
  if (members.length) {
    const newest = members.reduce((a, b) => ((b.epoch ?? 0) > (a.epoch ?? 0) ? b : a));
    const chain = [...newest.chain];
    const links = chain.slice(1).map((b, i) => edgeKey(chain[i], b));
    const left = nodes.map((n) => n.node_id).filter((id) => !chain.includes(id)).sort();
    return { epoch: newest.epoch ?? 0, chain, links, left, membership_round: newest.membership_round ?? null };
  }
  const chain = nodes.filter((n) => n.in_chain !== false).map((n) => n.node_id).sort();
  const seen = new Set();
  const links = [];
  for (const n of nodes) {
    for (const nb of n.neighbours || []) {
      if (!chain.includes(n.node_id) || !chain.includes(nb)) continue;
      const [a, b] = chain.indexOf(n.node_id) < chain.indexOf(nb) ? [n.node_id, nb] : [nb, n.node_id];
      if (!seen.has(edgeKey(a, b))) links.push(edgeKey(a, b));
      seen.add(edgeKey(a, b));
    }
  }
  const left = nodes.filter((n) => n.in_chain === false).map((n) => n.node_id).sort();
  return { epoch: null, chain, links, left, membership_round: null };
}

// Per-edge primal residuals (ADMM engines only). Both ends of an edge log the same value; keep the
// larger if they ever differ. Pair lines are the current chain's links: a link that no longer exists
// disappears, a new one appears. "Consensus reached" only looks at rounds of the current epoch, so
// it resets with every membership change until the new chain's edges are all below epsilon again.
export function consensusFrom(metricsRows, detectedRows, topology) {
  const empty = { edges: [], series: [], epsilon: null, latest_round: null, latest_max: null, reached: false,
                  converged_round: null, epoch: topology?.epoch ?? null, epoch_start_round: null };
  const rows = metricsRows.filter((r) => r.residuals?.edges);
  if (!rows.length) return empty;
  const rowEpoch = (r) => r.epoch ?? r.residuals.epoch ?? 0;
  const epsilon = rows.reduce((e, r) => (r.residuals.epsilon != null ? r.residuals.epsilon : e), null);
  const epoch = topology?.epoch ?? Math.max(...rows.map(rowEpoch));
  let edges = topology?.epoch != null ? [...topology.links] : null;
  if (!edges) edges = [...new Set(rows.flatMap((r) => Object.keys(r.residuals.edges)))].sort();
  const byRound = new Map();
  for (const row of rows) {
    const point = byRound.get(row.round) || { round: row.round };
    for (const [edge, value] of Object.entries(row.residuals.edges)) {
      if (edges.includes(edge)) point[edge] = Math.max(point[edge] ?? 0, value);
    }
    byRound.set(row.round, point);
  }
  const series = [...byRound.values()].sort((a, b) => a.round - b.round);
  const epochRounds = new Set(rows.filter((r) => rowEpoch(r) === epoch).map((r) => r.round));
  const epochStart = epochRounds.size ? Math.min(...epochRounds) : null;
  const complete = series.filter((p) => epochRounds.has(p.round) && edges.every((e) => p[e] != null));
  const latest = complete[complete.length - 1];
  const latestMax = latest && edges.length ? Math.max(...edges.map((e) => latest[e])) : null;
  const detected = detectedRows.filter((d) => d.r != null && (d.epoch ?? 0) === epoch).map((d) => d.r);
  return {
    edges,
    series,
    epsilon,
    latest_round: latest?.round ?? null,
    latest_max: latestMax,
    reached: latestMax != null && epsilon != null && latestMax < epsilon,
    converged_round: detected.length ? Math.min(...detected) : null,
    epoch,
    epoch_start_round: epochStart,
  };
}

// Network training loss: every hospital's mean training log-loss at its own model, weighted by its
// training rows - the objective the nodes minimise together. A round counts once every member of that
// round's chain has logged it (chain_size, written by the nodes), so the average recomputes over the
// new membership when a hospital joins or leaves. Rows without chain_size fall back to `fallbackCount`.
export function networkLossFrom(metricsRows, fallbackCount) {
  const byRound = new Map();
  for (const row of metricsRows) {
    const l = byRound.get(row.round) || { sum: 0, n: 0, nodes: 0, size: null };
    l.sum += row.train_loss * row.n_train;
    l.n += row.n_train;
    l.nodes += 1;
    if (row.chain_size != null) l.size = Math.max(l.size ?? 0, row.chain_size);
    byRound.set(row.round, l);
  }
  return [...byRound.entries()]
    .filter(([, l]) => l.nodes >= (l.size ?? fallbackCount))
    .sort((a, b) => a[0] - b[0])
    .map(([round, l]) => ({ round, loss: l.sum / l.n, hospitals: l.nodes }));
}

// Hospitals in the chain per round: chain_size if the nodes logged it, else how many logged that round.
export function hospitalsByRound(metricsRows) {
  const out = new Map();
  for (const row of metricsRows) {
    const cur = out.get(row.round) || { size: null, count: 0 };
    cur.count += 1;
    if (row.chain_size != null) cur.size = Math.max(cur.size ?? 0, row.chain_size);
    out.set(row.round, cur);
  }
  return new Map([...out].map(([r, v]) => [r, v.size ?? v.count]));
}

// Communication totals. Byte counts are the measured payloads in updates_meta; `hospitals` per round
// and the current hospital / link count show that the totals track the chain's size as it changes.
export function commFrom(perRoundRows, topology, hospitals = new Map()) {
  const perRound = perRoundRows.map((r) => ({ ...r, hospitals: hospitals.get(r.round) ?? null }));
  const last = perRound[perRound.length - 1];
  const sum = (k) => perRound.reduce((s, r) => s + r[k], 0);
  const links = topology?.links?.length ?? 0;
  return {
    round: last?.round || 0,
    bytes_this_round: last?.bytes || 0,
    full_precision_bytes_this_round: last?.full_precision_bytes || 0,
    ref32_bytes_this_round: last?.ref32_bytes || 0,
    messages_this_round: last?.messages || 0,
    failed_this_round: last?.failed || 0,
    hospitals_this_round: last?.hospitals ?? null,
    total_bytes: sum("bytes"),
    ref32_total_bytes: sum("ref32_bytes"),
    coordinate_bytes_total: sum("coordinate_bytes"),
    messages_total: sum("messages"),
    d: last?.d ?? null,
    bits: last?.bits ?? null,
    hospitals: topology?.chain?.length ?? 0,
    links,
    packets_per_round: 2 * links,
    per_round: perRound,
  };
}

export async function buildSnapshot(pool) {
  const nodesRes = await pool.query(
    `SELECT node_id, display_name, url, patients, neighbours, engine, run_id, run_state, current_round,
            epoch, chain, in_chain, membership_round,
            EXTRACT(EPOCH FROM (now() - last_heartbeat))::float AS seconds_since_heartbeat
       FROM nodes ORDER BY node_id`,
  );
  const nodes = nodesRes.rows.map((n) => ({
    ...n,
    status: n.seconds_since_heartbeat != null && n.seconds_since_heartbeat <= STALE_AFTER_S ? "alive" : "down",
  }));
  const topology = currentTopology(nodes);

  const runRes = await pool.query(`SELECT run_id, total_rounds, engine, initiated_by, started_at FROM runs ORDER BY started_at DESC LIMIT 1`);
  const run = runRes.rows[0] || null;

  const metrics = {};
  let networkLoss = [];
  let consensus = consensusFrom([], [], topology);
  let comm = commFrom([], topology);
  if (run) {
    const m = await pool.query(
      `SELECT node_id, round, loss, accuracy, train_loss, n_train, residuals, epoch, chain_size
         FROM metrics WHERE run_id = $1 ORDER BY node_id, round`,
      [run.run_id],
    );
    for (const row of m.rows) {
      (metrics[row.node_id] ||= []).push({ round: row.round, loss: row.loss, accuracy: row.accuracy, train_loss: row.train_loss });
    }
    networkLoss = networkLossFrom(m.rows, nodes.filter((n) => n.in_chain !== false).length);
    if (m.rows.some((r) => r.residuals?.edges)) {
      const detected = await pool.query(
        `SELECT (detail->>'converged_round')::int AS r, (detail->>'epoch')::int AS epoch
           FROM audit_logs WHERE run_id = $1 AND event = 'consensus_detected'`,
        [run.run_id],
      );
      consensus = consensusFrom(m.rows, detected.rows, topology);
    }

    // ref32_bytes: the same d parameters as 32-bit floats, 4*d per message (d = full_precision_bytes / 8,
    // which the nodes log as the float64 size). coordinate_bytes: ADMM packets minus their 8-byte (b, R) header.
    const c = await pool.query(
      `SELECT round,
              COALESCE(SUM(payload_bytes) FILTER (WHERE status = 'delivered'), 0)::int AS bytes,
              COALESCE(SUM(full_precision_bytes) FILTER (WHERE status = 'delivered'), 0)::int AS full_precision_bytes,
              COALESCE(SUM(full_precision_bytes / 2) FILTER (WHERE status = 'delivered'), 0)::int AS ref32_bytes,
              COALESCE(SUM(CASE WHEN dtype = 'qgadmm-packet' THEN GREATEST(payload_bytes - 8, 0) ELSE payload_bytes END)
                       FILTER (WHERE status = 'delivered'), 0)::int AS coordinate_bytes,
              COUNT(*) FILTER (WHERE status = 'delivered')::int AS messages,
              COUNT(*) FILTER (WHERE status <> 'delivered')::int AS failed,
              MAX(full_precision_bytes / 8)::int AS d,
              MAX(bits)::int AS bits
         FROM updates_meta WHERE run_id = $1 GROUP BY round ORDER BY round`,
      [run.run_id],
    );
    comm = commFrom(c.rows, topology, hospitalsByRound(m.rows));
  }

  const auditRes = await pool.query(
    `SELECT id, ts, node_id, event, run_id, round, sender, receiver, payload_bytes, checksum, detail
       FROM audit_logs ORDER BY id DESC LIMIT $1`,
    [AUDIT_ROWS],
  );

  const engine = run?.engine || nodes.find((n) => n.engine)?.engine || "naive gossip baseline";
  return {
    generated_at: new Date().toISOString(),
    engine,
    heartbeat_stale_after_s: STALE_AFTER_S,
    nodes,
    topology,
    run: run ? { ...run, state: runState(run, nodes) } : null,
    metrics,
    network_loss: networkLoss,
    consensus,
    comm,
    audit: auditRes.rows,
  };
}

// Rows of updates_meta newer than `afterId`: one per POST /update a node made. The dashboard turns
// them into WebSocket "update" events (the envelope animation); it still only reads Postgres.
export async function updatesSince(pool, afterId) {
  const { rows } = await pool.query(
    `SELECT id::int AS id, run_id, round, sender, receiver, payload_bytes, bits, status
       FROM updates_meta WHERE id > $1 ORDER BY id LIMIT 200`,
    [afterId],
  );
  return rows;
}

export async function latestUpdateId(pool) {
  const { rows } = await pool.query(`SELECT COALESCE(MAX(id), 0)::int AS id FROM updates_meta`);
  return rows[0].id;
}
