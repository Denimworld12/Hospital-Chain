"""Dynamic membership over real HTTPS: a directory, signed updates, joins, planned leaves.

Every node here is the production app with a TLS certificate from a throwaway CA and an
Ed25519 signing key; its chain comes only from the directory (directory/), never from
its YAML. See chain_harness.directory_configs.
"""

import httpx
import numpy as np
import pytest

from chain_harness import AUTH, DirectoryServer, NodeServer, directory_configs, wait_until
from node import security, wire
from node.membership import Membership
from node.qgadmm.state import HEAD, TAIL

HOSPITALS = ["hospital-a", "hospital-b", "hospital-c", "hospital-d"]
RHO = 5.0


class Network:
    def __init__(self, tmp_path, hospitals=HOSPITALS, **kw):
        self.dir_cfg, self.cfgs = directory_configs(tmp_path, hospitals, rho=RHO, **kw)
        self.directory = DirectoryServer(self.dir_cfg).start()
        self.nodes: dict[str, NodeServer] = {}

    def up(self, *names):
        for n in names:
            self.nodes[n] = NodeServer(self.cfgs[n]).start()
        wait_until(lambda: all(self.nodes[n].status()["in_chain"] for n in names), 20, f"{names} registered")
        return self

    def chain(self, node=None):
        return self.nodes[node or next(iter(self.nodes))].status()["chain"]

    def start(self, via, rounds=400) -> str:
        r = self.nodes[via].post("/round/start", json={"rounds": rounds}).json()
        assert r["status"] == "started", r
        return r["run_id"]

    def members(self):
        return [self.nodes[n] for n in self.chain()]

    def completed(self, names):
        return all(self.nodes[n].status()["run_state"] == "completed" for n in names)

    def stop(self):
        # abrupt: a graceful stop waits out asyncio's 30 s TLS close-notify on every idle keep-alive connection
        for s in [*self.nodes.values(), self.directory]:
            s.stop(abrupt=True)


@pytest.fixture
def net(tmp_path):
    n = Network(tmp_path)
    yield n
    n.stop()


def assert_links_in_sync(nodes: dict[str, NodeServer], chain):
    for a, b in zip(chain, chain[1:]):
        ea, eb = nodes[a].engine.state, nodes[b].engine.state
        assert set(ea.lambdas) >= {b} and set(eb.lambdas) >= {a}, (a, b)
        assert np.array_equal(ea.lambdas[b], eb.lambdas[a]), f"lambda of {a}|{b} differs at its two ends"
        assert np.array_equal(ea.theta_hat, eb.mirrors[a]) and np.array_equal(eb.theta_hat, ea.mirrors[b])


def assert_roles(nodes, chain):
    for i, m in enumerate(chain, start=1):
        st = nodes[m].status()
        assert (st["position"], st["group"]) == (i, HEAD if i % 2 else TAIL), (m, st["position"], st["group"])


def test_directory_places_hospitals_in_configured_order_and_needs_their_credential(net):
    # registration order is d, b, a, c; with no run in progress the directory keeps its configured order
    net.up("hospital-d", "hospital-b", "hospital-a", "hospital-c")
    wait_until(lambda: all(len(s.status()["chain"]) == 4 for s in net.nodes.values()), 10, "everyone on epoch 4")
    assert {tuple(s.status()["chain"]) for s in net.nodes.values()} == {tuple(HOSPITALS)}
    assert_roles(net.nodes, HOSPITALS)
    url = net.directory.url
    v = net.directory.verify
    body = {"node_id": "hospital-a", "url": "https://127.0.0.1:1"}
    assert httpx.post(f"{url}/register", json=body, verify=v).status_code == 401  # no credential
    assert httpx.post(f"{url}/register", json=body, headers={"Authorization": "Bearer wrong"}, verify=v).status_code == 401
    cred_b = net.nodes["hospital-b"].cfg.directory.credential
    stranger = {"node_id": "hospital-z", "url": "https://127.0.0.1:1"}
    assert httpx.post(f"{url}/register", json=stranger, headers={"Authorization": f"Bearer {cred_b}"},
                      verify=v).status_code == 403
    # b's credential does not let anyone register as a
    assert httpx.post(f"{url}/register", json=body, headers={"Authorization": f"Bearer {cred_b}"},
                      verify=v).status_code == 401
    events = [e["event"] for e in net.directory.store.events()]
    assert events.count("auth_failed") == 4 and events.count("joined") == 4


def test_join_mid_run_appends_at_the_end_and_converges(net):
    net.up("hospital-a", "hospital-b", "hospital-c")
    assert net.chain() == HOSPITALS[:3]
    run_id = net.start("hospital-a")
    wait_until(lambda: net.nodes["hospital-c"].runner.current_round >= 8, 60, "round 8")

    net.up("hospital-d")  # registers while the run is going: appended, at a round boundary ahead
    d = net.nodes["hospital-d"]
    doc = d.runner.membership
    assert doc.chain == tuple(HOSPITALS) and doc.run_id == run_id and doc.effective_round > 8
    joined = wait_until(lambda: d.db.events("joined_run"), 20, "d joined the run")[0]
    assert joined["round_num"] == doc.effective_round and joined["run_id"] == run_id
    wait_until(lambda: net.completed(HOSPITALS), 240, "run completes with four hospitals", 0.3)

    final = {s.status()["current_round"] for s in net.nodes.values()}
    assert len(final) == 1 and final.pop() < 400, "four hospitals reach consensus before the round limit"
    assert_roles(net.nodes, HOSPITALS)
    assert_links_in_sync(net.nodes, HOSPITALS)
    # the change landed on the same boundary everywhere; a and b kept their roles (append-only)
    for n in HOSPITALS[:3]:
        changed = [a for a in net.nodes[n].db.events("membership_changed") if a["round_num"]]
        assert [a["round_num"] for a in changed] == [doc.effective_round], n
    assert changed[0]["detail"]["added_links"] == ["hospital-c|hospital-d"]
    # the new link: lambda started at 0 and a full snapshot crossed both ways before any quantized packet
    for x, y in (("hospital-c", "hospital-d"), ("hospital-d", "hospital-c")):
        snaps = [a for a in net.nodes[x].db.events("snapshot_resync") if a["sender"] == y]
        assert snaps and snaps[0]["round_num"] == doc.effective_round and "new link" in snaps[0]["detail"]["reason"]
    first_d = min(m["round_num"] for m in d.db.metrics)
    assert first_d == doc.effective_round
    cd_sent = [s for s in net.nodes["hospital-c"].db.sent if s["receiver"] == "hospital-d"]
    assert min(s["round_num"] for s in cd_sent) >= doc.effective_round
    # d warm-started and then converged with the others: its edge residual ends below epsilon
    last = max(d.db.metrics, key=lambda m: m["round_num"])
    assert last["residuals"]["edges"]["hospital-c|hospital-d"] < 1e-3
    assert last["epoch"] == doc.epoch and last["chain_size"] == 4
    detected = [a for s in net.nodes.values() for a in s.db.events("consensus_detected")]
    assert detected and all(a["detail"]["epoch"] == doc.epoch for a in detected), "consensus is judged on the new chain"


def test_planned_leave_of_a_middle_hospital_bridges_its_neighbours(net):
    net.up(*HOSPITALS)
    run_id = net.start("hospital-a")
    wait_until(lambda: net.nodes["hospital-d"].runner.current_round >= 8, 60, "round 8")

    out = net.nodes["hospital-b"].post("/membership/leave").json()
    assert out["chain"] == ["hospital-a", "hospital-c", "hospital-d"] and out["effective_round"] > 8
    assert set(out["told_neighbours"]) == {"hospital-a", "hospital-c"}
    at = out["effective_round"]
    rest = ["hospital-a", "hospital-c", "hospital-d"]
    wait_until(lambda: net.completed(rest), 240, "the remaining three finish", 0.3)
    b = net.nodes["hospital-b"]
    assert b.status()["run_state"] == "left" and not b.status()["in_chain"]
    assert max(m["round_num"] for m in b.db.metrics) == at - 1, "b trains up to the boundary, then leaves"
    assert b.db.events("left_chain")

    final = {net.nodes[n].status()["current_round"] for n in rest}
    assert len(final) == 1 and final.pop() < 400, "the bridged chain reaches consensus"
    # a and c bridged with a brand-new link; c and d flipped roles (c head -> tail, d tail -> head)
    assert_roles(net.nodes, rest)
    assert_links_in_sync(net.nodes, rest)
    a_change = [x for x in net.nodes["hospital-a"].db.events("membership_changed") if x["round_num"]][0]
    assert a_change["round_num"] == at
    assert a_change["detail"]["added_links"] == ["hospital-a|hospital-c"]
    assert a_change["detail"]["removed_links"] == ["hospital-a|hospital-b"]
    groups = {r: m["residuals"]["group"] for m in net.nodes["hospital-c"].db.metrics for r in [m["round_num"]]}
    assert groups[at - 1] == HEAD and groups[at] == TAIL
    assert "hospital-b" not in net.nodes["hospital-a"].engine.state.lambdas  # the removed link's lambda is gone
    # the bridge's first exchange: both ends fetched the other's full snapshot at the boundary
    for x, y in (("hospital-a", "hospital-c"), ("hospital-c", "hospital-a")):
        snaps = [s for s in net.nodes[x].db.events("snapshot_resync") if s["sender"] == y]
        assert snaps and snaps[0]["round_num"] == at
    last = max(net.nodes["hospital-a"].db.metrics, key=lambda m: m["round_num"])
    assert last["residuals"]["edges"]["hospital-a|hospital-c"] < 1e-3 and last["chain_size"] == 3
    assert run_id == net.nodes["hospital-a"].status()["run_id"]


def test_unplanned_crash_is_not_a_departure(net):
    """A hospital that dies without notice stays in the chain: neighbours skip it (stale mirror), no bridging."""
    net.up("hospital-a", "hospital-b", "hospital-c")
    net.start("hospital-a", rounds=40)
    wait_until(lambda: net.nodes["hospital-b"].runner.current_round >= 5, 60, "round 5")
    epoch = net.nodes["hospital-a"].status()["epoch"]
    net.nodes["hospital-b"].stop(abrupt=True)
    wait_until(lambda: net.nodes["hospital-a"].db.events("round_degraded"), 30, "a notices b is gone")
    assert net.nodes["hospital-a"].status()["chain"] == HOSPITALS[:3]
    assert net.nodes["hospital-a"].status()["epoch"] == epoch, "a crash changes no membership"
    assert [e for e in net.directory.store.events() if e["event"] == "left"] == []


def test_the_directory_going_down_does_not_stop_training(net):
    net.up("hospital-a", "hospital-b", "hospital-c")
    net.start("hospital-a")
    wait_until(lambda: net.nodes["hospital-b"].runner.current_round >= 3, 60, "round 3")
    net.directory.stop(abrupt=True)
    wait_until(lambda: net.completed(HOSPITALS[:3]), 240, "run completes without the directory", 0.3)
    for n in HOSPITALS[:3]:
        hist = net.nodes[n].metrics()["history"]
        assert not any(h["degraded"] for h in hist) and hist[-1]["max_residual"] < 1e-3
    # ...but joining needs it: a new hospital keeps retrying and is not placed
    with pytest.raises(AssertionError, match="timed out"):
        net.up("hospital-d")
    assert not net.nodes["hospital-d"].status()["in_chain"]


# -- signed updates over TLS ----------------------------------------------------------


def signed_update(sender_key, sender, receiver, body, **extra):
    headers = {wire.H_RUN: "r", wire.H_ROUND: "1", wire.H_SENDER: sender, wire.H_RECEIVER: receiver,
               wire.H_EPOCH: "1", wire.H_CHECKSUM: wire.checksum(body), wire.H_DTYPE: "qgadmm-packet",
               wire.H_SEQ: "1", wire.H_BASE: "", wire.H_PRODUCES: "", **extra}
    headers[wire.H_SIGNATURE] = security.sign(sender_key, body, headers, "update")
    return headers | AUTH


def test_tampered_or_unsigned_or_unknown_updates_are_rejected(net, tmp_path):
    net.up("hospital-a", "hospital-b", "hospital-c")
    b, a = net.nodes["hospital-b"], net.nodes["hospital-a"]
    a_key = security.load_signing_key(a.cfg.signing_key)
    body = b"\x02\x00\x00\x00" + bytes(8)

    def post(content, headers):  # over a connection that presents a's certificate, as a really does
        return httpx.post(f"{b.url}/update", content=content, headers=headers, verify=a.identity(), timeout=5)

    ok = post(body, signed_update(a_key, "hospital-a", "hospital-b", body))
    assert ok.status_code == 200, ok.text  # a correctly signed packet from a real neighbour is accepted
    # tampering: flip a byte and fix up the checksum, as an attacker on the wire could
    bad = bytearray(body)
    bad[-1] ^= 1
    h = signed_update(a_key, "hospital-a", "hospital-b", body)
    h[wire.H_CHECKSUM] = wire.checksum(bytes(bad))
    r = post(bytes(bad), h)
    assert r.status_code == 401 and "signature" in r.json()["detail"]
    # a header that steers training (the stop proposal) cannot be injected either
    h = signed_update(a_key, "hospital-a", "hospital-b", body)
    r = post(body, h | {wire.H_STOP: "2"})
    assert r.status_code == 401
    # unsigned
    h = signed_update(a_key, "hospital-a", "hospital-b", body)
    del h[wire.H_SIGNATURE]
    assert post(body, h).status_code == 401
    # signed by someone else's key, claiming to be hospital-a
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    r = post(body, signed_update(Ed25519PrivateKey.generate(), "hospital-a", "hospital-b", body))
    assert r.status_code == 401
    # an unrecognized sender (not a member, no key on record)
    r = post(body, signed_update(Ed25519PrivateKey.generate(), "hospital-z", "hospital-b", body))
    assert r.status_code == 403
    # a packet a really signed for c, replayed to b
    r = post(body, signed_update(a_key, "hospital-a", "hospital-c", body))
    assert r.status_code == 403
    reasons = [x["detail"]["reason"] for x in b.db.events("update_rejected")]
    assert len(reasons) == 6 and any("signature" in x for x in reasons)
    # plain HTTP is not served at all
    with pytest.raises(httpx.HTTPError):
        httpx.get(b.url.replace("https://", "http://") + "/health", timeout=3)
    # and the certificate is checked: a client without the network CA refuses the connection
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{b.url}/health", timeout=3)


def test_membership_documents_must_carry_the_directory_signature(net):
    net.up("hospital-a", "hospital-b", "hospital-c")
    a = net.nodes["hospital-a"]
    doc = a.runner.membership
    from dataclasses import replace

    forged = replace(doc, epoch=doc.epoch + 1, chain=("hospital-a", "hospital-c"))
    r = httpx.post(f"{a.url}/membership", json=forged.to_dict(), verify=a.verify, timeout=5)
    assert r.status_code == 403
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    self_signed = replace(forged, signature=None).signed_by(Ed25519PrivateKey.generate())
    r = httpx.post(f"{a.url}/membership", json=self_signed.to_dict(), verify=a.verify, timeout=5)
    assert r.status_code == 403
    assert a.status()["chain"] == HOSPITALS[:3] and len(a.db.events("membership_rejected")) == 2
    # a genuine document can be relayed by anyone (here: re-posted by the test) and is idempotent
    r = httpx.post(f"{a.url}/membership", json=doc.to_dict(), verify=a.verify, timeout=5)
    assert r.status_code == 200 and r.json()["status"] == "stale"
    assert Membership.from_dict(doc.to_dict()) == doc


def test_the_gossip_baseline_also_takes_its_chain_from_the_directory(tmp_path):
    net = Network(tmp_path, HOSPITALS[:3], engine="gossip", round_pause_s=0.05)
    try:
        net.up(*HOSPITALS[:3])
        net.start("hospital-b", rounds=15)
        wait_until(lambda: net.completed(HOSPITALS[:3]), 60, "gossip run completes", 0.3)
        for n in HOSPITALS[:3]:
            hist = net.nodes[n].metrics()["history"]
            assert len(hist) == 15 and all(h["neighbours_heard"] == h["neighbours_expected"] > 0 for h in hist)
            assert {s["status"] for s in net.nodes[n].db.sent} == {"delivered"}  # signed, over TLS, accepted
    finally:
        net.stop()


def test_a_chain_change_never_applies_mid_round(tmp_path):
    """Only at a round boundary: a chain change that arrives mid-run waits, even if it names no round."""
    from node.config import load_config
    from node.db import NullDatabase
    from node.membership import Member
    from node.runner import Runner
    from node.writer import DbWriter

    from chain_harness import write_configs

    cfg = load_config(write_configs(tmp_path, ["a", "b", "c"])["b"])
    r = Runner(cfg, engine=None, db=DbWriter(NullDatabase()), client=None)
    members = {m: Member(m, f"http://{m}:1") for m in "abcd"}
    r.state, r.run_id, r.current_round = "running", "run-1", 7
    r.membership = Membership(epoch=1, chain=("a", "b", "c"), members=members)
    grown = Membership(epoch=2, chain=("a", "b", "c", "d"), members=members)  # no effective_round
    assert r.offer_membership(grown) == "scheduled"
    assert r.membership.epoch == 1 and r.peer("c") is not None  # still the old chain mid-round
    moved = Membership(epoch=3, chain=("a", "b", "c", "d"), members={**members, "d": Member("d", "http://d:2")})
    assert r.offer_membership(moved) == "scheduled", "queued behind the pending change, in epoch order"
    assert [d.epoch for d in r._due(8)] == [2, 3]
    # between runs, nothing is in flight: a change applies at once
    r.state = "completed"
    assert r.offer_membership(Membership(epoch=4, chain=("a", "b"), members=members)) == "applied"
    assert r.membership.epoch == 4 and [n.id for n in r.neighbours] == ["a"]


def test_an_address_only_change_never_overtakes_a_scheduled_chain_change(tmp_path):
    """Pending documents apply strictly in epoch order: a later one waits for every earlier one to be due."""
    from node.config import load_config
    from node.db import NullDatabase
    from node.membership import Member
    from node.runner import Runner
    from node.writer import DbWriter

    from chain_harness import write_configs

    cfg = load_config(write_configs(tmp_path, ["a", "b", "c"])["b"])
    r = Runner(cfg, engine=None, db=DbWriter(NullDatabase()), client=None)
    members = {m: Member(m, f"http://{m}:1") for m in "abcd"}
    r.state, r.run_id, r.current_round = "running", "run-1", 3
    r.membership = Membership(epoch=1, chain=("a", "b", "c"), members=members)
    grown = Membership(epoch=2, chain=("a", "b", "c", "d"), members=members, run_id="run-1", effective_round=10)
    assert r.offer_membership(grown) == "scheduled"
    moved = Membership(epoch=3, chain=("a", "b", "c", "d"), members={**members, "c": Member("c", "http://c:2")})
    assert r.offer_membership(moved) == "scheduled"
    assert r._due(4) == [] and r._due(9) == []
    assert r.membership.epoch == 1 and [n.id for n in r.neighbours] == ["a", "c"]
    assert [d.epoch for d in r._due(10)] == [2, 3]
    assert r._pending == []
