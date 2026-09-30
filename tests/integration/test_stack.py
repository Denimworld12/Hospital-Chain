"""End-to-end tests against the running docker compose stack.

    docker compose up -d --build
    MEDCHAIN_STACK=1 pytest tests/integration

They drive real training runs and really `docker kill` containers. Node identities,
ports and the engine under test are read from config/*.yaml, not hardcoded, so the
same suite checks whichever engine the stack runs (`engine: qgadmm`, `gadmm_fp` or
`gossip`). MEDCHAIN_PORT_OFFSET / MEDCHAIN_CONTAINER_PREFIX let it target a stack
whose published ports or container names were remapped by a compose override.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from pathlib import Path

import httpx
import psycopg
import pytest

from node.config import load_config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("MEDCHAIN_STACK") != "1", reason="set MEDCHAIN_STACK=1 with the compose stack up"),
]

ROOT = Path(__file__).resolve().parents[2]
NODES = {c.node_id: c for c in (load_config(p) for p in sorted((ROOT / "config").glob("*.yaml")))}
ENGINE = next(iter(NODES.values())).engine
ADMM = ENGINE in ("qgadmm", "gadmm_fp")
CHAIN = next(iter(NODES.values())).chain
BITS = next(iter(NODES.values())).bits
LABEL = {"gossip": "naive gossip baseline", "gadmm_fp": "GADMM full precision (float32 deltas)"}.get(ENGINE, f"Q-GADMM (b={BITS})")
D = 16  # 15 features + bias
PACKET = 8 * D if ENGINE == "gossip" else (4 * D + 8 if ENGINE == "gadmm_fp" else math.ceil(BITS * D / 8) + 8)

HOST = os.environ.get("MEDCHAIN_HOST", "localhost")
PORT_OFFSET = int(os.environ.get("MEDCHAIN_PORT_OFFSET", "0"))
PREFIX = os.environ.get("MEDCHAIN_CONTAINER_PREFIX", "")
DASHBOARD = os.environ.get("MEDCHAIN_DASHBOARD", f"http://{HOST}:{8080 + PORT_OFFSET}")
DB_URL = os.environ.get("MEDCHAIN_DB", f"postgresql://medchain:medchain@{HOST}:{5432 + PORT_OFFSET}/medchain")
DASHBOARD_CONTAINER = PREFIX + os.environ.get("MEDCHAIN_DASHBOARD_CONTAINER", "dashboard")
# the ADMM kill test kills a TAIL node (even chain position); hospital-b in the 3-node chain
KILL_NODE = os.environ.get("MEDCHAIN_KILL_NODE", CHAIN[1] if len(CHAIN) > 1 else "hospital-b")
GOSSIP_ROUNDS = 20
EPSILON = next(iter(NODES.values())).epsilon


def node_url(node_id: str) -> str:
    return f"http://{HOST}:{NODES[node_id].port + PORT_OFFSET}"


def get(url: str, **kw):
    return httpx.get(url, timeout=5, **kw)


def wait_until(pred, timeout: float, interval: float = 0.25, what: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = pred()
            if last:
                return last
        except (httpx.HTTPError, ValueError, KeyError, StopIteration):
            pass
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} (last={last!r})")


def snapshot() -> dict:
    return get(f"{DASHBOARD}/api/snapshot").json()


def metrics(node_id: str) -> dict:
    return get(f"{node_url(node_id)}/metrics").json()


def status(node_id: str) -> dict:
    return get(f"{node_url(node_id)}/status").json()


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True, text=True)


def all_nodes_healthy() -> bool:
    return all(get(f"{node_url(n)}/health").status_code == 200 for n in NODES)


def wait_idle(timeout: float = 150):
    """No node is mid-run (earlier tests may still be finishing)."""
    wait_until(lambda: all(metrics(n)["run_state"] not in ("running", "paused") for n in NODES), timeout, what="idle nodes")


def start_via_dashboard(rounds: int | None = None) -> str:
    """Press START. Without `rounds` every node runs its configured training.rounds (as the button does)."""
    r = httpx.post(f"{DASHBOARD}/api/round/start", json={"rounds": rounds} if rounds else {}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "started" and body["engine"] == LABEL
    return body["run_id"]


def start_run() -> str:
    return start_via_dashboard(None if ADMM else GOSSIP_ROUNDS)


def rounds_done(node_id: str, run_id: str) -> int:
    m = metrics(node_id)
    return len(m["history"]) if m["run_id"] == run_id else 0


def completed(node_id: str, run_id: str) -> bool:
    m = metrics(node_id)
    return m["run_id"] == run_id and m["run_state"] == "completed"


@pytest.fixture(scope="module", autouse=True)
def stack_up():
    wait_until(all_nodes_healthy, 90, what="all nodes healthy")
    wait_until(lambda: get(f"{DASHBOARD}/healthz").status_code == 200, 60, what="dashboard healthy")
    yield


def db_rows(sql: str, params=()):
    with psycopg.connect(DB_URL) as conn:
        return conn.execute(sql, params).fetchall()


admm_only = pytest.mark.skipif(not ADMM, reason=f"engine {ENGINE} is not an ADMM engine")
gossip_only = pytest.mark.skipif(ADMM, reason=f"engine {ENGINE} is not the gossip baseline")


# ---------------------------------------------------------------------------


def test_health_and_registration():
    for node_id, cfg in NODES.items():
        assert get(f"{node_url(node_id)}/health").json() == {"status": "ok", "node_id": node_id}
        s = status(node_id)
        assert s["engine"] == LABEL and s["engine_name"] == ENGINE
        assert sorted(n["id"] for n in s["neighbours"]) == sorted(n.id for n in cfg.neighbours)
        if ADMM:
            assert s["chain"] == list(CHAIN) and s["d"] == D
            assert s["group"] == ("head" if CHAIN.index(node_id) % 2 == 0 else "tail")

    def registered():
        s = snapshot()
        return s if len(s["nodes"]) == len(NODES) and all(n["status"] == "alive" for n in s["nodes"]) else None

    snap = wait_until(registered, 15, what="all nodes registered and alive")
    assert {n["node_id"] for n in snap["nodes"]} == set(NODES)
    assert all(n["seconds_since_heartbeat"] < 7 for n in snap["nodes"])
    assert all(n["engine"] == LABEL for n in snap["nodes"])


def test_dashboard_renders():
    html = get(f"{DASHBOARD}/").text
    assert '<div id="root">' in html
    for asset in [p.split('"')[0] for p in html.split('src="')[1:]] + [p.split('"')[0] for p in html.split('href="')[1:]]:
        if asset.startswith("/assets/"):
            assert get(f"{DASHBOARD}{asset}").status_code == 200, asset


def test_websocket_pushes_live_snapshots():
    from websockets.sync.client import connect

    ws_url = DASHBOARD.replace("http", "ws", 1) + "/ws"
    with connect(ws_url, open_timeout=5) as ws:
        first = json.loads(ws.recv(timeout=5))
        second = next(m for m in (json.loads(ws.recv(timeout=5)) for _ in range(20)) if m["type"] == "snapshot")
    assert first["type"] == second["type"] == "snapshot"
    assert second["data"]["generated_at"] > first["data"]["generated_at"]
    assert {n["node_id"] for n in second["data"]["nodes"]} == set(NODES)


def test_training_run_completes_and_is_audited():
    wait_idle()
    from websockets.sync.client import connect

    ws_url = DASHBOARD.replace("http", "ws", 1) + "/ws"
    with connect(ws_url, open_timeout=5) as ws:
        run_id = start_run()
        # one WebSocket "update" event per /update (drives the topology's envelope animation)
        events = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not events:
            msg = json.loads(ws.recv(timeout=10))
            if msg["type"] == "update":
                events = [e for e in msg["data"] if e["run_id"] == run_id]
    assert events and all(NODES[e["receiver"]].neighbour(e["sender"]) for e in events)
    assert {e["payload_bytes"] for e in events if e["status"] == "delivered"} <= {PACKET, 8}

    wait_until(lambda: all(completed(n, run_id) for n in NODES), 240, what="run completes on every node")
    hist = {n: metrics(n)["history"] for n in NODES}
    rounds = {n: len(h) for n, h in hist.items()}
    assert len(set(rounds.values())) == 1, rounds
    total = rounds[next(iter(NODES))]
    for n, h in hist.items():
        assert [x["round"] for x in h] == list(range(1, total + 1))
        assert all(x["neighbours_heard"] == len(NODES[n].neighbours) for x in h)
        if not ADMM:
            assert h[-1]["loss"] < h[0]["loss"], f"{n} loss did not fall"
    # the objective the hospitals minimise together falls (for the ADMM engines one non-IID hospital's own
    # held-out loss may rise a little as it agrees on the shared model; that is shown, not hidden)
    net = snapshot()["network_loss"]
    assert [p["round"] for p in net] == list(range(1, total + 1))
    assert net[-1]["loss"] < net[0]["loss"] - 0.01, "network training loss did not fall"

    edges = sum(len(c.neighbours) for c in NODES.values())
    sent = db_rows("SELECT payload_bytes, full_precision_bytes, bits, status FROM updates_meta WHERE run_id = %s", (run_id,))
    assert len(sent) == edges * total
    assert all(s == "delivered" for *_, s in sent)
    audited = db_rows(
        "SELECT sender, receiver, payload_bytes, checksum, detail FROM audit_logs WHERE run_id = %s AND event = 'update_received'",
        (run_id,),
    )
    assert len(audited) == edges * total
    for sender, receiver, payload, checksum, detail in audited:
        assert NODES[receiver].neighbour(sender) is not None
        assert len(checksum) == 64 and detail["verified"] is True and detail["engine"] == LABEL
    snap = snapshot()
    assert snap["run"]["run_id"] == run_id and snap["run"]["state"] == "completed" and snap["engine"] == LABEL

    if not ADMM:  # the baseline ships the raw float64 weight vector, nothing else
        assert total == GOSSIP_ROUNDS
        assert {(p, f) for p, f, _, _ in sent} == {(128, 128)}
        assert all(p == 128 for _, _, p, _, _ in audited)
        assert snap["comm"]["bytes_this_round"] == edges * 128
        assert snap["comm"]["total_bytes"] == edges * 128 * GOSSIP_ROUNDS
        assert snap["consensus"]["edges"] == []
        assert db_rows("SELECT count(*) FROM metrics WHERE run_id = %s AND residuals IS NOT NULL", (run_id,))[0][0] == 0
        return

    # ADMM: stopped by consensus before the round limit, every node on the same round
    limit = next(iter(NODES.values())).training.rounds
    assert total < limit, f"no consensus within {limit} rounds"
    assert {p for p, *_ in sent} <= {PACKET, 8}  # 8 = the header-only "no change" packet
    assert sum(p == PACKET for p, *_ in sent) > 0.95 * len(sent)
    assert {(f, b) for _, f, b, _ in sent} == {(8 * D, 32 if ENGINE == "gadmm_fp" else BITS)}
    # the real communication numbers the dashboard shows
    c = snap["comm"]
    assert c["d"] == D and c["messages_total"] == edges * total
    assert c["ref32_total_bytes"] == 4 * D * edges * total
    assert c["total_bytes"] == sum(p for p, *_ in sent)
    if ENGINE == "qgadmm":
        assert c["ref32_total_bytes"] / c["total_bytes"] == pytest.approx(4 * D / PACKET, rel=0.02)
        assert c["coordinate_bytes_total"] == sum(max(p - 8, 0) for p, *_ in sent)
    # residuals collapse; the consensus pill's condition holds
    s = snap["consensus"]
    assert s["edges"] == sorted(f"{a}|{b}" for a, b in zip(CHAIN, CHAIN[1:]))
    assert s["epsilon"] == EPSILON and s["converged_round"] is not None
    first = s["series"][0]
    assert max(first[e] for e in s["edges"]) > 100 * EPSILON
    streak = [p for p in s["series"] if s["converged_round"] - 2 <= p["round"] <= s["converged_round"]]
    assert len(streak) == 3 and all(p[e] < EPSILON for p in streak for e in s["edges"])
    detected = db_rows("SELECT count(*) FROM audit_logs WHERE run_id = %s AND event = 'consensus_detected'", (run_id,))
    assert detected[0][0] >= 1
    assert db_rows("SELECT count(*) FROM metrics WHERE run_id = %s AND residuals IS NULL", (run_id,))[0][0] == 0


@gossip_only
def test_kill_node_turns_orange_and_survivors_keep_training():
    wait_idle()
    run_id = start_via_dashboard(GOSSIP_ROUNDS)
    survivors = [n for n in NODES if n != KILL_NODE]
    wait_until(lambda: all(rounds_done(n, run_id) >= 3 for n in NODES), 60, what="3 rounds before the kill")
    try:
        docker("kill", PREFIX + KILL_NODE)
        killed_at = time.monotonic()
        wait_until(
            lambda: next(n for n in snapshot()["nodes"] if n["node_id"] == KILL_NODE)["status"] == "down",
            12, interval=0.2, what=f"{KILL_NODE} marked down",
        )
        took = time.monotonic() - killed_at
        print(f"{KILL_NODE} marked down {took:.1f}s after docker kill")
        assert took <= 10, f"{KILL_NODE} took {took:.1f}s to turn orange"
        at_kill = {n: rounds_done(n, run_id) for n in survivors}

        wait_until(lambda: all(rounds_done(n, run_id) == GOSSIP_ROUNDS for n in survivors), 150, what="survivors finish the run")
        for n in survivors:
            assert rounds_done(n, run_id) > at_kill[n], f"{n} stopped training after the kill"
            assert metrics(n)["run_state"] == "completed"
        assert all(n["status"] == "alive" for n in snapshot()["nodes"] if n["node_id"] in survivors)
        unreachable = db_rows(
            "SELECT count(*) FROM updates_meta WHERE run_id = %s AND receiver = %s AND status = 'unreachable'", (run_id, KILL_NODE)
        )[0][0]
        assert unreachable > 0
    finally:
        docker("start", PREFIX + KILL_NODE)
        wait_until(lambda: get(f"{node_url(KILL_NODE)}/health").status_code == 200, 60, what=f"{KILL_NODE} back up")
        wait_until(lambda: next(n for n in snapshot()["nodes"] if n["node_id"] == KILL_NODE)["status"] == "alive", 20,
                   what=f"{KILL_NODE} alive again")


@admm_only
def test_kill_a_tail_mid_round_heads_continue_and_it_rejoins_from_its_checkpoint():
    """docker kill a TAIL node mid-run; the heads keep training; on restart it reloads its checkpoint,
    resyncs its mirrors with one GET /snapshot per neighbour, rejoins at the current round, and the
    residuals resume falling to consensus."""
    assert CHAIN.index(KILL_NODE) % 2 == 1, f"{KILL_NODE} is not a TAIL node"
    heads = [n for n in NODES if n != KILL_NODE and KILL_NODE in {x.id for x in NODES[n].neighbours}]
    wait_idle()
    # a larger round budget than the default: while the tail is down the heads run on alone (tens of
    # rounds), and after the rejoin the chain needs roughly a fresh run's worth of rounds to agree again
    budget = 400
    run_id = start_via_dashboard(budget)
    wait_until(lambda: rounds_done(KILL_NODE, run_id) >= 12, 90, what=f"{KILL_NODE} past round 12 (a checkpoint exists)")
    try:
        docker("kill", PREFIX + KILL_NODE)
        killed_at = time.monotonic()
        at_kill = {n: rounds_done(n, run_id) for n in heads}
        wait_until(lambda: next(n for n in snapshot()["nodes"] if n["node_id"] == KILL_NODE)["status"] == "down",
                   12, interval=0.2, what=f"{KILL_NODE} marked down")
        print(f"{KILL_NODE} marked down {time.monotonic() - killed_at:.1f}s after docker kill; heads at {at_kill}")
        wait_until(lambda: all(rounds_done(n, run_id) >= at_kill[n] + 5 for n in heads), 60, what="heads keep training")
        for n in heads:
            assert metrics(n)["run_state"] == "running"
    finally:
        docker("start", PREFIX + KILL_NODE)
    wait_until(lambda: get(f"{node_url(KILL_NODE)}/health").status_code == 200, 90, what=f"{KILL_NODE} back up")

    def rejoined():
        rows = db_rows("SELECT round, detail FROM audit_logs WHERE run_id = %s AND node_id = %s AND event = 'rejoined_from_checkpoint'",
                       (run_id, KILL_NODE))
        return rows[0] if rows else None

    rejoin_round, detail = wait_until(rejoined, 60, what=f"{KILL_NODE} rejoined from its checkpoint")
    print(f"{KILL_NODE} rejoined at round {rejoin_round}: {detail}")
    assert detail["checkpoint_round"] >= 10 and detail["checkpoint_round"] % NODES[KILL_NODE].checkpoint_every == 0
    assert rejoin_round > max(at_kill.values())
    assert sorted(detail["resynced_from"]) == sorted(heads)
    resyncs = db_rows("SELECT sender, payload_bytes, detail FROM audit_logs WHERE run_id = %s AND node_id = %s "
                      "AND event = 'snapshot_resync'", (run_id, KILL_NODE))
    assert sorted(s for s, _, _ in resyncs) == sorted(heads)
    assert all(p == 3 * D * 8 and d["reason"] == "rejoin after restart" for _, p, d in resyncs)  # byte cost audited
    served = db_rows("SELECT node_id, receiver FROM audit_logs WHERE run_id = %s AND event = 'snapshot_served'", (run_id,))
    assert {(n, r) for n, r in served} >= {(h, KILL_NODE) for h in heads}
    degraded = db_rows("SELECT DISTINCT node_id FROM audit_logs WHERE run_id = %s AND event = 'round_degraded'", (run_id,))
    assert {h for (h,) in degraded} >= set(heads), "heads should log degraded rounds while the tail is down"

    wait_until(lambda: all(completed(n, run_id) for n in NODES), 240, what="run completes after the rejoin")
    final = {n: status(n)["current_round"] for n in NODES}
    assert len(set(final.values())) == 1, f"nodes stopped on different rounds: {final}"
    hist = metrics(KILL_NODE)["history"]
    assert hist[0]["round"] == rejoin_round
    # residuals resume falling: from the post-rejoin disagreement down to consensus
    assert max(h["max_residual"] for h in hist[:5]) > 10 * EPSILON
    assert final[KILL_NODE] < budget, "no consensus after the rejoin (hit the round limit)"
    assert hist[-1]["max_residual"] < 10 * EPSILON
    snap = snapshot()
    assert snap["consensus"]["converged_round"] is not None and snap["consensus"]["converged_round"] > rejoin_round
    assert next(n for n in snap["nodes"] if n["node_id"] == KILL_NODE)["status"] == "alive"


def test_killing_the_dashboard_does_not_affect_training():
    wait_idle()
    run_id = start_run()
    wait_until(lambda: all(rounds_done(n, run_id) >= 2 for n in NODES), 60, what="2 rounds before killing the dashboard")
    try:
        docker("kill", DASHBOARD_CONTAINER)
        with pytest.raises(httpx.HTTPError):
            get(f"{DASHBOARD}/healthz")
        wait_until(lambda: all(completed(n, run_id) for n in NODES), 240, what="run completes with dashboard dead")
        for n in NODES:
            hist = metrics(n)["history"]
            assert all(h["neighbours_heard"] == len(NODES[n].neighbours) for h in hist), f"{n} lost neighbours"
            assert not any(h.get("degraded") for h in hist), f"{n} had degraded rounds"
    finally:
        docker("start", DASHBOARD_CONTAINER)
        wait_until(lambda: get(f"{DASHBOARD}/healthz").status_code == 200, 60, what="dashboard back up")
    # the observer catches up from Postgres after restart
    assert wait_until(lambda: snapshot()["run"]["run_id"] == run_id and snapshot()["run"]["state"] == "completed", 15,
                      what="dashboard shows the completed run")
