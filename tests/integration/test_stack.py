"""End-to-end tests against the running docker compose stack.

    docker compose up -d --build
    MEDCHAIN_STACK=1 pytest tests/integration

They drive real training runs and really `docker kill` containers. Node identities
and ports are read from config/*.yaml, not hardcoded.
"""

from __future__ import annotations

import json
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
HOST = os.environ.get("MEDCHAIN_HOST", "localhost")
DASHBOARD = os.environ.get("MEDCHAIN_DASHBOARD", f"http://{HOST}:8080")
DB_URL = os.environ.get("MEDCHAIN_DB", f"postgresql://medchain:medchain@{HOST}:5432/medchain")
KILL_NODE = os.environ.get("MEDCHAIN_KILL_NODE", "hospital-b")
DASHBOARD_CONTAINER = os.environ.get("MEDCHAIN_DASHBOARD_CONTAINER", "dashboard")
ROUNDS = 20


def node_url(node_id: str) -> str:
    return f"http://{HOST}:{NODES[node_id].port}"


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
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} (last={last!r})")


def snapshot() -> dict:
    return get(f"{DASHBOARD}/api/snapshot").json()


def metrics(node_id: str) -> dict:
    return get(f"{node_url(node_id)}/metrics").json()


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True, text=True)


def all_nodes_healthy() -> bool:
    return all(get(f"{node_url(n)}/health").status_code == 200 for n in NODES)


def wait_idle(timeout: float = 90):
    """No node is mid-run (earlier tests may still be finishing)."""
    wait_until(lambda: all(metrics(n)["run_state"] not in ("running", "paused") for n in NODES), timeout, what="idle nodes")


def start_via_dashboard(rounds: int = ROUNDS) -> str:
    r = httpx.post(f"{DASHBOARD}/api/round/start", json={"rounds": rounds}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "started" and body["engine"] == "naive gossip baseline"
    return body["run_id"]


def rounds_done(node_id: str, run_id: str) -> int:
    m = metrics(node_id)
    return len(m["history"]) if m["run_id"] == run_id else 0


@pytest.fixture(scope="module", autouse=True)
def stack_up():
    wait_until(all_nodes_healthy, 60, what="all nodes healthy")
    wait_until(lambda: get(f"{DASHBOARD}/healthz").status_code == 200, 60, what="dashboard healthy")
    yield


def db_rows(sql: str, params=()):
    with psycopg.connect(DB_URL) as conn:
        return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------


def test_health_and_registration():
    for node_id, cfg in NODES.items():
        assert get(f"{node_url(node_id)}/health").json() == {"status": "ok", "node_id": node_id}
        status = get(f"{node_url(node_id)}/status").json()
        assert status["engine"] == "naive gossip baseline"
        assert sorted(n["id"] for n in status["neighbours"]) == sorted(n.id for n in cfg.neighbours)
    def registered():
        s = snapshot()
        return s if len(s["nodes"]) == len(NODES) and all(n["status"] == "alive" for n in s["nodes"]) else None

    snap = wait_until(registered, 15, what="all nodes registered and alive")
    assert {n["node_id"] for n in snap["nodes"]} == set(NODES)
    assert all(n["seconds_since_heartbeat"] < 7 for n in snap["nodes"])


def test_websocket_pushes_live_snapshots():
    from websockets.sync.client import connect

    ws_url = DASHBOARD.replace("http", "ws", 1) + "/ws"
    with connect(ws_url, open_timeout=5) as ws:
        first = json.loads(ws.recv(timeout=5))
        second = json.loads(ws.recv(timeout=5))
    assert first["type"] == second["type"] == "snapshot"
    assert second["data"]["generated_at"] > first["data"]["generated_at"]
    assert {n["node_id"] for n in second["data"]["nodes"]} == set(NODES)


def test_training_round_completes_and_is_audited():
    wait_idle()
    run_id = start_via_dashboard()
    wait_until(lambda: all(rounds_done(n, run_id) == ROUNDS for n in NODES), 120, what="20 rounds on every node")

    for n in NODES:
        hist = metrics(n)["history"]
        assert [h["round"] for h in hist] == list(range(1, ROUNDS + 1))
        assert hist[-1]["loss"] < hist[0]["loss"], f"{n} loss did not fall"
        assert all(h["neighbours_heard"] == len(NODES[n].neighbours) for h in hist)

    edges = sum(len(c.neighbours) for c in NODES.values())
    sent = db_rows("SELECT payload_bytes, full_precision_bytes, status FROM updates_meta WHERE run_id = %s", (run_id,))
    assert len(sent) == edges * ROUNDS
    assert all(s == "delivered" for _, _, s in sent)
    # honest accounting: the baseline ships the raw float64 weight vector, nothing else
    assert {(p, f) for p, f, _ in sent} == {(128, 128)}

    audited = db_rows(
        "SELECT sender, receiver, payload_bytes, checksum, detail FROM audit_logs WHERE run_id = %s AND event = 'update_received'",
        (run_id,),
    )
    assert len(audited) == edges * ROUNDS
    for sender, receiver, payload, checksum, detail in audited:
        assert NODES[receiver].neighbour(sender) is not None
        assert payload == 128 and len(checksum) == 64 and detail["verified"] is True

    snap = snapshot()
    assert snap["run"]["run_id"] == run_id and snap["run"]["state"] == "completed"
    assert snap["comm"]["bytes_this_round"] == edges * 128
    assert snap["comm"]["total_bytes"] == edges * 128 * ROUNDS


def test_kill_node_turns_orange_and_survivors_keep_training():
    wait_idle()
    run_id = start_via_dashboard()
    survivors = [n for n in NODES if n != KILL_NODE]
    wait_until(lambda: all(rounds_done(n, run_id) >= 3 for n in NODES), 60, what="3 rounds before the kill")
    try:
        docker("kill", KILL_NODE)
        killed_at = time.monotonic()
        wait_until(
            lambda: next(n for n in snapshot()["nodes"] if n["node_id"] == KILL_NODE)["status"] == "down",
            12, interval=0.2, what=f"{KILL_NODE} marked down",
        )
        took = time.monotonic() - killed_at
        print(f"{KILL_NODE} marked down {took:.1f}s after docker kill")
        assert took <= 10, f"{KILL_NODE} took {took:.1f}s to turn orange"
        at_kill = {n: rounds_done(n, run_id) for n in survivors}

        wait_until(lambda: all(rounds_done(n, run_id) == ROUNDS for n in survivors), 150, what="survivors finish the run")
        for n in survivors:
            assert rounds_done(n, run_id) > at_kill[n], f"{n} stopped training after the kill"
            assert metrics(n)["run_state"] == "completed"
        assert all(n["status"] == "alive" for n in snapshot()["nodes"] if n["node_id"] in survivors)
        unreachable = db_rows(
            "SELECT count(*) FROM updates_meta WHERE run_id = %s AND receiver = %s AND status = 'unreachable'", (run_id, KILL_NODE)
        )[0][0]
        assert unreachable > 0
    finally:
        docker("start", KILL_NODE)
        wait_until(lambda: get(f"{node_url(KILL_NODE)}/health").status_code == 200, 60, what=f"{KILL_NODE} back up")
        wait_until(lambda: next(n for n in snapshot()["nodes"] if n["node_id"] == KILL_NODE)["status"] == "alive", 20,
                   what=f"{KILL_NODE} alive again")


def test_killing_the_dashboard_does_not_affect_training():
    wait_idle()
    run_id = start_via_dashboard()
    wait_until(lambda: all(rounds_done(n, run_id) >= 2 for n in NODES), 60, what="2 rounds before killing the dashboard")
    try:
        docker("kill", DASHBOARD_CONTAINER)
        with pytest.raises(httpx.HTTPError):
            get(f"{DASHBOARD}/healthz")
        wait_until(lambda: all(rounds_done(n, run_id) == ROUNDS for n in NODES), 120, what="run completes with dashboard dead")
        for n in NODES:
            hist = metrics(n)["history"]
            assert all(h["neighbours_heard"] == len(NODES[n].neighbours) for h in hist), f"{n} lost neighbours"
    finally:
        docker("start", DASHBOARD_CONTAINER)
        wait_until(lambda: get(f"{DASHBOARD}/healthz").status_code == 200, 60, what="dashboard back up")
    # the observer catches up from Postgres after restart
    assert wait_until(lambda: snapshot()["run"]["run_id"] == run_id and snapshot()["run"]["state"] == "completed", 15,
                      what="dashboard shows the completed run")
