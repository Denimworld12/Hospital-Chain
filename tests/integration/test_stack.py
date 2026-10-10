"""End-to-end tests against the running docker compose stack.

    docker compose up -d --build
    MEDCHAIN_STACK=1 pytest tests/integration

They drive real training runs and really `docker kill` containers. Node identities,
ports and the engine under test are read from config/hospital-*.yaml and the chain from
the live nodes, not hardcoded, so the same suite checks whichever engine the stack runs
(`engine: qgadmm`, `gadmm_fp` or `gossip`). MEDCHAIN_PORT_OFFSET / MEDCHAIN_CONTAINER_PREFIX
/ MEDCHAIN_COMPOSE let it target a stack whose published ports or container names were
remapped by a compose override. Every node is reached over HTTPS, checked against the
stack's CA (.pki/ca.crt, written by the compose `pki` service).
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
# the hospitals a plain `docker compose up` starts; hospital-d (profile `join`, Swiss data) joins later
STARTED = os.environ.get("MEDCHAIN_NODES", "hospital-a,hospital-b,hospital-c").split(",")
ALL = {c.node_id: c for c in (load_config(p) for p in sorted((ROOT / "config").glob("hospital-*.yaml")))}
NODES = {n: ALL[n] for n in STARTED}
JOINER = os.environ.get("MEDCHAIN_JOINER", "hospital-d") if os.environ.get("MEDCHAIN_JOINER", "hospital-d") in ALL else None
ENGINE = next(iter(NODES.values())).engine
ADMM = ENGINE in ("qgadmm", "gadmm_fp")
CA = os.environ.get("MEDCHAIN_CA", str(ROOT / ".pki" / "ca.crt"))
COMPOSE = os.environ.get("MEDCHAIN_COMPOSE", "docker compose").split()
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
GOSSIP_ROUNDS = 20
EPSILON = next(iter(NODES.values())).epsilon


def node_url(node_id: str) -> str:
    return f"https://{HOST}:{ALL[node_id].port + PORT_OFFSET}"


def get(url: str, **kw):
    return httpx.get(url, timeout=5, verify=CA if url.startswith("https") else True, **kw)


def post_node(node_id: str, path: str, **kw):
    secret = ALL[node_id].shared_secret
    return httpx.post(f"{node_url(node_id)}{path}", headers={"X-MedChain-Secret": secret}, timeout=30, verify=CA, **kw)


def in_chain(rows: list[dict]) -> list[dict]:
    """Snapshot node rows of hospitals in the chain (a hospital that joined and left keeps its row)."""
    return [n for n in rows if n.get("in_chain") is not False]


def current_chain() -> list[str]:
    return get(f"{node_url(STARTED[0])}/status").json()["chain"]


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


def all_placed() -> bool:
    """Every started hospital registered with the directory and sees the same chain of all of them."""
    chains = {tuple(status(n)["chain"]) for n in NODES}
    return len(chains) == 1 and set(chains.pop()) == set(NODES)


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
    wait_until(all_placed, 60, what="every hospital placed in one chain by the directory")
    wait_until(lambda: get(f"{DASHBOARD}/healthz").status_code == 200, 60, what="dashboard healthy")
    yield


def db_rows(sql: str, params=()):
    with psycopg.connect(DB_URL) as conn:
        return conn.execute(sql, params).fetchall()


admm_only = pytest.mark.skipif(not ADMM, reason=f"engine {ENGINE} is not an ADMM engine")
gossip_only = pytest.mark.skipif(ADMM, reason=f"engine {ENGINE} is not the gossip baseline")


# ---------------------------------------------------------------------------


def test_health_and_registration():
    chain = current_chain()
    assert chain == sorted(NODES), "no run in progress: the directory keeps its configured order"
    for node_id in NODES:
        assert get(f"{node_url(node_id)}/health").json() == {"status": "ok", "node_id": node_id}
        s = status(node_id)
        assert s["engine"] == LABEL and s["engine_name"] == ENGINE
        assert s["tls"] and s["signed_updates"] and s["in_chain"]
        i = chain.index(node_id)
        assert [n["id"] for n in s["neighbours"]] == [chain[j] for j in (i - 1, i + 1) if 0 <= j < len(chain)]
        assert s["position"] == i + 1
        if ADMM:
            assert s["chain"] == chain and s["d"] == D
            assert s["group"] == ("head" if i % 2 == 0 else "tail")
    with pytest.raises(httpx.HTTPError):  # plain HTTP is not served
        httpx.get(node_url(STARTED[0]).replace("https", "http", 1) + "/health", timeout=3)

    def registered():
        s = snapshot()
        members = in_chain(s["nodes"])
        return s if len(members) == len(NODES) and all(n["status"] == "alive" for n in members) else None

    snap = wait_until(registered, 15, what="all nodes registered and alive")
    assert {n["node_id"] for n in in_chain(snap["nodes"])} == set(NODES)
    assert all(n["seconds_since_heartbeat"] < 7 for n in in_chain(snap["nodes"]))
    assert all(n["engine"] == LABEL for n in in_chain(snap["nodes"]))


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
    assert {n["node_id"] for n in in_chain(second["data"]["nodes"])} == set(NODES)


PATIENT = {"age": 58, "gender": "male", "height": 172, "weight": 88, "ap_hi": 150, "ap_lo": 95,
           "cholesterol": 2, "gluc": 1, "smoke": True, "alco": False, "active": False}


def ecg_png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_risk_assessment_on_every_node_and_via_the_dashboard():
    secret = next(iter(NODES.values())).shared_secret
    for node_id in NODES:
        assert get(f"{node_url(node_id)}/status").json()["assess"] == {"tabular": True, "ecg": True}
        assert httpx.post(f"{node_url(node_id)}/assess", json=PATIENT, timeout=15, verify=CA).status_code == 401
        r = httpx.post(f"{node_url(node_id)}/assess", json=PATIENT, headers={"X-MedChain-Secret": secret}, timeout=15, verify=CA)
        assert r.status_code == 200 and r.json()["node_id"] == node_id
        assert r.json()["risk_tier"] in ("LOW", "MODERATE", "HIGH")
    last = sorted(NODES)[-1]
    via = httpx.post(f"{DASHBOARD}/api/assess", params={"node": last}, json=PATIENT, timeout=15)
    assert via.status_code == 200 and via.json()["node_id"] == last
    assert via.json()["model"]["metrics"]["roc_auc"] > 0.75  # the full model, trained at image build
    pdf = httpx.post(f"{DASHBOARD}/api/assess/pdf", json=PATIENT, timeout=15)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    ecg = httpx.post(f"{DASHBOARD}/api/assess/ecg", files={"ecg": ("ecg.png", ecg_png(), "image/png")}, timeout=15)
    assert ecg.status_code == 200 and set(ecg.json()["probabilities"]) == {"normal", "mi", "abnormal"}


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
    chain = current_chain()
    links = {frozenset(p) for p in zip(chain, chain[1:])}
    assert events and all(frozenset((e["sender"], e["receiver"])) in links for e in events)
    assert {e["payload_bytes"] for e in events if e["status"] == "delivered"} <= {PACKET, 8}

    wait_until(lambda: all(completed(n, run_id) for n in NODES), 240, what="run completes on every node")
    hist = {n: metrics(n)["history"] for n in NODES}
    rounds = {n: len(h) for n, h in hist.items()}
    assert len(set(rounds.values())) == 1, rounds
    total = rounds[next(iter(NODES))]
    for n, h in hist.items():
        assert [x["round"] for x in h] == list(range(1, total + 1))
        assert all(x["neighbours_heard"] == len(status(n)["neighbours"]) for x in h)
        if not ADMM:
            assert h[-1]["loss"] < h[0]["loss"], f"{n} loss did not fall"
    # the objective the hospitals minimise together falls (for the ADMM engines one non-IID hospital's own
    # held-out loss may rise a little as it agrees on the shared model; that is shown, not hidden)
    net = snapshot()["network_loss"]
    assert [p["round"] for p in net] == list(range(1, total + 1))
    assert net[-1]["loss"] < net[0]["loss"] - 0.01, "network training loss did not fall"

    edges = sum(len(status(n)["neighbours"]) for n in NODES)
    sent = db_rows("SELECT payload_bytes, full_precision_bytes, bits, status FROM updates_meta WHERE run_id = %s", (run_id,))
    assert len(sent) == edges * total
    assert all(s == "delivered" for *_, s in sent)
    audited = db_rows(
        "SELECT sender, receiver, payload_bytes, checksum, detail FROM audit_logs WHERE run_id = %s AND event = 'update_received'",
        (run_id,),
    )
    assert len(audited) == edges * total
    for sender, receiver, payload, checksum, detail in audited:
        assert frozenset((sender, receiver)) in links
        assert len(checksum) == 64 and detail["verified"] is True and detail["signed"] is True
        assert detail["engine"] == LABEL
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
    assert s["edges"] == [f"{a}|{b}" for a, b in zip(chain, chain[1:])]
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
    # long enough that the run outlasts the heartbeat-based down detection (~7s) and the survivors are
    # still visibly training afterwards (a round takes ~0.2s with the configured round_pause_s)
    rounds = 100
    run_id = start_via_dashboard(rounds)
    kill_node = current_chain()[1]
    survivors = [n for n in NODES if n != kill_node]
    wait_until(lambda: all(rounds_done(n, run_id) >= 3 for n in NODES), 60, what="3 rounds before the kill")
    try:
        docker("kill", PREFIX + kill_node)
        killed_at = time.monotonic()
        wait_until(
            lambda: next(n for n in snapshot()["nodes"] if n["node_id"] == kill_node)["status"] == "down",
            12, interval=0.2, what=f"{kill_node} marked down",
        )
        took = time.monotonic() - killed_at
        print(f"{kill_node} marked down {took:.1f}s after docker kill")
        assert took <= 10, f"{kill_node} took {took:.1f}s to turn orange"
        at_kill = {n: rounds_done(n, run_id) for n in survivors}

        assert all(at_kill[n] < rounds for n in survivors), f"run finished before the kill was detected: {at_kill}"
        wait_until(lambda: all(rounds_done(n, run_id) == rounds for n in survivors), 150, what="survivors finish the run")
        for n in survivors:
            assert rounds_done(n, run_id) > at_kill[n], f"{n} stopped training after the kill"
            assert metrics(n)["run_state"] == "completed"
        assert all(n["status"] == "alive" for n in snapshot()["nodes"] if n["node_id"] in survivors)
        unreachable = db_rows(
            "SELECT count(*) FROM updates_meta WHERE run_id = %s AND receiver = %s AND status = 'unreachable'", (run_id, kill_node)
        )[0][0]
        assert unreachable > 0
    finally:
        docker("start", PREFIX + kill_node)
        wait_until(lambda: get(f"{node_url(kill_node)}/health").status_code == 200, 60, what=f"{kill_node} back up")
        wait_until(lambda: next(n for n in snapshot()["nodes"] if n["node_id"] == kill_node)["status"] == "alive", 20,
                   what=f"{kill_node} alive again")


@admm_only
def test_kill_a_tail_mid_round_heads_continue_and_it_rejoins_from_its_checkpoint():
    """docker kill a TAIL node mid-run; the heads keep training; on restart it reloads its checkpoint,
    resyncs its mirrors with one GET /snapshot per neighbour, rejoins at the current round, and the
    residuals resume falling to consensus."""
    chain = current_chain()
    kill_node = chain[1]  # position 2: a TAIL
    heads = [chain[0], chain[2]]
    wait_idle()
    # a larger round budget than the default: while the tail is down the heads run on alone (tens of
    # rounds), and after the rejoin the chain needs roughly a fresh run's worth of rounds to agree again
    budget = 400
    run_id = start_via_dashboard(budget)
    wait_until(lambda: rounds_done(kill_node, run_id) >= 12, 90, what=f"{kill_node} past round 12 (a checkpoint exists)")
    try:
        docker("kill", PREFIX + kill_node)
        killed_at = time.monotonic()
        at_kill = {n: rounds_done(n, run_id) for n in heads}
        wait_until(lambda: next(n for n in snapshot()["nodes"] if n["node_id"] == kill_node)["status"] == "down",
                   12, interval=0.2, what=f"{kill_node} marked down")
        print(f"{kill_node} marked down {time.monotonic() - killed_at:.1f}s after docker kill; heads at {at_kill}")
        wait_until(lambda: all(rounds_done(n, run_id) >= at_kill[n] + 5 for n in heads), 60, what="heads keep training")
        for n in heads:
            assert metrics(n)["run_state"] == "running"
    finally:
        docker("start", PREFIX + kill_node)
    wait_until(lambda: get(f"{node_url(kill_node)}/health").status_code == 200, 90, what=f"{kill_node} back up")

    def rejoined():
        rows = db_rows("SELECT round, detail FROM audit_logs WHERE run_id = %s AND node_id = %s AND event = 'rejoined_from_checkpoint'",
                       (run_id, kill_node))
        return rows[0] if rows else None

    rejoin_round, detail = wait_until(rejoined, 60, what=f"{kill_node} rejoined from its checkpoint")
    print(f"{kill_node} rejoined at round {rejoin_round}: {detail}")
    assert detail["checkpoint_round"] >= 10 and detail["checkpoint_round"] % ALL[kill_node].checkpoint_every == 0
    assert rejoin_round > max(at_kill.values())
    assert sorted(detail["resynced_from"]) == sorted(heads)
    resyncs = db_rows("SELECT sender, payload_bytes, detail FROM audit_logs WHERE run_id = %s AND node_id = %s "
                      "AND event = 'snapshot_resync'", (run_id, kill_node))
    assert sorted(s for s, _, _ in resyncs) == sorted(heads)
    assert all(p == 3 * D * 8 and d["reason"] == "rejoin after restart" for _, p, d in resyncs)  # byte cost audited
    served = db_rows("SELECT node_id, receiver FROM audit_logs WHERE run_id = %s AND event = 'snapshot_served'", (run_id,))
    assert {(n, r) for n, r in served} >= {(h, kill_node) for h in heads}
    degraded = db_rows("SELECT DISTINCT node_id FROM audit_logs WHERE run_id = %s AND event = 'round_degraded'", (run_id,))
    assert {h for (h,) in degraded} >= set(heads), "heads should log degraded rounds while the tail is down"

    wait_until(lambda: all(completed(n, run_id) for n in NODES), 240, what="run completes after the rejoin")
    final = {n: status(n)["current_round"] for n in NODES}
    assert len(set(final.values())) == 1, f"nodes stopped on different rounds: {final}"
    hist = metrics(kill_node)["history"]
    assert hist[0]["round"] == rejoin_round
    # residuals resume falling: from the post-rejoin disagreement down to consensus
    assert max(h["max_residual"] for h in hist[:5]) > 10 * EPSILON
    assert final[kill_node] < budget, "no consensus after the rejoin (hit the round limit)"
    assert hist[-1]["max_residual"] < 10 * EPSILON
    snap = snapshot()
    assert snap["consensus"]["converged_round"] is not None and snap["consensus"]["converged_round"] > rejoin_round
    assert next(n for n in snap["nodes"] if n["node_id"] == kill_node)["status"] == "alive"


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
            assert all(h["neighbours_heard"] == len(status(n)["neighbours"]) for h in hist), f"{n} lost neighbours"
            assert not any(h.get("degraded") for h in hist), f"{n} had degraded rounds"
    finally:
        docker("start", DASHBOARD_CONTAINER)
        wait_until(lambda: get(f"{DASHBOARD}/healthz").status_code == 200, 60, what="dashboard back up")
    # the observer catches up from Postgres after restart
    assert wait_until(lambda: snapshot()["run"]["run_id"] == run_id and snapshot()["run"]["state"] == "completed", 15,
                      what="dashboard shows the completed run")


def compose(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], check=True, capture_output=True, text=True, cwd=ROOT)


def loss_rounds(s: dict) -> set[int]:
    """Rounds in the dashboard's network loss: a round shows once every member's metrics row reached the observer,
    which each node pushes from its own queue, so it can trail the round's comm counts by a moment."""
    return {p["round"] for p in s["network_loss"]}


@admm_only
@pytest.mark.skipif(JOINER is None, reason="no spare hospital config to join")
def test_a_hospital_joins_mid_run_then_a_middle_hospital_leaves_and_the_dashboard_follows():
    """Chain of 3 -> hospital-d joins mid-run (appended) -> hospital-b leaves (a and c bridge) -> converges.

    Checked from outside, the way an operator sees it: the nodes' /status, the observer database and
    the dashboard's snapshot (topology, consensus, network training loss, communication)."""
    wait_idle()
    start_chain = current_chain()
    n = len(start_chain)
    leaver = start_chain[1]
    run_id = start_via_dashboard(400)
    try:
        wait_until(lambda: rounds_done(start_chain[0], run_id) >= 3, 60, what="the run is going")
        compose("--profile", "join", "up", "-d", JOINER)

        # -- join: appended at the end, at a round boundary, then trained with the others
        joined = wait_until(lambda: db_rows("SELECT round, detail FROM audit_logs WHERE run_id = %s AND node_id = %s "
                                            "AND event = 'joined_run'", (run_id, JOINER)), 90, what=f"{JOINER} joins")
        join_round, detail = joined[0]
        grown = start_chain + [JOINER]
        assert detail["chain"] == grown and detail["start_round"] == join_round
        for m in start_chain:  # every member switched on that same boundary (written when it reaches it)
            changed = wait_until(lambda: db_rows("SELECT round FROM audit_logs WHERE run_id = %s AND node_id = %s "
                                                 "AND event = 'membership_changed'", (run_id, m)), 60,
                                 what=f"{m} reaches the boundary")
            assert [r for (r,) in changed] == [join_round], f"{m} switched on a different round"

        def after_join():
            s = snapshot()
            return s if (s["topology"]["chain"] == grown and s["comm"]["hospitals_this_round"] == n + 1
                         and loss_rounds(s) >= {join_round - 1, join_round}) else None

        s = wait_until(after_join, 60, what=f"the dashboard shows {n + 1} hospitals")
        assert s["topology"]["links"] == [f"{a}|{b}" for a, b in zip(grown, grown[1:])]
        assert s["consensus"]["edges"] == s["topology"]["links"]  # the new pair line appears
        assert s["consensus"]["epoch_start_round"] == join_round and not s["consensus"]["reached"]
        assert s["consensus"]["converged_round"] is None  # reset by the membership change
        assert s["comm"]["hospitals"] == n + 1 and s["comm"]["links"] == n and s["comm"]["packets_per_round"] == 2 * n
        by_round = {r["round"]: r for r in s["comm"]["per_round"]}
        assert by_round[join_round - 1]["hospitals"] == n and by_round[join_round - 1]["messages"] == 2 * (n - 1)
        net = {p["round"]: p["hospitals"] for p in s["network_loss"]}
        assert net[join_round - 1] == n and net[join_round] == n + 1  # the weighted average now spans the joiner too

        wait_until(lambda: rounds_done(JOINER, run_id) >= 10, 60, what=f"{JOINER} trains")

        # -- planned departure of a middle hospital: its neighbours bridge
        subprocess.run(["docker", "exec", PREFIX + leaver, "python", "-m", "node.leave"], check=True,
                       capture_output=True, text=True)
        shrunk = [m for m in grown if m != leaver]
        left = wait_until(lambda: db_rows("SELECT round FROM audit_logs WHERE run_id = %s AND node_id = %s AND "
                                          "event = 'left_chain'", (run_id, leaver)), 60, what=f"{leaver} leaves")
        leave_round = left[0][0]

        def after_leave():
            s = snapshot()
            return s if (s["topology"]["chain"] == shrunk and s["comm"]["hospitals_this_round"] == n
                         and loss_rounds(s) >= {leave_round - 1, leave_round}) else None

        s = wait_until(after_leave, 60, what="the dashboard shows the bridged chain")
        assert leaver in s["topology"]["left"]
        assert s["consensus"]["edges"] == [f"{a}|{b}" for a, b in zip(shrunk, shrunk[1:])]  # a|b, b|c gone; a|c new
        assert s["consensus"]["epoch_start_round"] == leave_round and s["consensus"]["converged_round"] is None
        assert s["comm"]["hospitals"] == n and s["comm"]["links"] == n - 1
        net = {p["round"]: p["hospitals"] for p in s["network_loss"]}
        assert net[leave_round - 1] == n + 1 and net[leave_round] == n
        assert status(leaver)["run_state"] == "left" and not status(leaver)["in_chain"]

        # -- and training keeps converging, to consensus on the new chain
        wait_until(lambda: all(completed(m, run_id) for m in shrunk), 300, what="the bridged chain finishes")
        final = {m: status(m)["current_round"] for m in shrunk}
        assert len(set(final.values())) == 1 and max(final.values()) < 400, f"no consensus: {final}"
        s = snapshot()
        assert s["consensus"]["reached"] and s["consensus"]["converged_round"] > leave_round
        assert all(s["consensus"]["series"][-1][e] < EPSILON for e in s["consensus"]["edges"])
        assert s["network_loss"][-1]["hospitals"] == n
        bridge = db_rows("SELECT node_id, sender, payload_bytes FROM audit_logs WHERE run_id = %s AND "
                         "event = 'snapshot_resync' AND round = %s", (run_id, leave_round))
        a, c = start_chain[0], start_chain[2]
        assert {(n, snd) for n, snd, _ in bridge} >= {(a, c), (c, a)}  # full snapshot both ways over the new link
    finally:
        # back to the starting stack for whoever runs next: the joiner leaves, the leaver re-registers
        subprocess.run(["docker", "exec", PREFIX + JOINER, "python", "-m", "node.leave"], capture_output=True)
        compose("--profile", "join", "stop", JOINER)
        docker("restart", PREFIX + leaver)
        wait_until(all_placed, 120, what="the original chain is back")
