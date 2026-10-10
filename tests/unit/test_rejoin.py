"""A node that restarts while the directory is down (node/rejoin.py).

The Step 0 audit reproduced the gap this closes. In a chain A-B-C, stop the directory,
then kill and restart B: B came back at epoch -1, outside the chain. A and C stopped
being neighbours, and A ran to the 400-round limit with 381/400 rounds degraded and no
consensus. Now B offers its saved, directory-signed chain to its saved neighbours, and
they take it back only if the chain epoch is unchanged and B's TLS key is the pinned one.
"""

import pki
from chain_harness import DirectoryServer, NodeServer, wait_until
from test_membership import HOSPITALS, Network

import pytest

ABC = HOSPITALS[:3]


@pytest.fixture
def net(tmp_path):
    n = Network(tmp_path)
    yield n
    n.stop()


def restart(net, name):
    net.nodes[name].stop(abrupt=True)
    net.nodes[name] = NodeServer(net.cfgs[name]).start()
    return net.nodes[name]


def test_restart_without_the_directory_rejoins_when_the_chain_is_unchanged(net):
    """The audit's reproduction: A-B-C, round 12: stop the directory, kill and restart B."""
    net.up(*ABC)
    a, c = net.nodes["hospital-a"], net.nodes["hospital-c"]
    net.start("hospital-a")
    wait_until(lambda: net.nodes["hospital-b"].runner.current_round >= 12, 60, "round 12")
    epoch = a.status()["epoch"]
    net.directory.stop(abrupt=True)
    b = restart(net, "hospital-b")
    wait_until(lambda: b.status()["in_chain"], 30, "b rejoins without the directory")
    st = b.status()
    assert (st["epoch"], st["chain"]) == (epoch, ABC)
    assert b.db.events("rejoin_accepted")[0]["detail"]["neighbours"] == {"hospital-a": "accepted",
                                                                         "hospital-c": "accepted"}
    assert a.db.events("rejoin_accepted") and c.db.events("rejoin_accepted")
    wait_until(lambda: b.db.events("rejoined_from_checkpoint"), 30, "b resumes the run from its checkpoint")
    wait_until(lambda: net.completed(ABC), 240, "the run completes without the directory", 0.3)
    hist = {n: net.nodes[n].metrics()["history"] for n in ABC}
    degraded = sum(h["degraded"] for h in hist["hospital-a"])
    last = hist["hospital-a"][-1]
    print(f"\nreproduction: hospital-a finished at round {last['round']} with {degraded}/{len(hist['hospital-a'])} "
          f"rounds degraded, max residual {last['max_residual']:.2e} (before the fix: 381/400 degraded, no consensus)")
    assert last["round"] < 400 and last["max_residual"] < 1e-3, "consensus, not the round limit"
    assert degraded < 40


def test_rejoin_is_refused_when_the_chain_changed_while_the_node_was_down(net):
    net.up(*HOSPITALS)
    a, c = net.nodes["hospital-a"], net.nodes["hospital-c"]
    old = a.status()["epoch"]
    net.nodes["hospital-b"].stop(abrupt=True)
    # while b is down the chain changes: d leaves (a leave is the directory's bridge event)
    assert net.nodes["hospital-d"].post("/membership/leave").status_code == 200
    wait_until(lambda: a.status()["epoch"] == c.status()["epoch"] == old + 1, 20, "a and c take the new epoch")
    net.directory.stop(abrupt=True)
    net.nodes["hospital-b"] = b = NodeServer(net.cfgs["hospital-b"]).start()
    wait_until(lambda: b.db.events("rejoin_refused"), 30, "b's neighbours turn it away")
    answers = b.db.events("rejoin_refused")[0]["detail"]["neighbours"]
    assert set(answers) == {"hospital-a", "hospital-c"}
    assert all(f"presents epoch {old}" in v and "wait for the directory" in v for v in answers.values()), answers
    assert a.db.events("rejoin_refused") and c.db.events("rejoin_refused")
    st = b.status()
    assert not st["in_chain"] and st["epoch"] == -1, "stale state is never trusted"
    # once the directory is back, b registers and gets the current chain from it
    net.directory = DirectoryServer(net.dir_cfg).start()
    wait_until(lambda: b.status()["in_chain"], 45, "b placed by the directory")
    assert (b.status()["epoch"], b.status()["chain"]) == (old + 1, ABC)


def test_rejoin_is_refused_when_the_node_comes_back_with_another_tls_key(net):
    net.up(*ABC)
    a = net.nodes["hospital-a"]
    pki_dir = net.dir_cfg.parent / "pki"
    net.nodes["hospital-b"].stop(abrupt=True)
    for f in ("tls.crt", "tls.key"):  # b is re-keyed while down: a CA-valid certificate, but not the pinned key
        (pki_dir / "hospital-b" / f).unlink()
    pki.ensure_identity(pki_dir, "hospital-b", ["127.0.0.1", "localhost"], credential=True)
    net.directory.stop(abrupt=True)
    net.nodes["hospital-b"] = b = NodeServer(net.cfgs["hospital-b"]).start()
    wait_until(lambda: b.db.events("rejoin_refused"), 30, "b's neighbours turn it away")
    answers = b.db.events("rejoin_refused")[0]["detail"]["neighbours"]
    assert all("hospital-b's TLS key changed" in v for v in answers.values()), answers
    assert [x for x in a.db.events("tls_pin_mismatch") if x["detail"]["path"] == "/membership/rejoin"]
    assert not b.status()["in_chain"]
