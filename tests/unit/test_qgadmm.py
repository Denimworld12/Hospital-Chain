"""Q-GADMM engine math, in process: determinism, boundary nodes, dual symmetry, convergence, checkpoints."""

from pathlib import Path

import numpy as np
import pytest
import torch
from sklearn.linear_model import LinearRegression

from node.qgadmm import quantizer as qz
from node.qgadmm.engine import LINEAR, LOGISTIC, QGADMMEngine
from node.qgadmm.scheduler import as_update, converged, simulate
from node.qgadmm.state import HEAD, TAIL, ChainPosition, all_edges
from node.trainer import LocalTrainer

ROOT = Path(__file__).resolve().parents[2]
SLICES = ROOT / "data" / "slices"
UCI = ("hospital-a", "hospital-b", "hospital-c")


def uci_chain(bits=2, rho=24.0, local_epochs=50, chain=UCI):
    engines = []
    for i, node in enumerate(chain):
        t = LocalTrainer(SLICES / f"{UCI[i % 3]}.csv", test_fraction=0.25, seed=7, learning_rate=0.005, local_epochs=2)
        engines.append(QGADMMEngine(t.X_train, t.y_train, t.X_test, t.y_test, position=ChainPosition(tuple(chain), node),
                                    rho=rho, alpha=1.0, bits=bits, local_epochs=local_epochs, lr=0.01, seed=42 + i + 1))
    return engines


def edge_pairs(engines):
    by = {e.pos.node_id: e for e in engines}
    chain = engines[0].pos.chain
    return [(by[a], by[b]) for a, b in zip(chain, chain[1:])]


# -- round-trip determinism -------------------------------------------------------


@pytest.mark.parametrize("bits", [2, 4, 8, qz.FULL_PRECISION_BITS])
def test_sender_snapshot_and_receiver_mirror_are_bit_identical_every_round(bits):
    """Exact float equality (not allclose), checked after every one of 50 rounds, on every edge, both ways."""
    engines = uci_chain(bits=bits)
    pairs = edge_pairs(engines)
    checked = []

    def check(rec):
        for left, right in pairs:
            assert np.array_equal(left.state.theta_hat, right.state.mirrors[left.pos.node_id]), f"round {rec.round}"
            assert np.array_equal(right.state.theta_hat, left.state.mirrors[right.pos.node_id]), f"round {rec.round}"
            # same bytes, not merely equal values
            assert left.state.theta_hat.tobytes() == right.state.mirrors[left.pos.node_id].tobytes()
            # and both ends of the edge hold the identical dual variable
            assert np.array_equal(left.state.lambdas[right.pos.node_id], right.state.lambdas[left.pos.node_id])
        checked.append(rec.round)

    simulate(engines, 50, stop_on_convergence=False, on_round=check)
    assert checked == list(range(1, 51))
    # the snapshots moved: this was not a trivial all-zeros comparison
    assert all(np.linalg.norm(e.state.theta_hat) > 0.1 for e in engines)


def test_every_packet_is_verified_against_the_receivers_mirror():
    a, b, _ = uci_chain()
    a.train_round(1)
    pkg = a.package_update(1)
    res = b.apply_neighbor_update([as_update("hospital-a", 1, pkg)])
    assert res.applied == ("hospital-a",) and not res.resync
    assert b.apply_neighbor_update([as_update("hospital-a", 1, pkg)]).duplicate == ("hospital-a",)
    # a mirror that drifted by a single ulp is caught on the next packet and flagged for resync
    b.state.mirrors["hospital-a"][3] = np.nextafter(b.state.mirrors["hospital-a"][3], np.inf)
    a.train_round(2)
    res = b.apply_neighbor_update([as_update("hospital-a", 2, a.package_update(2))])
    assert res.resync == ("hospital-a",) and not res.applied


# -- boundary nodes ---------------------------------------------------------------


def test_chain_groups_and_boundary_neighbours():
    chain = ("n1", "n2", "n3", "n4", "n5")
    pos = {n: ChainPosition(chain, n) for n in chain}
    assert [pos[n].group for n in chain] == [HEAD, TAIL, HEAD, TAIL, HEAD]
    assert pos["n1"].neighbours == ("n2",) and pos["n5"].neighbours == ("n4",)
    assert pos["n3"].neighbours == ("n2", "n4")
    assert pos["n1"].edges == (("n1", "n2"),) and pos["n5"].edges == (("n4", "n5"),)
    assert all_edges(chain) == ("n1|n2", "n2|n3", "n3|n4", "n4|n5")


def test_boundary_nodes_have_exactly_one_penalty_and_one_dual_term():
    """3-node chain: nodes 1 and 3 have ONE edge, so one lambda, one mirror, one dual and one penalty term."""
    rho = 24.0
    engines = uci_chain(rho=rho)
    rng = np.random.default_rng(5)
    for e in engines:  # arbitrary non-zero duals / mirrors so every term is exercised
        for m in e.pos.neighbours:
            e.state.lambdas[m] = rng.normal(0, 1, e.d)
            e.state.mirrors[m] = rng.normal(0, 1, e.d)
    first, middle, last = engines
    assert set(first.state.lambdas) == set(first.state.mirrors) == {"hospital-b"}
    assert set(last.state.lambdas) == set(last.state.mirrors) == {"hospital-b"}
    assert set(middle.state.lambdas) == {"hospital-a", "hospital-c"}

    theta = rng.normal(0, 0.3, first.d)
    for e in engines:
        terms = e.lagrangian_terms(theta)
        dual = sorted(k for k in terms if k.startswith("dual:"))
        penalty = sorted(k for k in terms if k.startswith("penalty:"))
        assert dual == [f"dual:{m}" for m in sorted(e.pos.neighbours)]
        assert penalty == [f"penalty:{m}" for m in sorted(e.pos.neighbours)]

        # composition, recomputed independently in numpy
        X, y = e._X.numpy(), e._y.numpy()
        with np.errstate(all="ignore"):  # spurious macOS Accelerate matmul warnings
            z = X @ theta[:-1] + theta[-1]
        local = np.sum(np.logaddexp(0, z) - y * z)  # BCE-with-logits, reduction='sum'
        assert terms["local_loss"] == pytest.approx(local, rel=1e-10)
        expected = local
        if e.pos.left is not None:  # edge (n-1, n): lambda^T (theta_hat_{n-1} - theta) + rho/2 ||theta_hat_{n-1} - theta||^2
            gap = e.state.mirrors[e.pos.left] - theta
            assert terms[f"dual:{e.pos.left}"] == pytest.approx(e.state.lambdas[e.pos.left] @ gap, rel=1e-10)
            assert terms[f"penalty:{e.pos.left}"] == pytest.approx(rho / 2 * gap @ gap, rel=1e-10)
            expected += e.state.lambdas[e.pos.left] @ gap + rho / 2 * gap @ gap
        if e.pos.right is not None:  # edge (n, n+1): lambda^T (theta - theta_hat_{n+1}) + rho/2 ||theta - theta_hat_{n+1}||^2
            gap = theta - e.state.mirrors[e.pos.right]
            assert terms[f"dual:{e.pos.right}"] == pytest.approx(e.state.lambdas[e.pos.right] @ gap, rel=1e-10)
            assert terms[f"penalty:{e.pos.right}"] == pytest.approx(rho / 2 * gap @ gap, rel=1e-10)
            expected += e.state.lambdas[e.pos.right] @ gap + rho / 2 * gap @ gap
        assert sum(terms.values()) == pytest.approx(expected, rel=1e-10)
        assert len(terms) == 1 + 2 * len(e.pos.neighbours)  # 3 for the boundary nodes, 5 for the middle


def test_primal_step_minimises_exactly_the_lagrangian_it_reports():
    """The gradient Adam follows is the gradient of sum(lagrangian_terms): no hidden extra/missing term."""
    first, _, last = uci_chain()
    rng = np.random.default_rng(9)
    for e in (first, last):
        m = e.pos.neighbours[0]
        e.state.lambdas[m] = rng.normal(0, 1, e.d)
        e.state.mirrors[m] = rng.normal(0, 1, e.d)
        theta = torch.tensor(rng.normal(0, 0.3, e.d), requires_grad=True)
        e._load_theta(theta.detach().numpy())
        e.model.zero_grad()
        loss = e._local_loss_sum(e._X, e._y) + sum(e._coupling_terms(e._theta_tensor()).values())
        loss.backward()
        grad = torch.cat([e.model.weight.grad.reshape(-1), e.model.bias.grad]).numpy()
        # finite differences of the reported objective
        f = lambda t: sum(e.lagrangian_terms(t).values())  # noqa: E731
        base = theta.detach().numpy()
        num = np.array([(f(base + h) - f(base - h)) / 2e-6 for h in np.eye(e.d) * 1e-6])
        np.testing.assert_allclose(grad, num, rtol=1e-4, atol=1e-4)


def test_boundary_dual_update_touches_its_single_edge_and_matches_the_other_end():
    engines = uci_chain()
    simulate(engines, 3, stop_on_convergence=False)
    first, middle, last = engines
    assert set(first.state.lambdas) == {"hospital-b"} and set(last.state.lambdas) == {"hospital-b"}
    np.testing.assert_array_equal(first.state.lambdas["hospital-b"], middle.state.lambdas["hospital-a"])
    np.testing.assert_array_equal(last.state.lambdas["hospital-b"], middle.state.lambdas["hospital-c"])
    # Eq. 18 by hand on the boundary edge: lambda += alpha*rho*(theta_hat_1 - theta_hat_2)
    before = first.state.lambdas["hospital-b"].copy()
    first.dual_update({"hospital-b"})
    np.testing.assert_array_equal(
        first.state.lambdas["hospital-b"],
        before + 1.0 * 24.0 * (first.state.theta_hat - first.state.mirrors["hospital-b"]),
    )
    # an edge whose exchange did not complete keeps its lambda
    before = middle.state.lambdas.copy()
    middle.dual_update({"hospital-a"})
    np.testing.assert_array_equal(middle.state.lambdas["hospital-c"], before["hospital-c"])


def test_two_node_chain_has_no_interior_node():
    engines = uci_chain(chain=("x1", "x2"))
    assert [e.pos.group for e in engines] == [HEAD, TAIL]
    assert all(len(e.pos.neighbours) == 1 for e in engines)
    simulate(engines, 5, stop_on_convergence=False)
    x1, x2 = engines
    np.testing.assert_array_equal(x1.state.theta_hat, x2.state.mirrors["x1"])


# -- convergence sanity -------------------------------------------------------------


def synthetic_linear(n_nodes=5, n_features=6, per_node=200, seed=0):
    rng = np.random.default_rng(seed)
    w, b = rng.normal(0, 1, n_features), 0.5
    data = []
    for i in range(n_nodes):  # non-IID: each node's features are shifted
        X = rng.normal(0.3 * i, 1, (per_node, n_features))
        with np.errstate(all="ignore"):  # spurious macOS Accelerate matmul warnings
            data.append((X, X @ w + b + rng.normal(0, 0.5, per_node)))
    return data


def linear_chain(data, bits):
    chain = tuple(f"node-{i + 1}" for i in range(len(data)))
    return [QGADMMEngine(X, y, X[:20], y[:20], position=ChainPosition(chain, chain[i]), rho=24.0, alpha=1.0, bits=bits,
                         local_epochs=50, lr=0.01, seed=42 + i + 1, task=LINEAR) for i, (X, y) in enumerate(data)]


def test_convergence_sanity_linear_regression_n5():
    """N=5 in-process: Q-GADMM b=2 lands within 1% of full-precision GADMM and 2% of centralized sklearn."""
    data = synthetic_linear()
    X = np.vstack([d[0] for d in data])
    y = np.concatenate([d[1] for d in data])
    with np.errstate(all="ignore"):
        central = LinearRegression().fit(X, y)
        central_loss = 0.5 * np.mean((central.predict(X) - y) ** 2)

    q = simulate(linear_chain(data, 2), 400, epsilon=1e-3)
    fp = simulate(linear_chain(data, qz.FULL_PRECISION_BITS), 400, epsilon=1e-3)
    assert q.converged_at is not None and fp.converged_at is not None, "consensus (max residual < eps x3) not reached"
    assert q.final.max_residual < 1e-3
    assert q.final.objective == pytest.approx(fp.final.objective, rel=0.01)
    assert q.final.objective == pytest.approx(central_loss, rel=0.02)
    assert fp.final.objective == pytest.approx(central_loss, rel=0.02)
    # and quantization really saved bits
    assert q.final.cumulative_bits < fp.final.cumulative_bits / 3


def test_uci_logistic_reaches_consensus_near_the_centralized_optimum():
    from sklearn.linear_model import LogisticRegression

    engines = uci_chain()
    res = simulate(engines, 300, epsilon=1e-3)
    assert res.converged_at is not None and res.converged_at < 300
    X = np.vstack([e._X.numpy() for e in engines])
    y = np.concatenate([e._y.numpy() for e in engines])
    with np.errstate(all="ignore"):
        clf = LogisticRegression(penalty=None, max_iter=5000).fit(X, y)
        p = np.clip(clf.predict_proba(X)[:, 1], 1e-15, 1 - 1e-15)
    central = float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
    assert res.final.objective == pytest.approx(central, rel=0.02)


def test_converged_needs_every_edge_below_epsilon_for_three_rounds():
    edges = ["a|b", "b|c"]
    ok = {"a|b": 1e-4, "b|c": 2e-4}
    assert converged([ok, ok, ok], edges, 1e-3)
    assert not converged([ok, ok], edges, 1e-3)
    assert not converged([ok, {"a|b": 1e-4, "b|c": 2e-3}, ok], edges, 1e-3)
    assert not converged([ok, {"a|b": 1e-4}, ok], edges, 1e-3)


# -- checkpointing ------------------------------------------------------------------


def test_checkpoint_roundtrip_restores_state_and_rng(tmp_path):
    engines = uci_chain()
    simulate(engines, 7, stop_on_convergence=False)
    e = engines[1]
    path = tmp_path / "b.ckpt.pkl"
    assert e.save(path) > 0
    twin = uci_chain()[1]
    assert twin.restore(path)
    for k in ("theta", "theta_hat"):
        np.testing.assert_array_equal(getattr(twin.state, k), getattr(e.state, k))
    for m in e.pos.neighbours:
        np.testing.assert_array_equal(twin.state.mirrors[m], e.state.mirrors[m])
        np.testing.assert_array_equal(twin.state.lambdas[m], e.state.lambdas[m])
    assert twin.state.round_no == 7 and twin.state.seq == e.state.seq
    # identical RNG and optimizer state: the next primal step and packet are bit-identical
    e.train_round(8)
    twin.train_round(8)
    np.testing.assert_array_equal(twin.state.theta, e.state.theta)
    assert twin.package_update(8).body == e.package_update(8).body
    assert not uci_chain()[0].restore(tmp_path / "missing.pkl")


def test_engine_labels_are_honest():
    assert uci_chain(bits=2)[0].label == "Q-GADMM (b=2)"
    assert uci_chain(bits=8)[0].label == "Q-GADMM (b=8)"
    fp = uci_chain(bits=qz.FULL_PRECISION_BITS)[0]
    assert fp.label == "GADMM full precision (float32 deltas)" and fp.name == "gadmm_fp"
    with pytest.raises(ValueError):
        uci_chain(bits=16)


def test_logistic_is_the_default_task():
    assert uci_chain()[0].task == LOGISTIC
