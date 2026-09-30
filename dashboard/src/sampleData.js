// Sample snapshot used by `?sample=1` (design review without a running stack).
// Same shape the dashboard server pushes over /ws; see server/snapshot.js.
const NODES = [
  { node_id: "hospital-a", display_name: "Hospital A", patients: 303, neighbours: ["hospital-b"] },
  { node_id: "hospital-b", display_name: "Hospital B", patients: 294, neighbours: ["hospital-a", "hospital-c"] },
  { node_id: "hospital-c", display_name: "Hospital C", patients: 200, neighbours: ["hospital-b"] },
];
const ENGINE = "naive gossip baseline";
const PARAMS = 16;

export function sampleSnapshot(tick = 0) {
  const round = Math.min(20, 1 + (tick % 24));
  const metrics = {};
  NODES.forEach((n, i) => {
    metrics[n.node_id] = Array.from({ length: round }, (_, k) => {
      const r = k + 1;
      return {
        round: r,
        loss: +(0.69 * Math.exp(-r / (7 + i)) + 0.42 + 0.02 * Math.sin(r + i)).toFixed(4),
        accuracy: +(0.55 + 0.25 * (1 - Math.exp(-r / (5 + i))) + 0.015 * Math.cos(r * 1.3 + i)).toFixed(4),
      };
    });
  });
  const perMessage = PARAMS * 8;
  const perRound = Array.from({ length: round }, (_, k) => ({
    round: k + 1,
    bytes: perMessage * 4,
    full_precision_bytes: perMessage * 4,
    messages: 4,
  }));
  const now = Date.now();
  const audit = [];
  let id = 1000;
  for (let r = round; r > Math.max(0, round - 4); r--) {
    for (const [s, t] of [["hospital-a", "hospital-b"], ["hospital-b", "hospital-a"], ["hospital-b", "hospital-c"], ["hospital-c", "hospital-b"]]) {
      audit.push({
        id: id--,
        ts: new Date(now - (round - r) * 1500).toISOString(),
        event: "update_received",
        node_id: t,
        sender: s,
        receiver: t,
        round: r,
        payload_bytes: perMessage,
        checksum: (r * 7919 + s.length * 31 + t.charCodeAt(9)).toString(16).padStart(8, "0") + "c0ffee5a9e11d0e2",
        detail: { verified: true, engine: ENGINE },
      });
    }
  }
  return {
    generated_at: new Date(now).toISOString(),
    engine: ENGINE,
    heartbeat_stale_after_s: 7,
    nodes: NODES.map((n) => ({
      ...n,
      status: "alive",
      engine: ENGINE,
      run_state: round < 20 ? "running" : "completed",
      current_round: round,
      seconds_since_heartbeat: 1.2,
    })),
    run: { run_id: "run-sample", total_rounds: 20, state: round < 20 ? "running" : "completed", engine: ENGINE, started_at: new Date(now - round * 1500).toISOString() },
    metrics,
    comm: {
      round,
      bytes_this_round: perMessage * 4,
      full_precision_bytes_this_round: perMessage * 4,
      messages_this_round: 4,
      total_bytes: perMessage * 4 * round,
      per_round: perRound,
    },
    audit,
  };
}
