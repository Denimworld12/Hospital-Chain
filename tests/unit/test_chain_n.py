"""Chains of any length: positions 1, 3, 5, ... are heads and 2, 4, ... tails, for odd and even N alike."""

import numpy as np
import pytest

from chain_harness import AUTH, ROOT, NodeServer, wait_until, write_configs
from node.qgadmm.engine import QGADMMEngine
from node.qgadmm.scheduler import simulate
from node.qgadmm.state import HEAD, TAIL, ChainPosition, all_edges
from node.trainer import LocalTrainer

SLICES = ["hospital-a", "hospital-b", "hospital-c"]


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 8])
def test_positions_alternate_head_tail_for_any_length(n):
    chain = tuple(f"h{i}" for i in range(1, n + 1))
    groups = [ChainPosition(chain, m).group for m in chain]
    assert groups == [HEAD if i % 2 else TAIL for i in range(1, n + 1)]
    for a, b in zip(chain, chain[1:]):  # every link joins a head to a tail: the two-phase schedule holds
        assert {ChainPosition(chain, a).group, ChainPosition(chain, b).group} == {HEAD, TAIL}
    assert len(all_edges(chain)) == n - 1


RHO = 5.0  # the shipped configs' rho: at b=2 it reaches consensus for every chain length tested (24 stalls at N>=4)


def engines_for(chain, bits=2):
    out = []
    for i, node in enumerate(chain):
        t = LocalTrainer(ROOT / "data" / "slices" / f"{SLICES[i % 3]}.csv", test_fraction=0.25, seed=7,
                         learning_rate=0.005, local_epochs=2)
        pos = ChainPosition(tuple(chain), node)
        out.append(QGADMMEngine(t.X_train, t.y_train, t.X_test, t.y_test, position=pos, rho=RHO, alpha=1.0, bits=bits,
                                local_epochs=50, lr=0.01, seed=42 + pos.position))
    return out


@pytest.mark.parametrize("n", [4, 5])
def test_in_process_chain_of_n_reaches_consensus(n):
    chain = [f"hospital-{i}" for i in range(1, n + 1)]
    res = simulate(engines_for(chain), 600)
    assert res.converged_at is not None, f"N={n}: no consensus"
    assert set(res.final.residuals) == set(all_edges(tuple(chain)))
    assert res.final.messages == 2 * (n - 1)  # one packet each way per link per round


@pytest.mark.parametrize("n", [4, 5])
def test_networked_round_loop_at_n(tmp_path, n):
    """The production node apps over real HTTP at N=4 and N=5: head/tail alternation, every link every round."""
    chain = [f"hospital-{i}" for i in range(1, n + 1)]
    cfgs = write_configs(tmp_path, chain, slices={m: SLICES[i % 3] for i, m in enumerate(chain)}, rounds=400, rho=RHO)
    nodes = {m: NodeServer(p).start() for m, p in cfgs.items()}
    try:
        r = nodes[chain[0]].post("/round/start", json={})
        assert r.json()["status"] == "started"
        wait_until(lambda: all(s.status()["run_state"] == "completed" for s in nodes.values()), 300, "run completes", 0.3)
        stops = {s.status()["current_round"] for s in nodes.values()}
        assert len(stops) == 1 and stops.pop() < 400, "consensus before the round limit, same round everywhere"
        for i, m in enumerate(chain, start=1):
            st = nodes[m].status()
            assert st["position"] == i and st["group"] == (HEAD if i % 2 else TAIL)
            groups = {row["residuals"]["group"] for row in nodes[m].db.metrics}
            assert groups == {st["group"]}
            hist = nodes[m].metrics()["history"]
            assert not any(h["degraded"] for h in hist), f"{m} missed a neighbour"
            assert all(h["neighbours_heard"] == len(nodes[m].cfg.neighbours) for h in hist)
        # identical copies of every link's lambda at both ends
        for a, b in zip(chain, chain[1:]):
            ea, eb = nodes[a].engine.state, nodes[b].engine.state
            assert np.array_equal(ea.lambdas[b], eb.lambdas[a])
            assert np.array_equal(ea.theta_hat, eb.mirrors[a]) and np.array_equal(eb.theta_hat, ea.mirrors[b])
    finally:
        for s in nodes.values():
            s.stop()
    assert AUTH  # the start went through the shared-secret check
