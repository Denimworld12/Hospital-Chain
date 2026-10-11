"""Join-code enrollment (node/enroll.py, directory/enroll.py) over real HTTPS, end to end.

A hospital enrolls with a one-time code instead of a kit: it makes its own keys, the
directory issues its certificate and node id, and the kit it writes runs as a node that
registers and joins the chain like any provisioned one.
"""

import hashlib
import time

import httpx
import pytest

import pki
from chain_harness import (ROOT, DirectoryServer, NodeServer, RecordingDatabase, directory_configs, free_ports,
                           wait_until)
from node import enroll
from node.pinning import cert_file_pin

ADMIN = "test-admin-token"


class HubDatabase(RecordingDatabase):
    def __init__(self):
        super().__init__()
        self.keys: dict[str, dict] = {}

    def hospital_forgotten(self, *, node_id):
        self.keys[node_id] = {**self.keys.get(node_id, {}), "observer_token_sha256": None, "enrolled": False}

    def hospital_key(self, **kw):
        self.keys[kw["node_id"]] = {**self.keys.get(kw["node_id"], {}),
                                    **{k: v for k, v in kw.items() if v is not None}}


class Hub:
    """A directory that takes join codes, beside one kit hospital (hospital-a)."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.dir_cfg, self.cfgs = directory_configs(tmp_path, ["hospital-a"], rho=5.0)
        self.pki = tmp_path / "pki"
        pki.ensure_identity(self.pki, "observer", ["127.0.0.1", "localhost"], credential=False)
        self.dir_cfg.write_text(self.dir_cfg.read_text() + f"""admin_token_file: {tmp_path / 'hub' / 'admin_token'}
enrollment:
  ca_cert: {self.pki / 'ca.crt'}
  ca_key: {self.pki / 'ca.key'}
  observer_cert: {self.pki / 'observer' / 'tls.crt'}
  observer_port: 8200
  slices_dir: {ROOT / 'data' / 'slices'}
  node_template: {ROOT / 'config' / 'hospital-a.yaml'}
""")
        (tmp_path / "hub").mkdir()
        (tmp_path / "hub" / "admin_token").write_text(ADMIN + "\n")
        monkeypatch.setenv("MEDCHAIN_SHARED_SECRET", "chain-test-secret")
        self.db = HubDatabase()
        self.directory = DirectoryServer(self.dir_cfg, database=self.db).start()
        self.hub = f"127.0.0.1:{self.directory.cfg.port}"
        self.nodes: list[NodeServer] = []

    def admin(self, method: str, path: str, token: str = ADMIN, **kw) -> httpx.Response:
        return httpx.request(method, f"{self.directory.url}{path}", headers={"Authorization": f"Bearer {token}"},
                             verify=str(self.pki / "ca.crt"), timeout=10, **kw)

    def code(self, **kw) -> str:
        r = self.admin("POST", "/admin/join-codes", json={"created_by": "admin", **kw})
        assert r.status_code == 200, r.text
        return r.json()["code"]

    def stop(self):
        for s in [*self.nodes, self.directory]:
            s.stop(abrupt=True)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    h = Hub(tmp_path, monkeypatch)
    yield h
    h.stop()


def kit_env(monkeypatch, kit):
    """Run an enrolled kit outside Docker: deploy/node.yml's container paths, mapped onto the kit (like make node-native)."""
    env = {k: v for k, v in (line.split("=", 1) for line in (kit / ".env").read_text().splitlines()
                             if line and not line.startswith("#"))}
    node_id = env["NODE_ID"]
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k, v in {"NODE_CONFIG": kit / "config" / "node.yaml", "DATA_PATH": kit / "data",
                 "TLS_CERT": kit / "pki" / node_id / "tls.crt", "TLS_KEY": kit / "pki" / node_id / "tls.key",
                 "TLS_CA": kit / "pki" / "ca.crt", "SIGNING_KEY": kit / "pki" / node_id / "signing.key",
                 "DIRECTORY_CREDENTIAL_FILE": kit / "pki" / node_id / "credential",
                 "DIRECTORY_PUBLIC_KEY": f"file:{kit / 'pki' / 'directory' / 'signing.pub'}",
                 "DIRECTORY_TLS_PIN": f"file:{kit / 'pki' / 'directory' / 'tls.crt'}",
                 "SHARED_SECRET_FILE": kit / "pki" / "network" / "shared_secret",
                 "OBSERVER_URL": ""}.items():
        monkeypatch.setenv(k, str(v))
    return node_id


def test_codes_are_normalized_and_only_their_hash_identifies_them():
    code = enroll.new_code()
    assert len(code) == 24 and code.count("-") == 4
    messy = " " + code.lower().replace("-", " ") + " "
    assert enroll.normalize_code(messy) == code.replace("-", "")
    assert enroll.code_id(messy) == code[:4] and enroll.code_key(messy) == enroll.code_key(code)
    for bad in ("ABCD-EFGH", code[:-1] + "0", code[:-1] + "O"):
        with pytest.raises(ValueError):
            enroll.normalize_code(bad)
    body = {"a": 1, "b": "x"}
    tag = enroll.mac(enroll.code_key(code), "response", body)
    assert enroll.check_mac(enroll.code_key(code), "response", body, tag)
    assert not enroll.check_mac(enroll.code_key(code), "response", {**body, "b": "y"}, tag), "a changed answer"
    assert not enroll.check_mac(enroll.code_key(code), "request", body, tag), "the other direction"
    assert not enroll.check_mac(enroll.code_key(enroll.new_code()), "response", body, tag), "another code"


def test_a_hospital_enrolls_with_a_join_code_and_joins_the_chain(hub, tmp_path, monkeypatch):
    code = hub.code(data_slice="hospital-d.csv")
    stored = hub.directory.store.code(enroll.code_id(code))
    assert stored[1] == enroll.code_key(code) and code not in str(stored), "only the code's hash is stored"
    (port,) = free_ports(1)
    kit = tmp_path / "kit"
    assert enroll.main(["--hub", hub.hub, "--name", "  St  Mary's   Leeds ", "--code", code, "--advertise", "127.0.0.1",
                        "--port", str(port), "--out", str(kit), "--host-name", "ALEX-LAPTOP",
                        "--host-os", "Microsoft Windows 11 Home  10.0.22631", "--tailscale-device", "alex-laptop"]) == 0
    env = (kit / ".env").read_text()
    assert "MEDCHAIN_HOST_NAME=ALEX-LAPTOP\n" in env and "MEDCHAIN_HOST_OS=Microsoft Windows 11 Home 10.0.22631\n" in env
    assert "MEDCHAIN_HOST_BOOTED_AT" not in env
    hub.nodes.append(NodeServer(hub.cfgs["hospital-a"]).start())  # a kit hospital, before the kit's env is set

    node_id = kit_env(monkeypatch, kit)
    record = hub.directory.store.enrolled()[0]
    assert (record["node_id"], record["display_name"], record["data_slice"]) == (node_id, "St Mary's Leeds",
                                                                                  "hospital-d.csv")
    assert node_id.startswith("st-mary-s-leeds-") and len(node_id) == len("st-mary-s-leeds-") + 4
    # its own keys: the hub has only the public halves and hashes
    assert record["tls_pin"] == cert_file_pin(kit / "pki" / node_id / "tls.crt")
    assert record["signing_public_key"] == (kit / "pki" / node_id / "signing.pub").read_text().strip()
    credential = (kit / "pki" / node_id / "credential").read_text().strip()
    assert record["credential_sha256"] == hashlib.sha256(credential.encode()).hexdigest()
    token = (kit / "pki" / "observer" / "token").read_text().strip()
    assert record["observer_token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert hub.db.keys[node_id]["observer_token_sha256"] == record["observer_token_sha256"]
    # the kit pins what the hub has, and is laid out like a provisioned one
    assert cert_file_pin(kit / "pki" / "directory" / "tls.crt") == cert_file_pin(hub.pki / "directory" / "tls.crt")
    assert (kit / "pki" / "observer" / "tls.crt").read_text() == (hub.pki / "observer" / "tls.crt").read_text()
    assert (kit / "data" / f"{node_id}.csv").read_text() == (ROOT / "data" / "slices" / "hospital-d.csv").read_text()
    assert (kit / "deploy" / "node.yml").is_file() and "SHARED_SECRET=" not in (kit / ".env").read_text()
    assert (kit / "pki" / "network" / "shared_secret").read_text().strip() == "chain-test-secret"
    wait_until(lambda: hub.db.events("enrolled"), 5, "the enrollment in the audit log")

    # it runs as a node: registers with its credential and keys, and joins the kit hospital's chain; registering
    # writes its own observer token's hash again, so a lost /enroll write (or a reset Postgres) heals itself
    hub.db.keys.pop(node_id)
    node = NodeServer(kit / "config" / "node.yaml").start()
    hub.nodes.append(node)
    wait_until(lambda: node.status()["chain"] == ["hospital-a", node_id], 20, "both hospitals in the chain")
    wait_until(lambda: hub.db.keys.get(node_id, {}).get("observer_token_sha256") == record["observer_token_sha256"],
               5, "the token hash re-synced at registration")
    assert hub.db.keys[node_id]["enrolled"] is True
    assert node.status()["display_name"] == "St Mary's Leeds"
    assert node.status()["patients"] == 62  # hospital-d.csv

    # running it again changes nothing
    assert enroll.main(["--hub", hub.hub, "--name", "x", "--code", code, "--advertise", "127.0.0.1",
                        "--out", str(kit)]) == 0
    assert len(hub.directory.store.enrolled()) == 1


def test_a_join_code_works_once_and_every_refusal_is_audited(hub):
    code = hub.code()
    enroll.request_enrollment(hub.hub, "Hospital One", code, ["127.0.0.1"], enroll.Keys.generate())
    with pytest.raises(enroll.EnrollError, match="has been used"):
        enroll.request_enrollment(hub.hub, "Hospital Two", code, ["127.0.0.1"], enroll.Keys.generate())

    revoked = hub.code()
    assert hub.admin("POST", f"/admin/join-codes/{enroll.code_id(revoked)}/revoke", json={}).status_code == 200
    with pytest.raises(enroll.EnrollError, match="has been revoked"):
        enroll.request_enrollment(hub.hub, "Hospital Three", revoked, ["127.0.0.1"], enroll.Keys.generate())

    expired = hub.code(ttl_minutes=1)
    with hub.directory.store._lock:
        hub.directory.store._conn.execute("UPDATE join_codes SET expires_at = ? WHERE code_id = ?",
                                          (time.time() - 1, enroll.code_id(expired)))
    with pytest.raises(enroll.EnrollError, match="has expired"):
        enroll.request_enrollment(hub.hub, "Hospital Four", expired, ["127.0.0.1"], enroll.Keys.generate())

    with pytest.raises(enroll.EnrollError, match="unknown join code"):
        enroll.request_enrollment(hub.hub, "Hospital Five", enroll.new_code(), ["127.0.0.1"], enroll.Keys.generate())

    # the right id with the wrong secret: refused, and five of those revoke the code
    target = hub.code()
    wrong = target[:5] + ("B" if target[5] != "B" else "C") + target[6:]
    for i in range(5):
        with pytest.raises(enroll.EnrollError, match="join code is wrong"):
            enroll.request_enrollment(hub.hub, "Guesser", wrong, ["127.0.0.1"], enroll.Keys.generate())
    with pytest.raises(enroll.EnrollError, match="has been revoked"):
        enroll.request_enrollment(hub.hub, "Hospital Six", target, ["127.0.0.1"], enroll.Keys.generate())

    states = {c["code_id"]: c["state"] for c in hub.admin("GET", "/admin/join-codes").json()["codes"]}
    assert states[enroll.code_id(code)] == "used" and states[enroll.code_id(target)] == "revoked"
    wait_until(lambda: len(hub.db.events("enroll_rejected")) == 10, 5, "every refusal in the audit log")
    assert {a["detail"]["display_name"] for a in hub.db.events("enroll_rejected")} >= {"Hospital Two", "Guesser"}
    assert len(hub.db.events("join_code_created")) == 4 and len(hub.db.events("join_code_revoked")) == 1


def test_the_admin_api_needs_the_hubs_admin_token(hub):
    assert hub.admin("POST", "/admin/join-codes", token="nope", json={}).status_code == 401
    assert httpx.get(f"{hub.directory.url}/admin/join-codes", verify=str(hub.pki / "ca.crt")).status_code == 401
    listing = hub.admin("GET", "/admin/join-codes").json()
    assert "hospital-a.csv" in listing["slices"] and listing["ttl_minutes"] == 30
    assert hub.admin("POST", "/admin/join-codes", json={"data_slice": "../config/directory.yaml"}).status_code == 400


def test_an_enrolled_hospital_must_register_with_the_keys_it_enrolled_with(hub, tmp_path, monkeypatch):
    kit = tmp_path / "kit"
    (port,) = free_ports(1)
    assert enroll.main(["--hub", hub.hub, "--name", "Rekeyed", "--code", hub.code(), "--advertise", "127.0.0.1",
                        "--port", str(port), "--out", str(kit)]) == 0
    node_id = kit_env(monkeypatch, kit)
    # someone with the credential but another signing key
    pki.ensure_identity(tmp_path / "other", "x", ["127.0.0.1"], credential=False)
    monkeypatch.setenv("SIGNING_KEY", str(tmp_path / "other" / "x" / "signing.key"))
    node = NodeServer(kit / "config" / "node.yaml").start()
    hub.nodes.append(node)
    wait_until(lambda: any(e["event"] == "auth_failed" and e["node_id"] == node_id
                           for e in hub.directory.store.events()), 10, "the registration refused")
    assert not node.status()["in_chain"]


def test_the_hub_must_prove_it_issued_the_code(hub, monkeypatch):
    """An answer that does not carry the code's HMAC (someone else answering at the hub's address) is refused."""
    real = enroll.post_enroll

    def tampered(host, port, body, timeout):
        status, answer, pin = real(host, port, body, timeout)
        answer["observer"]["port"] = 9999
        return status, answer, pin

    monkeypatch.setattr(enroll, "post_enroll", tampered)
    with pytest.raises(enroll.EnrollError, match="not from the hub"):
        enroll.request_enrollment(hub.hub, "Hospital", hub.code(), ["127.0.0.1"], enroll.Keys.generate())


def test_display_names_are_free_text_but_never_shared_and_ids_stay_stable(hub, tmp_path, monkeypatch):
    from directory.enroll import name_key, slug

    assert slug("  Hôpital Saint-Éloi (Paris) ") == "hopital-saint-eloi-p"
    assert slug("医院") == "hospital" and name_key("St  MARY'S") == name_key("st mary's")
    hub.nodes.append(NodeServer(hub.cfgs["hospital-a"]).start())  # no display_name in its config: "hospital-a"
    wait_until(lambda: hub.nodes[0].status()["in_chain"], 20, "hospital-a registered")

    code = hub.code()
    with pytest.raises(enroll.EnrollError, match=r"\(hospital-a\) is already called 'HOSPITAL-A'"):
        enroll.request_enrollment(hub.hub, "HOSPITAL-A", code, ["127.0.0.1"], enroll.Keys.generate())
    first = enroll.request_enrollment(hub.hub, "Leeds General", code, ["127.0.0.1"], enroll.Keys.generate())
    with pytest.raises(enroll.EnrollError, match="already called"):
        enroll.request_enrollment(hub.hub, "LEEDS general", hub.code(), ["127.0.0.1"], enroll.Keys.generate())
    second = enroll.request_enrollment(hub.hub, "Leeds General 2", hub.code(), ["127.0.0.1"], enroll.Keys.generate())
    assert first["node_id"] != second["node_id"] and first["node_id"].startswith("leeds-general-")

    # the name it enrolled with is the one the chain shows, whatever its node's config says
    kit = tmp_path / "kit"
    (port,) = free_ports(1)
    assert enroll.main(["--hub", hub.hub, "--name", "Royal Infirmary", "--code", hub.code(), "--advertise",
                        "127.0.0.1", "--port", str(port), "--out", str(kit)]) == 0
    node_id = kit_env(monkeypatch, kit)
    config = kit / "config" / "node.yaml"
    config.write_text(config.read_text().replace("display_name: Royal Infirmary", "display_name: hospital-a"))
    node = NodeServer(config).start()
    hub.nodes.append(node)
    wait_until(lambda: node_id in hub.nodes[0].status()["chain"], 20, "the enrolled node in the chain")
    assert hub.directory.store.latest().members[node_id].display_name == "Royal Infirmary"
    wait_until(lambda: hub.db.keys.get(node_id, {}).get("display_name") == "Royal Infirmary", 5, "its key record")


def test_forgetting_an_abandoned_enrollment_frees_its_name(hub):
    """An install that enrolled but never started still holds its name until the organiser forgets it."""
    ghost = enroll.request_enrollment(hub.hub, "St Mary's Leeds", hub.code(), ["127.0.0.1"], enroll.Keys.generate())
    with pytest.raises(enroll.EnrollError, match="already called"):
        enroll.request_enrollment(hub.hub, "St Mary's Leeds", hub.code(), ["127.0.0.1"], enroll.Keys.generate())
    assert hub.admin("POST", f"/admin/members/{ghost['node_id']}/remove", token="nope", json={}).status_code == 401
    out = hub.admin("POST", f"/admin/members/{ghost['node_id']}/remove", json={"removed_by": "admin"}).json()
    assert (out["removed_from_chain"], out["forgotten"]) == (False, True)
    wait_until(lambda: hub.db.keys.get(ghost["node_id"], {}).get("enrolled") is False, 5, "its token revoked")
    assert hub.db.keys[ghost["node_id"]]["observer_token_sha256"] is None
    again = enroll.request_enrollment(hub.hub, "St Mary's Leeds", hub.code(), ["127.0.0.1"], enroll.Keys.generate())
    assert again["node_id"] != ghost["node_id"]
    assert hub.admin("POST", "/admin/members/no-such-hospital/remove", json={}).status_code == 404
    wait_until(lambda: hub.db.events("member_removed"), 5, "the removal in the audit log")


def test_the_organiser_removes_a_hospital_that_is_gone_and_its_neighbours_bridge(tmp_path):
    dir_cfg, cfgs = directory_configs(tmp_path, ["hospital-a", "hospital-b", "hospital-c"], engine="gossip")
    dir_cfg.write_text(dir_cfg.read_text() + f"admin_token_file: {tmp_path / 'admin_token'}\n")
    (tmp_path / "admin_token").write_text(ADMIN + "\n")
    directory = DirectoryServer(dir_cfg).start()
    nodes = {n: NodeServer(cfgs[n]).start() for n in cfgs}
    try:
        wait_until(lambda: nodes["hospital-a"].status()["chain"] == ["hospital-a", "hospital-b", "hospital-c"], 20,
                   "all three placed")
        nodes.pop("hospital-b").stop(abrupt=True)  # its laptop goes off: it can never call /leave
        r = httpx.post(f"{directory.url}/admin/members/hospital-b/remove", json={"removed_by": "admin"},
                       headers={"Authorization": f"Bearer {ADMIN}"}, verify=directory.verify, timeout=15)
        assert r.status_code == 200 and r.json()["removed_from_chain"] and not r.json()["forgotten"], r.text
        assert r.json()["told"]["hospital-b"].startswith("unreachable")
        wait_until(lambda: nodes["hospital-a"].status()["chain"] == ["hospital-a", "hospital-c"] ==
                   nodes["hospital-c"].status()["chain"], 20, "a and c bridged")
        # a chain cannot shrink below two
        r = httpx.post(f"{directory.url}/admin/members/hospital-c/remove", json={},
                       headers={"Authorization": f"Bearer {ADMIN}"}, verify=directory.verify, timeout=15)
        assert r.status_code == 409 and "at least 2" in r.json()["detail"]
    finally:
        for n in [*nodes.values(), directory]:
            n.stop(abrupt=True)


def test_a_hub_made_before_join_codes_says_how_to_upgrade(tmp_path):
    dir_cfg, _ = directory_configs(tmp_path, ["hospital-a"])
    dir_cfg.write_text(dir_cfg.read_text() + f"admin_token_file: {tmp_path / 'admin_token'}\n")
    (tmp_path / "admin_token").write_text(ADMIN + "\n")
    directory = DirectoryServer(dir_cfg).start()
    try:
        r = httpx.get(f"{directory.url}/admin/join-codes", headers={"Authorization": f"Bearer {ADMIN}"},
                      verify=directory.verify, timeout=10)
        assert r.status_code == 503
        assert "made before join codes" in r.json()["detail"] and "provision.py" in r.json()["detail"]
    finally:
        directory.stop(abrupt=True)
