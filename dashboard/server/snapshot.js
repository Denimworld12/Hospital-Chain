// Builds the dashboard snapshot by READING Postgres. The dashboard never writes
// the training tables and never coordinates training.

export const STALE_AFTER_S = Number(process.env.HEARTBEAT_STALE_S || 7);
const AUDIT_ROWS = Number(process.env.AUDIT_ROWS || 80);

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

export async function buildSnapshot(pool) {
  const nodesRes = await pool.query(
    `SELECT node_id, display_name, url, patients, neighbours, engine, run_id, run_state, current_round,
            EXTRACT(EPOCH FROM (now() - last_heartbeat))::float AS seconds_since_heartbeat
       FROM nodes ORDER BY node_id`,
  );
  const nodes = nodesRes.rows.map((n) => ({
    ...n,
    status: n.seconds_since_heartbeat != null && n.seconds_since_heartbeat <= STALE_AFTER_S ? "alive" : "down",
  }));

  const runRes = await pool.query(`SELECT run_id, total_rounds, engine, initiated_by, started_at FROM runs ORDER BY started_at DESC LIMIT 1`);
  const run = runRes.rows[0] || null;

  const metrics = {};
  let networkLoss = [];
  let consensus = { edges: [], series: [], epsilon: null, latest_max: null, reached: false, converged_round: null };
  let comm = {
    round: 0, bytes_this_round: 0, full_precision_bytes_this_round: 0, ref32_bytes_this_round: 0,
    messages_this_round: 0, total_bytes: 0, ref32_total_bytes: 0, coordinate_bytes_total: 0, messages_total: 0,
    d: null, bits: null, per_round: [],
  };
  if (run) {
    const m = await pool.query(
      `SELECT node_id, round, loss, accuracy, train_loss, n_train, residuals FROM metrics WHERE run_id = $1 ORDER BY node_id, round`,
      [run.run_id],
    );
    // Per-edge primal residuals (ADMM engines only). Both ends of an edge log the same value;
    // keep the larger if they ever differ.
    const byRound = new Map();
    const edgeSet = new Set();
    const lossByRound = new Map(); // round -> {sum, n, nodes}
    for (const row of m.rows) {
      const l = lossByRound.get(row.round) || { sum: 0, n: 0, nodes: 0 };
      l.sum += row.train_loss * row.n_train;
      l.n += row.n_train;
      l.nodes += 1;
      lossByRound.set(row.round, l);
      (metrics[row.node_id] ||= []).push({ round: row.round, loss: row.loss, accuracy: row.accuracy, train_loss: row.train_loss });
      const edges = row.residuals?.edges;
      if (!edges) continue;
      if (row.residuals.epsilon != null) consensus.epsilon = row.residuals.epsilon;
      const point = byRound.get(row.round) || { round: row.round };
      for (const [edge, value] of Object.entries(edges)) {
        edgeSet.add(edge);
        point[edge] = Math.max(point[edge] ?? 0, value);
      }
      byRound.set(row.round, point);
    }
    // Network training loss: every hospital's mean training log-loss at its own model, weighted by its
    // training rows - the objective the nodes minimise together. Only rounds every registered node logged.
    const expectedNodes = nodes.length;
    networkLoss = [...lossByRound.entries()]
      .filter(([, l]) => l.nodes >= expectedNodes)
      .sort((a, b) => a[0] - b[0])
      .map(([round, l]) => ({ round, loss: l.sum / l.n }));
    if (edgeSet.size) {
      const edges = [...edgeSet].sort();
      const series = [...byRound.values()].sort((a, b) => a.round - b.round);
      const complete = series.filter((p) => edges.every((e) => p[e] != null));
      const latest = complete[complete.length - 1];
      const latestMax = latest ? Math.max(...edges.map((e) => latest[e])) : null;
      const detected = await pool.query(
        `SELECT MIN((detail->>'converged_round')::int) AS r FROM audit_logs WHERE run_id = $1 AND event = 'consensus_detected'`,
        [run.run_id],
      );
      consensus = {
        edges,
        series,
        epsilon: consensus.epsilon,
        latest_round: latest?.round ?? null,
        latest_max: latestMax,
        reached: latestMax != null && consensus.epsilon != null && latestMax < consensus.epsilon,
        converged_round: detected.rows[0]?.r ?? null,
      };
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
    const perRound = c.rows;
    const last = perRound[perRound.length - 1];
    const sum = (k) => perRound.reduce((s, r) => s + r[k], 0);
    comm = {
      round: last?.round || 0,
      bytes_this_round: last?.bytes || 0,
      full_precision_bytes_this_round: last?.full_precision_bytes || 0,
      ref32_bytes_this_round: last?.ref32_bytes || 0,
      messages_this_round: last?.messages || 0,
      failed_this_round: last?.failed || 0,
      total_bytes: sum("bytes"),
      ref32_total_bytes: sum("ref32_bytes"),
      coordinate_bytes_total: sum("coordinate_bytes"),
      messages_total: sum("messages"),
      d: last?.d ?? null,
      bits: last?.bits ?? null,
      per_round: perRound,
    };
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
