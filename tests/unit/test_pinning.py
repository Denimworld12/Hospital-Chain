"""TLS by identity (node/pinning.py): both ends of every link check the peer's certificate key against the key
the directory has on record for that hospital; the directory's own key is pinned from the kit.

Every certificate here is genuinely issued by the network CA, so each refusal below is the pin
check at work, not ordinary certificate verification.
"""

import httpx
import pytest
from fastapi import FastAPI, Request

import pki
from chain_harness import AUTH, _Server, as_party, wait_until
from node import pinning, security, wire
from test_membership import HOSPITALS, Network, signed_update


@pytest.fixture
def net(tmp_path):
    n = Network(tmp_path)
    yield n
    n.stop()


def mallory(net) -> tuple[str, str, str]:
    """A certificate the network CA really issued, for a party the directory has never registered."""
    pki_dir = net.dir_cfg.parent / "pki"
    d = pki.ensure_identity(pki_dir, "mallory", ["127.0.0.1", "localhost"], credential=False)
    return str(pki_dir / "ca.crt"), str(d / "tls.crt"), str(d / "tls.key")


class Impostor(_Server):
    """Answers at a hospital's address with a valid CA-issued certificate that is not that hospital's key."""

    def __init__(self, port: int, ca: str, cert: str, key: str):
        app = FastAPI()
        self.hits: list[str] = []

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def anything(path: str, request: Request):
            self.hits.append(path)
            return {"accepted": True, "participating": True, "on_time": True}

        self.url = f"https://127.0.0.1:{port}"
        self._serve(app, port, cert, key, ca)


def test_a_server_with_the_wrong_key_is_refused_before_anything_is_sent(net):
    net.up("hospital-a", "hospital-b", "hospital-c")
    a, b = net.nodes["hospital-a"], net.nodes["hospital-b"]
    net.start("hospital-a")
    wait_until(lambda: a.runner.current_round >= 3, 60, "round 3")
    b.stop(abrupt=True)
    impostor = Impostor(b.cfg.port, *mallory(net)).start()  # b's address, a CA-valid certificate, the wrong key
    try:
        wait_until(lambda: a.db.events("tls_pin_mismatch"), 30, "a refuses the impostor")
        refusal = a.db.events("tls_pin_mismatch")[0]
        assert refusal["receiver"] == "hospital-b"
        assert "hospital-b's TLS key changed" in refusal["detail"]["reason"]
        assert "re-register it with the directory" in refusal["detail"]["reason"]
        wait_until(lambda: a.runner.neighbour_status.get("hospital-b") == "tls-key-mismatch", 10, "status shows why")
        assert impostor.hits == [], "not one request (no update, no snapshot) reached the impostor"
    finally:
        impostor.stop(abrupt=True)


def test_a_client_with_the_wrong_key_is_refused(net):
    net.up("hospital-a", "hospital-b", "hospital-c")
    a, b, c = (net.nodes[h] for h in HOSPITALS[:3])
    a_key = security.load_signing_key(a.cfg.signing_key)
    body = b"\x02\x00\x00\x00" + bytes(8)
    headers = signed_update(a_key, "hospital-a", "hospital-b", body)

    def post(verify):
        return httpx.post(f"{b.url}/update", content=body, headers=headers, verify=verify, timeout=5)

    assert post(a.identity()).status_code == 200  # a, with its own certificate and signature: accepted
    # a's valid signature, but sent over c's certificate: c cannot speak as a, even with a captured packet
    r = post(c.identity())
    assert r.status_code == 403
    assert "hospital-a's TLS key changed" in r.json()["detail"] and "re-register" in r.json()["detail"]
    r = post(as_party(*mallory(net)))  # a certificate from the right CA that nobody registered
    assert r.status_code == 403 and "TLS key changed" in r.json()["detail"]
    r = post(b.verify)  # no client certificate at all
    assert r.status_code == 401 and "must connect with its TLS certificate" in r.json()["detail"]
    # the same on the other endpoints that carry model state or chain addresses
    r = httpx.get(f"{b.url}/snapshot", params={"requester": "hospital-a"}, headers=AUTH, verify=c.identity(), timeout=5)
    assert r.status_code == 403
    r = httpx.get(f"{b.url}/membership", headers=AUTH | {wire.H_SENDER: "hospital-a"}, verify=c.identity(), timeout=5)
    assert r.status_code == 403
    r = httpx.get(f"{b.url}/membership", headers=AUTH | {wire.H_SENDER: "hospital-a"}, verify=a.identity(), timeout=5)
    assert r.status_code == 200 and r.json()["documents"]
    assert len(b.db.events("tls_pin_mismatch")) == 5


def test_an_unregistered_key_cannot_register_as_an_existing_hospital(net):
    net.up("hospital-a", "hospital-b", "hospital-c")
    before = net.directory.store.latest()
    b_pin = before.members["hospital-b"].tls_pin
    assert b_pin == pinning.cert_file_pin(net.nodes["hospital-b"].cfg.tls.cert)
    ca, cert, key = mallory(net)
    m_pin = pinning.cert_file_pin(cert)
    as_mallory = as_party(ca, cert, key)

    def register(pin, credential=None, verify=as_mallory):
        body = {"node_id": "hospital-b", "url": "https://127.0.0.1:1", "tls_spki_sha256": pin}
        auth = {"Authorization": f"Bearer {credential}"} if credential else {}
        return httpx.post(f"{net.directory.url}/register", json=body, headers=auth, verify=verify, timeout=5)

    cred = {h: net.nodes[h].cfg.directory.credential for h in HOSPITALS[:3]}
    assert register(m_pin).status_code == 401  # its own key, no credential
    assert register(m_pin, cred["hospital-c"]).status_code == 401  # another hospital's kit credential
    # b's published pin, even with b's credential: it cannot claim a key it does not hold
    r = register(b_pin, cred["hospital-b"])
    assert r.status_code == 403 and "not the key of the certificate" in r.json()["detail"]
    assert register(b_pin, cred["hospital-b"], verify=net.directory.verify).status_code == 401  # no certificate
    after = net.directory.store.latest()
    assert after.epoch == before.epoch and after.members["hospital-b"].tls_pin == b_pin
    assert [e["event"] for e in net.directory.store.events()].count("auth_failed") == 4


def test_pins_hold_while_the_directory_is_down(net):
    net.up("hospital-a", "hospital-b", "hospital-c")
    a = net.nodes["hospital-a"]
    net.start("hospital-a")
    wait_until(lambda: a.runner.current_round >= 3, 60, "round 3")
    net.directory.stop(abrupt=True)
    stopped_at = a.runner.current_round
    wait_until(lambda: a.runner.current_round >= stopped_at + 10, 90, "ten more rounds without the directory")
    later = [s for n in HOSPITALS[:3] for s in net.nodes[n].db.sent if s["round_num"] > stopped_at]
    assert later and {s["status"] for s in later} == {"delivered"}, "every link stays pinned and delivered"
    assert not any(net.nodes[n].db.events("tls_pin_mismatch") for n in HOSPITALS[:3])
    # the pins come from the signed documents the nodes hold, not from the (unreachable) directory
    expected = {h: pinning.cert_file_pin(net.nodes[h].cfg.tls.cert) for h in HOSPITALS[:3]}
    assert {p: pin for p, _, _, pin in a.runner.known_parties()} == expected
