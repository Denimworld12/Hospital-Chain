"""Telemetry is pushed to the observer service (observer/), never awaited by training (node/writer.py)."""

import sqlite3
import threading
import time

import httpx

import pki
from chain_harness import NodeServer, RecordingDatabase, _Server, as_party, wait_until, write_configs
from node.main import create_app as create_node_app
from node.writer import DbWriter
from observer.app import ObserverSettings, create_app

TOKEN = "observer-test-token"


class Blocking:
    """A sink that holds its first batch until released, so the queue behind it fills up."""

    def __init__(self):
        self.release = threading.Event()
        self.batches: list[list] = []

    def write_batch(self, items):
        self.release.wait(10)
        self.batches.append([kw.get("round_num", name) for name, kw in items])


def test_a_full_queue_drops_the_oldest_and_never_blocks_the_caller(tmp_path):
    sink = Blocking()
    w = DbWriter(sink, maxsize=3, audit_db=tmp_path / "audit.db")
    w.start()
    w.audit(node_id="a", event="e", round_num=1)
    wait_until(lambda: not w._lane._q, 5, "the sender takes round 1 and blocks on it")
    t0 = time.monotonic()
    for r in range(2, 11):
        w.audit(node_id="a", event="e", round_num=r)
    assert time.monotonic() - t0 < 0.1, "enqueueing never waits for the sink"
    assert w.dropped == 6
    sink.release.set()
    wait_until(lambda: sum(len(b) for b in sink.batches) == 4, 5, "the rest is sent")
    assert sink.batches == [[1], [8, 9, 10]], "rounds 2-7 (the oldest) were dropped, the newest kept"
    w.heartbeat("a", None, "idle", 0)  # positional calls still map onto the Database signatures
    wait_until(lambda: sink.batches[-1] == ["heartbeat"], 5, "the heartbeat")
    # every audit() also lands in the local audit.db, which has its own queue
    wait_until(lambda: _rows(tmp_path / "audit.db") == 10, 5, "all ten audit rows, none dropped")
    w.stop()


def _rows(path) -> int:
    try:
        with sqlite3.connect(path) as c:
            return c.execute("SELECT count(*) FROM audit_logs").fetchone()[0]
    except sqlite3.Error:
        return 0


class ObserverServer(_Server):
    def __init__(self, port: int, pki_dir, db):
        s = ObserverSettings(OBSERVER_PORT=port, OBSERVER_TOKEN=TOKEN, TLS_CERT=str(pki_dir / "observer" / "tls.crt"),
                             TLS_KEY=str(pki_dir / "observer" / "tls.key"), TLS_CA=str(pki_dir / "ca.crt"))
        self.url = f"https://127.0.0.1:{port}"
        self._serve(create_app(s, database=db), port, s.tls_cert, s.tls_key, s.tls_ca)


def test_a_node_pushes_its_telemetry_to_the_observer_over_pinned_tls(tmp_path):
    cfgs = write_configs(tmp_path, ["hospital-a", "hospital-b"], engine="gossip", secure=True)
    pki_dir = tmp_path / "pki"
    pki.ensure_identity(pki_dir, "observer", ["127.0.0.1", "localhost"], credential=False)
    from chain_harness import free_ports

    (port,) = free_ports(1)
    received = RecordingDatabase()
    observer = ObserverServer(port, pki_dir, received).start()
    for cfg in cfgs.values():
        cfg.write_text(cfg.read_text() + f"observer: {{url: {observer.url}, token: {TOKEN}, "
                                         f"tls_pin: 'file:{pki_dir / 'observer' / 'tls.crt'}'}}\n")
    node = NodeServer(cfgs["hospital-a"])
    node.app = create_node_app(node.cfg)  # no recorder: the production sink, i.e. the observer
    node._serve(node.app, node.cfg.port, node.cfg.tls.cert, node.cfg.tls.key, node.cfg.tls.ca)
    node.start()
    try:
        wait_until(lambda: received.events("node_registered"), 15, "the observer gets the node's audit")
        assert received.events("node_registered")[0]["node_id"] == "hospital-a"
        # the node's own copy, on its own disk
        wait_until(lambda: _rows(tmp_path / "ckpt" / "audit.db") >= 1, 5, "local audit.db")
        # the ingest token is required; an unknown kind or a schema change is refused item by item
        url = f"{observer.url}/ingest"
        batch = {"node_id": "x", "items": [{"kind": "audit", "payload": {"node_id": "x", "event": "forged"}}]}
        ctx = as_party(pki_dir / "ca.crt", pki_dir / "hospital-b" / "tls.crt", pki_dir / "hospital-b" / "tls.key")
        assert httpx.post(url, json=batch, verify=ctx, timeout=5).status_code == 401
        assert httpx.post(url, json=batch, headers={"Authorization": "Bearer nope"}, verify=ctx, timeout=5).status_code == 401
        bad = {"node_id": "x", "items": [{"kind": "ensure_schema", "payload": {}},
                                         {"kind": "heartbeat", "payload": {"bogus": 1}}]}
        r = httpx.post(url, json=bad, headers={"Authorization": f"Bearer {TOKEN}"}, verify=ctx, timeout=5)
        assert r.status_code == 200 and r.json()["applied"] == 0 and len(r.json()["rejected"]) == 2
        assert not received.events("forged")
    finally:
        node.stop(abrupt=True)
        observer.stop(abrupt=True)
