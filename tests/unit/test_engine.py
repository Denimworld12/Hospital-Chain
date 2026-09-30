import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from node import wire
from node.engine import Engine, NeighborUpdate
from node.gossip import LABEL, GossipBaselineEngine, build_engine
from node.trainer import N_PARAMS, LocalTrainer

ROOT = Path(__file__).resolve().parents[2]
SLICES = ROOT / "data" / "slices"


def trainer(node="hospital-a"):
    return LocalTrainer(SLICES / f"{node}.csv", test_fraction=0.25, seed=7, learning_rate=0.005, local_epochs=2)


def test_slices_are_real_uci_non_iid_splits():
    manifest = json.loads((SLICES / "manifest.json").read_text())
    counts = {k: v["patients"] for k, v in manifest["slices"].items()}
    assert counts == {"hospital-a": 303, "hospital-b": 294, "hospital-c": 200}
    rates = [v["positive_rate"] for v in manifest["slices"].values()]
    assert max(rates) - min(rates) > 0.3, "slices should differ in disease prevalence (non-IID)"
    for node, n in counts.items():
        assert len(trainer(node).y_train) + len(trainer(node).y_test) == n


def test_engine_implements_the_swappable_interface():
    eng = build_engine("gossip-baseline", trainer(), bits=64, rho=1.0)
    assert isinstance(eng, Engine)
    for method in ("train_round", "package_update", "apply_neighbor_update"):
        assert callable(getattr(eng, method))
    assert eng.label == LABEL == "naive gossip baseline"


def test_unknown_engine_and_quantized_bits_are_refused():
    with pytest.raises(ValueError):
        build_engine("q-gadmm", trainer(), bits=64, rho=1.0)
    with pytest.raises(ValueError, match="float64"):
        GossipBaselineEngine(trainer(), bits=8)


def test_local_training_reduces_loss():
    eng = GossipBaselineEngine(trainer())
    start = eng.evaluate().loss
    for r in range(1, 11):
        eng.train_round(r)
    assert eng.evaluate().loss < start - 0.1


def test_update_package_is_only_the_weight_array():
    eng = GossipBaselineEngine(trainer())
    eng.train_round(1)
    pkg = eng.package_update(1)
    assert pkg.n_params == N_PARAMS
    assert pkg.payload_bytes == N_PARAMS * 8 == pkg.full_precision_bytes
    assert pkg.checksum == hashlib.sha256(pkg.body).hexdigest()
    np.testing.assert_array_equal(wire.decode(pkg.body), eng.trainer.weights)


def test_apply_neighbor_update_is_a_uniform_average():
    eng = GossipBaselineEngine(trainer())
    own = np.arange(N_PARAMS, dtype=float)
    eng.trainer.weights = own.copy()
    a, b = own + 3, own - 6
    ups = [NeighborUpdate(s, 1, wire.encode(w), wire.checksum(wire.encode(w)), "float64") for s, w in (("x", a), ("y", b))]
    eng.apply_neighbor_update(ups)
    np.testing.assert_allclose(eng.trainer.weights, (own + a + b) / 3)
    eng.apply_neighbor_update([])  # no neighbours heard: weights unchanged
    np.testing.assert_allclose(eng.trainer.weights, (own + a + b) / 3)


def test_apply_rejects_wrong_shape():
    eng = GossipBaselineEngine(trainer())
    body = wire.encode(np.zeros(3))
    with pytest.raises(ValueError):
        eng.apply_neighbor_update([NeighborUpdate("x", 1, body, wire.checksum(body), "float64")])
