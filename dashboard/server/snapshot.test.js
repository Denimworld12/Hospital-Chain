// node --test: how one run's membership changes show up on the topology, training monitor
// (consensus panel + network training loss) and communication pages. The scenario is the live demo:
// a chain of 4 trains, a 5th hospital joins at the end at round 6, then a middle hospital (b) leaves
// at round 11 and its neighbours bridge.

import { test } from "node:test";
import assert from "node:assert/strict";
import { commFrom, consensusFrom, currentTopology, hospitalsByRound, missingFromRun, networkLossFrom, proofsFrom } from "./snapshot.js";
import { W, chainLinks, chainMembers, layout } from "../src/topologyLayout.js";

const EPS = 1e-3;
const EPOCHS = [
  { epoch: 1, from: 1, chain: ["hospital-a", "hospital-b", "hospital-c", "hospital-d"] },
  { epoch: 2, from: 6, chain: ["hospital-a", "hospital-b", "hospital-c", "hospital-d", "hospital-e"] },
  { epoch: 3, from: 11, chain: ["hospital-a", "hospital-c", "hospital-d", "hospital-e"] },
];
const N_TRAIN = { "hospital-a": 227, "hospital-b": 220, "hospital-c": 150, "hospital-d": 46, "hospital-e": 46 };
const LOSS = { "hospital-a": 0.4, "hospital-b": 0.5, "hospital-c": 0.6, "hospital-d": 0.3, "hospital-e": 0.2 };

const epochAt = (round) => [...EPOCHS].reverse().find((e) => round >= e.from);
const links = (chain) => chain.slice(1).map((b, i) => `${chain[i]}|${b}`);

// residual of an edge: high when the link is new, below epsilon from the 3rd round of each epoch on
function residual(round) {
  const e = epochAt(round);
  return round - e.from >= 2 ? EPS / 10 : 0.5;
}

// One metrics row per chain member per round, as node/qgadmm/scheduler.py writes them.
function metricsRows(upto, { drop = [] } = {}) {
  const rows = [];
  for (let round = 1; round <= upto; round++) {
    const e = epochAt(round);
    for (const id of e.chain) {
      if (drop.some(([r, n]) => r === round && n === id)) continue;
      const mine = Object.fromEntries(links(e.chain).filter((l) => l.split("|").includes(id)).map((l) => [l, residual(round)]));
      rows.push({ node_id: id, round, loss: LOSS[id], accuracy: 0.8, train_loss: LOSS[id], n_train: N_TRAIN[id],
                  epoch: e.epoch, chain_size: e.chain.length, residuals: { edges: mine, epsilon: EPS, epoch: e.epoch } });
    }
  }
  return rows;
}

function nodeRows(stage) {
  const e = EPOCHS[stage];
  const all = ["hospital-a", "hospital-b", "hospital-c", "hospital-d", ...(stage >= 1 ? ["hospital-e"] : [])];
  return all.map((id) => ({
    node_id: id, display_name: id, patients: N_TRAIN[id], neighbours: [], status: "alive",
    epoch: e.epoch, chain: e.chain, in_chain: e.chain.includes(id), membership_round: e.from > 1 ? e.from : null,
  }));
}

test("topology: the current chain in chain order for N = 4, 5 after a join, 4 after a middle leave", () => {
  const t1 = currentTopology(nodeRows(0));
  assert.deepEqual(t1.chain, EPOCHS[0].chain);
  assert.deepEqual(t1.links, ["hospital-a|hospital-b", "hospital-b|hospital-c", "hospital-c|hospital-d"]);
  assert.deepEqual(t1.left, []);

  const t2 = currentTopology(nodeRows(1));
  assert.equal(t2.epoch, 2);
  assert.deepEqual(t2.chain, EPOCHS[1].chain);
  assert.equal(t2.links.length, 4);
  assert.equal(t2.links.at(-1), "hospital-d|hospital-e");

  const t3 = currentTopology(nodeRows(2));
  assert.deepEqual(t3.chain, ["hospital-a", "hospital-c", "hospital-d", "hospital-e"]);
  assert.deepEqual(t3.links, ["hospital-a|hospital-c", "hospital-c|hospital-d", "hospital-d|hospital-e"]);
  assert.deepEqual(t3.left, ["hospital-b"]);
  assert.equal(t3.membership_round, 11);
});

test("topology: the newest epoch wins over a member still reporting an older view", () => {
  const rows = nodeRows(2);
  rows[0] = { ...rows[0], epoch: 2, chain: EPOCHS[1].chain };  // hospital-a has not written epoch 3 yet
  assert.deepEqual(currentTopology(rows).chain, EPOCHS[2].chain);
});

test("topology: rows from before membership epochs fall back to registered nodes and their neighbours", () => {
  const legacy = [
    { node_id: "hospital-c", neighbours: ["hospital-b"] },
    { node_id: "hospital-a", neighbours: ["hospital-b"] },
    { node_id: "hospital-b", neighbours: ["hospital-a", "hospital-c"] },
  ];
  const t = currentTopology(legacy);
  assert.equal(t.epoch, null);
  assert.deepEqual(t.chain, ["hospital-a", "hospital-b", "hospital-c"]);
  assert.deepEqual(t.links.sort(), ["hospital-a|hospital-b", "hospital-b|hospital-c"]);
});

test("consensus: pair lines follow the links and CONSENSUS REACHED resets after each membership change", () => {
  const detected = [{ r: 5, epoch: 1 }];

  const before = consensusFrom(metricsRows(5), detected, currentTopology(nodeRows(0)));
  assert.deepEqual(before.edges, links(EPOCHS[0].chain));
  assert.equal(before.reached, true);
  assert.equal(before.converged_round, 5);

  // the join takes effect at round 6: the new link d|e appears, the check restarts on the new chain
  const joined = consensusFrom(metricsRows(6), detected, currentTopology(nodeRows(1)));
  assert.ok(joined.edges.includes("hospital-d|hospital-e"));
  assert.equal(joined.reached, false);
  assert.equal(joined.converged_round, null, "epoch 1's consensus does not count for the new chain");
  assert.equal(joined.epoch, 2);
  assert.equal(joined.epoch_start_round, 6);

  const settled = consensusFrom(metricsRows(10), [...detected, { r: 10, epoch: 2 }], currentTopology(nodeRows(1)));
  assert.equal(settled.reached, true);
  assert.equal(settled.converged_round, 10);

  // b leaves at round 11: a|b and b|c disappear, the bridge a|c appears, and the pill resets again
  const left = consensusFrom(metricsRows(11), [...detected, { r: 10, epoch: 2 }], currentTopology(nodeRows(2)));
  assert.deepEqual(left.edges, ["hospital-a|hospital-c", "hospital-c|hospital-d", "hospital-d|hospital-e"]);
  assert.ok(left.series.every((p) => !("hospital-a|hospital-b" in p) && !("hospital-b|hospital-c" in p)));
  assert.equal(left.series.find((p) => p.round === 11)["hospital-a|hospital-c"], 0.5);
  assert.equal(left.reached, false);
  assert.equal(left.converged_round, null);
  assert.equal(left.epoch_start_round, 11);

  assert.equal(consensusFrom(metricsRows(15), [], currentTopology(nodeRows(2))).reached, true);
});

test("consensus: right after a change, before the new chain logs a round, it is not reached", () => {
  // the topology already reports epoch 2 but every logged round is still epoch 1
  const c = consensusFrom(metricsRows(5), [{ r: 5, epoch: 1 }], currentTopology(nodeRows(1)));
  assert.equal(c.reached, false);
  assert.equal(c.epoch_start_round, null);
  assert.ok(c.edges.includes("hospital-d|hospital-e"));
});

test("network training loss: the patient-weighted average recomputes over each round's members", () => {
  const rows = metricsRows(15);
  const series = networkLossFrom(rows, 99);  // chain_size is logged, so the registered count is not used
  assert.deepEqual(series.map((p) => p.round), Array.from({ length: 15 }, (_, i) => i + 1));
  const weighted = (ids) => ids.reduce((s, id) => s + LOSS[id] * N_TRAIN[id], 0) / ids.reduce((s, id) => s + N_TRAIN[id], 0);
  for (const [round, e] of [[1, EPOCHS[0]], [6, EPOCHS[1]], [11, EPOCHS[2]]]) {
    const p = series.find((x) => x.round === round);
    assert.equal(p.hospitals, e.chain.length);
    assert.ok(Math.abs(p.loss - weighted(e.chain)) < 1e-12, `round ${round}`);
  }
  // the join and the leave both change the average
  const at = (r) => series.find((x) => x.round === r).loss;
  assert.notEqual(at(5), at(6));
  assert.notEqual(at(10), at(11));
});

test("network training loss: a round counts only once every member of its chain has logged it", () => {
  const rows = metricsRows(12, { drop: [[7, "hospital-e"], [12, "hospital-c"]] });
  const rounds = networkLossFrom(rows, 4).map((p) => p.round);
  assert.ok(!rounds.includes(7) && !rounds.includes(12));
  assert.ok(rounds.includes(6) && rounds.includes(11));
  // rows from before chain_size existed fall back to the registered member count
  const legacy = metricsRows(3).map((row) => {
    const r = { ...row };
    delete r.chain_size;
    delete r.epoch;
    return r;
  });
  assert.equal(networkLossFrom(legacy, 4).length, 3);
  assert.equal(networkLossFrom(legacy, 5).length, 0);
});

test("communication: totals track the current hospital and link count", () => {
  const PACKET = 12;
  const perRound = Array.from({ length: 15 }, (_, i) => {
    const round = i + 1;
    const n = epochAt(round).chain.length;
    const messages = 2 * (n - 1);
    return { round, bytes: messages * PACKET, full_precision_bytes: messages * 128, ref32_bytes: messages * 64,
             coordinate_bytes: messages * 4, messages, failed: 0, d: 16, bits: 2 };
  });
  const hospitals = hospitalsByRound(metricsRows(15));

  const mid = commFrom(perRound.slice(0, 8), currentTopology(nodeRows(1)), hospitals);
  assert.equal(mid.hospitals, 5);
  assert.equal(mid.links, 4);
  assert.equal(mid.packets_per_round, 8);
  assert.equal(mid.messages_this_round, 8);
  assert.equal(mid.bytes_this_round, 8 * PACKET);
  assert.equal(mid.per_round[0].hospitals, 4);
  assert.equal(mid.per_round[7].hospitals, 5);

  const end = commFrom(perRound, currentTopology(nodeRows(2)), hospitals);
  assert.equal(end.hospitals, 4);
  assert.equal(end.links, 3);
  assert.equal(end.bytes_this_round, 6 * PACKET);
  assert.equal(end.hospitals_this_round, 4);
  assert.equal(end.total_bytes, PACKET * (5 * 6 + 5 * 8 + 5 * 6));
  assert.equal(end.messages_total, 5 * 6 + 5 * 8 + 5 * 6);
});

test("topology layout: any N in chain order, heads at odd positions, links between neighbours only", () => {
  for (const n of [1, 2, 3, 4, 5, 8]) {
    const chain = Array.from({ length: n }, (_, i) => `hospital-${"abcdefgh"[i]}`);
    const placed = layout(chainMembers({ chain }, chain.map((id) => ({ node_id: id, display_name: id, status: "alive" }))));
    assert.deepEqual(placed.map((p) => p.node_id), chain);
    assert.deepEqual(placed.map((p) => p.role), chain.map((_, i) => (i % 2 === 0 ? "head" : "tail")));
    assert.ok(placed.every((p) => p.x - p.size / 2 >= 0 && p.x + p.size / 2 <= W), `N=${n} fits the width`);
    for (let i = 1; i < n; i++) {
      assert.ok(placed[i].x > placed[i - 1].x);
      // neighbouring buildings do not overlap by more than a sliver
      assert.ok(placed[i].x - placed[i - 1].x >= 0.85 * Math.max(placed[i].size, placed[i - 1].size) || n <= 3, `N=${n}`);
    }
    assert.deepEqual(chainLinks(placed).map(([a, b]) => `${a.node_id}|${b.node_id}`), links(chain));
  }
});

test("topology layout: a member without a node row yet still gets a placeholder", () => {
  const placed = chainMembers({ chain: ["hospital-a", "hospital-e"] }, [{ node_id: "hospital-a", status: "alive" }]);
  assert.equal(placed[1].node_id, "hospital-e");
  assert.equal(placed[1].status, "down");
});

test("proof of origin: verified only while the newest signed message is fresh and was accepted", () => {
  const msg = { host: { hostname: "alex-laptop" }, data_rows: 62 };
  const rows = [
    { node_id: "st-marys-3f9a", display_name: "St Mary's", enrolled: true, seconds_since_proof: 4, message: msg,
      key_fingerprint: "ab12" },
    { node_id: "hospital-b", seconds_since_proof: 120, message: msg },
    { node_id: "hospital-c", seconds_since_proof: 20, seconds_since_rejection: 3, rejection: "signature: does not verify" },
    { node_id: "hospital-d", seconds_since_rejection: 9, rejection: "no signing key is on record" },
    { node_id: "hospital-e", seconds_since_proof: 2, seconds_since_rejection: 50, rejection: "old" },
  ];
  const out = Object.fromEntries(proofsFrom(rows, 35).map((p) => [p.node_id, p]));
  assert.equal(out["st-marys-3f9a"].status, "verified");
  assert.equal(out["st-marys-3f9a"].display_name, "St Mary's");
  assert.equal(out["hospital-b"].status, "stale");
  assert.equal(out["hospital-b"].display_name, "hospital-b");
  assert.equal(out["hospital-c"].status, "rejected");
  assert.equal(out["hospital-c"].rejection, "signature: does not verify");
  assert.equal(out["hospital-d"].status, "rejected");
  assert.equal(out["hospital-e"].status, "verified", "a later accepted message clears an older refusal");
  assert.equal(out["hospital-e"].rejection, null);
});

test("a run that never reached a chain member is flagged, not reported as plainly complete", () => {
  const run = { run_id: "run-1", started_at: "2026-10-11T10:00:00Z" };
  const topology = { chain: ["hospital-a", "dead-laptop", "st-marys-3f9a"] };
  const nodes = [
    { node_id: "hospital-a", display_name: "Hospital A", run_id: "run-1", registered_at: "2026-10-11T09:00:00Z" },
    { node_id: "dead-laptop", display_name: "Dead Laptop", run_id: null, registered_at: "2026-10-11T09:00:00Z" },
    { node_id: "st-marys-3f9a", display_name: "St Mary's", run_id: null, registered_at: "2026-10-11T09:30:00Z" },
    { node_id: "late-joiner", display_name: "Late", run_id: null, registered_at: "2026-10-11T11:00:00Z" },
  ];
  const metrics = { "hospital-a": [{ round: 1 }] };
  assert.deepEqual(missingFromRun(run, "completed", nodes, topology, metrics).map((n) => n.node_id), ["dead-laptop", "st-marys-3f9a"]);
  // one that took part (it logged rounds, even if it has since moved on) is not missing
  assert.deepEqual(missingFromRun(run, "completed", nodes, topology, { ...metrics, "dead-laptop": [{ round: 1 }], "st-marys-3f9a": [{ round: 2 }] }), []);
  // a hospital that registered after the run started (or is not in the chain) is not counted
  assert.deepEqual(missingFromRun(run, "completed", nodes, { chain: ["hospital-a", "late-joiner"] }, metrics), []);
  assert.deepEqual(missingFromRun(null, "idle", nodes, topology, metrics), []);
  // while the run is starting or going, the others may simply not have heard of it yet
  for (const state of ["stalled", "running", "paused"]) assert.deepEqual(missingFromRun(run, state, nodes, topology, metrics), []);
  assert.equal(missingFromRun(run, "failed", nodes, topology, metrics).length, 2);
});
