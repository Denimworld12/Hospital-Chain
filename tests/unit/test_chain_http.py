"""The networked Q-GADMM round loop over real HTTP: three production node apps in one process.

No docker: each node is node/main.py's FastAPI app on its own localhost port with its own
config, data slice and checkpoint directory (see chain_harness.py).
"""

import time

import httpx
import numpy as np
import pytest

from chain_harness import AUTH, NodeServer, wait_until, write_configs
from node import wire
from node.qgadmm.engine import parse_snapshot
from node.qgadmm.scheduler import simulate

CHAIN = ["hospital-a", "hospital-b", "hospital-c"]


@pytest.fixture
def chain(tmp_path):
    cfgs = write_configs(tmp_path, CHAIN)
    nodes = {n: NodeServer(p).start() for n, p in cfgs.items()}
    yield nodes, cfgs
    for s in nodes.values():
        s.stop()


def start(nodes, rounds=300) -> str:
    r = httpx.post(f"{nodes['hospital-a'].url}/round/start", json={"rounds": rounds}, headers=AUTH, timeout=10).json()
    assert r["status"] == "started" and r["engine"] == "Q-GADMM (b=2)"
    return r["run_id"]


def all_completed(nodes):
    return all(s.status()["run_state"] == "completed" for s in nodes.values())


def assert_consensus_streak(nodes, eps=1e-3):
    """Every edge was below epsilon on the 3 consecutive rounds that triggered the stop."""
    detected = {s.status()["converged_round"] for s in nodes.values()} - {None}
    assert detected, "no node detected consensus"
    t = min(detected)
    for s in nodes.values():
        by_round = {h["round"]: h["max_residual"] for h in s.metrics()["history"]}
        streak = [by_round[r] for r in (t - 2, t - 1, t) if r in by_round]
        assert streak and max(streak) < eps, (s.cfg.node_id, t, streak)


def assert_edges_in_sync(nodes):
    a, b, c = (nodes[n].engine.state for n in CHAIN)
    for left, right, ln, rn in ((a, b, "hospital-a", "hospital-b"), (b, c, "hospital-b", "hospital-c")):
        assert np.array_equal(left.theta_hat, right.mirrors[ln]) and np.array_equal(right.theta_hat, left.mirrors[rn])
        assert np.array_equal(left.lambdas[rn], right.lambdas[ln])


def test_networked_run_reaches_consensus_and_matches_the_in_process_choreography(chain):
    nodes, _ = chain
    start(nodes)
    wait_until(lambda: all_completed(nodes), 120, "run completes", 0.2)
    stops = {n: s.status() for n, s in nodes.items()}
    last = {st["current_round"] for st in stops.values()}
    assert len(last) == 1, f"nodes stopped on different rounds: {stops}"
    stop_round = last.pop()
    assert stop_round < 300, "consensus never reached; hit the round limit"
    assert all(st["converged_round"] is not None or st["stop_at"] == stop_round for st in stops.values())
    for s in nodes.values():
        hist = s.metrics()["history"]
        assert [h["round"] for h in hist] == list(range(1, stop_round + 1))
        assert not any(h["degraded"] for h in hist)
        assert hist[-1]["max_residual"] < 2e-3 and hist[0]["max_residual"] > 0.1
        assert not s.db.events("snapshot_resync") and not s.db.events("round_degraded")
        # every packet on the wire: ceil(2*16/8) + 8 = 12 bytes, logged per send
        assert {x["payload_bytes"] for x in s.db.sent} == {12} and {x["status"] for x in s.db.sent} == {"delivered"}
        assert all(m["residuals"]["edges"] for m in s.db.metrics)
    assert_consensus_streak(nodes)
    assert_edges_in_sync(nodes)

    # the HTTP choreography is exactly the in-process one: same seeds, same order -> identical bits
    from node.engines import build_engine
    from node.trainer import LocalTrainer

    engines = []
    for n in CHAIN:
        cfg = nodes[n].cfg
        t = LocalTrainer(cfg.data_slice, test_fraction=cfg.training.test_fraction, seed=cfg.training.seed,
                         learning_rate=cfg.training.learning_rate, local_epochs=cfg.training.local_epochs)
        engines.append(build_engine(cfg, t))
    simulate(engines, stop_round, stop_on_convergence=False)
    for n, e in zip(CHAIN, engines):
        assert np.array_equal(e.state.theta, nodes[n].engine.state.theta), n
        assert np.array_equal(e.state.theta_hat, nodes[n].engine.state.theta_hat), n


def test_kill_a_tail_heads_continue_and_it_rejoins_from_its_checkpoint(tmp_path):
    # a small round pause, as in production, so the heads do not race hundreds of rounds ahead while
    # the tail is down (their round counters drift apart then; the barrier re-aligns them after the rejoin)
    cfgs = write_configs(tmp_path, CHAIN, round_pause_s=0.01, rounds=500)
    nodes = {n: NodeServer(p).start() for n, p in cfgs.items()}
    try:
        _kill_and_rejoin(nodes, cfgs)
    finally:
        for s in nodes.values():
            s.stop()


def _kill_and_rejoin(nodes, cfgs):
    run_id = start(nodes, rounds=500)
    wait_until(lambda: nodes["hospital-b"].runner.current_round >= 13, 60, "hospital-b past round 13")
    nodes["hospital-b"].stop(abrupt=True)  # its in-memory state is gone; its checkpoint stays on disk
    heads = ("hospital-a", "hospital-c")
    at_kill = {n: nodes[n].runner.current_round for n in heads}
    wait_until(lambda: all(nodes[n].runner.current_round >= at_kill[n] + 10 for n in heads), 60, "heads keep going")

    nodes["hospital-b"] = NodeServer(cfgs["hospital-b"]).start()
    b = nodes["hospital-b"]
    rejoined = wait_until(lambda: b.db.events("rejoined_from_checkpoint"), 30, "rejoin")[0]
    assert rejoined["run_id"] == run_id
    d = rejoined["detail"]
    assert d["checkpoint_round"] >= 10 and d["checkpoint_round"] % 5 == 0
    assert d["rejoin_round"] > max(at_kill.values()) and d["resynced_from"] == list(heads)
    resyncs = b.db.events("snapshot_resync")
    assert len(resyncs) == 2 and all(r["payload_bytes"] == 3 * 16 * 8 for r in resyncs)  # byte cost is audited
    assert d["resync_bytes"] == sum(r["payload_bytes"] for r in resyncs)

    wait_until(lambda: all_completed(nodes), 120, "run completes after the rejoin", 0.2)
    rounds = {s.status()["current_round"] for s in nodes.values()}
    assert len(rounds) == 1 and rounds.pop() < 500, "consensus after the rejoin"
    for n in heads:
        degraded = [h["round"] for h in nodes[n].metrics()["history"] if h["degraded"]]
        assert degraded, f"{n} should have logged degraded rounds while hospital-b was down"
        assert nodes[n].db.events("round_degraded")
    # residuals resume falling after the rejoin, down to consensus
    hist = b.metrics()["history"]
    assert hist[0]["round"] == d["rejoin_round"]
    assert max(h["max_residual"] for h in hist[:5]) > 0.1
    assert_consensus_streak(nodes)
    assert_edges_in_sync(nodes)


def test_snapshot_endpoint_returns_model_state_only_and_needs_the_secret(chain):
    nodes, _ = chain
    a = nodes["hospital-a"]
    assert httpx.get(f"{a.url}/snapshot", params={"requester": "hospital-b"}).status_code == 401
    assert httpx.get(f"{a.url}/snapshot", params={"requester": "stranger"}, headers=AUTH).status_code == 403
    r = httpx.get(f"{a.url}/snapshot", params={"requester": "hospital-b"}, headers=AUTH)
    assert r.status_code == 200
    d = a.engine.d
    assert len(r.content) == 3 * d * 8  # theta_hat | my mirror of the requester | our edge's lambda, float64
    snap = parse_snapshot(r.content, d, int(r.headers[wire.H_SEQ]), int(r.headers[wire.H_MIRROR_SEQ]))
    np.testing.assert_array_equal(snap["theta_hat"], a.engine.state.theta_hat)
    served = a.db.events("snapshot_served")
    assert served and served[-1]["payload_bytes"] == len(r.content) and served[-1]["receiver"] == "hospital-b"


def test_update_rejects_packets_for_another_engine(chain):
    nodes, _ = chain
    body = wire.encode(np.zeros(16))
    r = httpx.post(f"{nodes['hospital-a'].url}/update", content=body, headers={
        **AUTH, wire.H_SENDER: "hospital-b", wire.H_RUN: "r", wire.H_ROUND: "1",
        wire.H_CHECKSUM: wire.checksum(body), wire.H_DTYPE: "float64"})
    assert r.status_code == 400 and "Q-GADMM" in r.json()["detail"]


def test_pause_does_not_degrade_rounds(chain):
    nodes, _ = chain
    start(nodes)
    wait_until(lambda: nodes["hospital-b"].runner.current_round >= 5, 30, "round 5")
    assert httpx.post(f"{nodes['hospital-b'].url}/round/pause", json={}, headers=AUTH).json()["status"] == "paused"
    wait_until(lambda: all(s.status()["run_state"] == "paused" for s in nodes.values()), 10, "all paused")
    time.sleep(3.0)  # longer than round_timeout_s
    httpx.post(f"{nodes['hospital-a'].url}/round/resume", json={}, headers=AUTH)
    wait_until(lambda: all_completed(nodes), 120, "run completes", 0.2)
    for s in nodes.values():
        assert not s.db.events("round_degraded"), s.cfg.node_id
    assert_edges_in_sync(nodes)
