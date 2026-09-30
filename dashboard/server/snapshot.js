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
  let comm = { round: 0, bytes_this_round: 0, full_precision_bytes_this_round: 0, messages_this_round: 0, total_bytes: 0, per_round: [] };
  if (run) {
    const m = await pool.query(
      `SELECT node_id, round, loss, accuracy, train_loss FROM metrics WHERE run_id = $1 ORDER BY node_id, round`,
      [run.run_id],
    );
    for (const row of m.rows) (metrics[row.node_id] ||= []).push({ round: row.round, loss: row.loss, accuracy: row.accuracy, train_loss: row.train_loss });

    const c = await pool.query(
      `SELECT round,
              COALESCE(SUM(payload_bytes) FILTER (WHERE status = 'delivered'), 0)::int AS bytes,
              COALESCE(SUM(full_precision_bytes) FILTER (WHERE status = 'delivered'), 0)::int AS full_precision_bytes,
              COUNT(*) FILTER (WHERE status = 'delivered')::int AS messages,
              COUNT(*) FILTER (WHERE status <> 'delivered')::int AS failed
         FROM updates_meta WHERE run_id = $1 GROUP BY round ORDER BY round`,
      [run.run_id],
    );
    const perRound = c.rows;
    const last = perRound[perRound.length - 1];
    comm = {
      round: last?.round || 0,
      bytes_this_round: last?.bytes || 0,
      full_precision_bytes_this_round: last?.full_precision_bytes || 0,
      messages_this_round: last?.messages || 0,
      failed_this_round: last?.failed || 0,
      total_bytes: perRound.reduce((s, r) => s + r.bytes, 0),
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
    comm,
    audit: auditRes.rows,
  };
}
