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


class KeysDatabase(RecordingDatabase):
    """hospital_keys as the directory wrote them: one enrolled hospital with its own token."""

    def __init__(self, owners: dict[str, str], keys: dict[str, str] | None = None):
        super().__init__()
        self.owners, self.keys = owners, keys or {}
        self.proofs: list[dict] = []
        self.rejections: list[dict] = []

    def fetch(self, sql, params=None):
        if "observer_token_sha256" in sql:
            return [(self.owners[params[0]],)] if params[0] in self.owners else []
        if "signing_public_key" in sql:
            return [(self.keys[params[0]],)] if params[0] in self.keys else []
        return [(1,)] if params[0] in self.keys else []

    def proof_accepted(self, **kw):
        self.proofs.append(kw)

    def proof_rejected(self, **kw):
        self.rejections.append(kw)

    def heartbeat(self, node_id, run_id, run_state, current_round):
        self.audits.append({"event": "heartbeat", "node_id": node_id})


def test_an_enrolled_hospitals_own_token_writes_only_as_that_hospital(tmp_path):
    import hashlib

    from chain_harness import free_ports, issue_pki

    pki_dir = issue_pki(tmp_path, ["hospital-a"])
    pki.ensure_identity(pki_dir, "observer", ["127.0.0.1", "localhost"], credential=False)
    own = "node-1-own-token"
    db = KeysDatabase({hashlib.sha256(own.encode()).hexdigest(): "node-1"})
    (port,) = free_ports(1)
    observer = ObserverServer(port, pki_dir, db).start()
    try:
        url, ca = f"{observer.url}/ingest", str(pki_dir / "ca.crt")

        def post(token, node_id, items):
            return httpx.post(url, json={"node_id": node_id, "items": items}, verify=ca, timeout=5,
                              headers={"Authorization": f"Bearer {token}"})

        beat = lambda n: {"kind": "heartbeat", "payload": {"node_id": n, "run_id": None, "run_state": "idle",  # noqa: E731
                                                          "current_round": 0}}
        assert post(own, "node-1", [beat("node-1")]).json()["applied"] == 1
        assert post(own, "hospital-a", [beat("hospital-a")]).status_code == 403, "another hospital's batch"
        # in its own batch, only a row written as another hospital is refused, never the rest of the batch
        received = {"kind": "audit", "payload": {"node_id": "node-1", "event": "update_received", "sender": "hospital-a",
                                                 "receiver": "node-1"}}
        sent = {"kind": "update_sent", "payload": {"run_id": "r", "round_num": 1, "sender": "node-1",
                                                   "receiver": "hospital-a", "payload_bytes": 12,
                                                   "full_precision_bytes": 128, "bits": 2, "dtype": "qgadmm-packet",
                                                   "checksum": "c", "status": "delivered"}}
        forged = {**sent, "payload": {**sent["payload"], "sender": "hospital-a", "receiver": "node-1"}}
        r = post(own, "node-1", [received, beat("hospital-a"), sent, forged])
        assert r.status_code == 200 and r.json()["applied"] == 2, r.json()
        assert [x.split(":")[0] for x in r.json()["rejected"]] == ["1", "3"]
        assert db.events("update_received")[0]["sender"] == "hospital-a"
        assert post("not-a-token", "node-1", [beat("node-1")]).status_code == 401
        # the network token still works for kit hospitals; neither token can write the directory's key records
        assert post(TOKEN, "hospital-a", [beat("hospital-a")]).json()["applied"] == 1
        keys = {"kind": "hospital_key", "payload": {"node_id": "node-1", "display_name": "x",
                                                    "signing_public_key": None, "tls_pin": None}}
        assert post(own, "node-1", [keys]).json()["applied"] == 0, "a key record, even as itself"
        assert post(TOKEN, "hospital-a", [keys]).json()["applied"] == 0
    finally:
        observer.stop(abrupt=True)


def test_proof_of_origin_is_accepted_only_with_the_hospitals_key_on_record(tmp_path):
    import hashlib
    import json

    from chain_harness import free_ports, issue_pki
    from node import proof, security, wire

    pki_dir = issue_pki(tmp_path, ["hospital-a", "hospital-b"])
    pki.ensure_identity(pki_dir, "observer", ["127.0.0.1", "localhost"], credential=False)
    key_a = security.load_signing_key(pki_dir / "hospital-a" / "signing.key")
    key_b = security.load_signing_key(pki_dir / "hospital-b" / "signing.key")
    other = "node-1-own-token"
    db = KeysDatabase({hashlib.sha256(other.encode()).hexdigest(): "node-1"},
                      {"hospital-a": security.public_key_b64(key_a)})
    (port,) = free_ports(1)
    observer = ObserverServer(port, pki_dir, db).start()
    try:
        url, ca = f"{observer.url}/proof", str(pki_dir / "ca.crt")
        auth = {"Authorization": f"Bearer {TOKEN}"}

        def post(body, headers):
            return httpx.post(url, content=body, headers={**headers, **auth}, verify=ca, timeout=5)

        prover = proof.Prover("hospital-a", key_a, 303, "v1.1.0")
        # naming a hospital without its token (none, a wrong one, another hospital's) is refused, and
        # shows nothing against the hospital it names
        body, headers = prover.message({"hospital-b": 4.2})
        for token, status in ((None, 401), ("nope", 401), (other, 403)):
            spoof = {"Authorization": f"Bearer {token}"} if token else {}
            assert httpx.post(url, content=body, headers={**headers, **spoof}, verify=ca, timeout=5).status_code == status
        assert not db.rejections and not db.events("proof_rejected")
        r = post(body, headers)
        assert r.status_code == 200 and r.json()["verified"], r.text
        msg = db.proofs[-1]["message"]
        assert msg["data_rows"] == 303 and msg["neighbours_rtt_ms"] == {"hospital-b": 4.2} and msg["host"]["hostname"]
        assert post(body, headers).status_code == 403, "the same message twice is a replay"
        # a changed fact, a message signed by another hospital's key, a hospital with no key on record
        body, headers = prover.message({})
        assert post(body.replace(b'"data_rows": 303', b'"data_rows": 999'), headers).status_code == 403
        forged, fh = proof.Prover("hospital-a", key_b, 1, "x").message({})
        assert post(forged, fh).status_code == 403
        unknown, uh = proof.Prover("hospital-b", key_b, 1, "x").message({})
        assert post(unknown, uh).status_code == 403
        reasons = [a["detail"]["reason"] for a in db.events("proof_rejected")]
        assert len(reasons) == 4 and "replayed" in reasons[0] and "signature" in reasons[1] and "signature" in reasons[2]
        assert "no signing key is on record for hospital-b" in reasons[3]
        assert [r["node_id"] for r in db.rejections] == ["hospital-a"] * 3, "shown on the dashboard for a known hospital"
        stale = json.loads(prover.message({})[0])
        stale["sent_at"] -= 3600
        sb = json.dumps(stale).encode()
        sh = {wire.H_SENDER: "hospital-a"}
        sh[wire.H_SIGNATURE] = security.sign(key_a, sb, sh, "proof")
        assert "10 minutes" in post(sb, sh).json()["detail"]
    finally:
        observer.stop(abrupt=True)


def test_a_node_sends_signed_proof_of_origin_with_its_host_facts(tmp_path, monkeypatch):
    import node.main as node_main
    from chain_harness import free_ports
    from node import security

    monkeypatch.setattr(node_main, "PROOF_INTERVAL_S", 0.2)
    monkeypatch.setenv("MEDCHAIN_HOST_NAME", "ALEX-LAPTOP")
    monkeypatch.setenv("MEDCHAIN_HOST_OS", "Microsoft Windows 11 Home 10.0.22631")
    monkeypatch.setenv("MEDCHAIN_HOST_BOOTED_AT", str(time.time() - 7200))
    monkeypatch.setenv("MEDCHAIN_TAILSCALE_DEVICE", "alex-laptop.tail1234.ts.net")
    cfgs = write_configs(tmp_path, ["hospital-a", "hospital-b"], engine="gossip", secure=True)
    pki_dir = tmp_path / "pki"
    pki.ensure_identity(pki_dir, "observer", ["127.0.0.1", "localhost"], credential=False)
    keys = {n: security.public_key_b64(security.load_signing_key(pki_dir / n / "signing.key")) for n in cfgs}
    db = KeysDatabase({}, keys)
    (port,) = free_ports(1)
    observer = ObserverServer(port, pki_dir, db).start()
    nodes = []
    try:
        for cfg in cfgs.values():
            cfg.write_text(cfg.read_text() + f"observer: {{url: {observer.url}, token: {TOKEN}, "
                                             f"tls_pin: 'file:{pki_dir / 'observer' / 'tls.crt'}'}}\n")
        for cfg in cfgs.values():
            n = NodeServer(cfg)
            n.app = create_node_app(n.cfg)
            n._serve(n.app, n.cfg.port, n.cfg.tls.cert, n.cfg.tls.key, n.cfg.tls.ca)
            nodes.append(n.start())
        wait_until(lambda: any(p["node_id"] == "hospital-a" and p["message"]["neighbours_rtt_ms"].get("hospital-b")
                               for p in db.proofs), 15, "a proof from hospital-a with a measured RTT to hospital-b")
        msg = next(p["message"] for p in db.proofs if p["node_id"] == "hospital-a")
        assert msg["host"]["hostname"] == "ALEX-LAPTOP" and msg["host"]["tailscale_device"].startswith("alex-laptop")
        assert 7190 < msg["host"]["uptime_s"] < 7300 and msg["data_rows"] == 303
        assert not db.events("proof_rejected")
    finally:
        for n in nodes:
            n.stop(abrupt=True)
        observer.stop(abrupt=True)
