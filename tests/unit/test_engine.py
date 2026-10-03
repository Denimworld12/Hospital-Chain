import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from node import wire
from node.engine import Engine, NeighborUpdate
from node.config import load_config
from node.engines import build_engine
from node.gossip import LABEL, GossipBaselineEngine
from node.trainer import N_PARAMS, LocalTrainer

ROOT = Path(__file__).resolve().parents[2]
SLICES = ROOT / "data" / "slices"


def trainer(node="hospital-a"):
    return LocalTrainer(SLICES / f"{node}.csv", test_fraction=0.25, seed=7, learning_rate=0.005, local_epochs=2)


def test_slices_are_real_uci_non_iid_splits():
    manifest = json.loads((SLICES / "manifest.json").read_text())
    counts = {k: v["patients"] for k, v in manifest["slices"].items()}
    assert counts == {"hospital-a": 303, "hospital-b": 294, "hospital-c": 200, "hospital-d": 62, "hospital-e": 61}
    # d and e are the two halves (alternating rows) of the one Swiss file: together, all of it
    assert "half 1 of 2" in manifest["slices"]["hospital-d"]["institution"]
    rates = [v["positive_rate"] for v in manifest["slices"].values()]
    assert max(rates) - min(rates) > 0.3, "slices should differ in disease prevalence (non-IID)"
    for node, n in counts.items():
        assert len(trainer(node).y_train) + len(trainer(node).y_test) == n


def node_config(tmp_path, engine, bits=2, extra=""):
    p = tmp_path / "n.yaml"
    p.write_text(
        f"""node_id: hospital-a
port: 1
data_slice: {SLICES / 'hospital-a.csv'}
shared_secret: s
engine: {engine}
bits: {bits}
chain: [hospital-a, hospital-b]
neighbours:
  - id: hospital-b
    url: http://b:2
{extra}"""
    )
    return load_config(p)


@pytest.mark.parametrize("engine,label", [
    ("gossip", "naive gossip baseline"),
    ("qgadmm", "Q-GADMM (b=2)"),
    ("gadmm_fp", "GADMM full precision (float32 deltas)"),
])
def test_every_engine_implements_the_swappable_interface(tmp_path, engine, label):
    eng = build_engine(node_config(tmp_path, engine), trainer())
    assert isinstance(eng, Engine)
    for method in ("train_round", "package_update", "apply_neighbor_update", "evaluate", "reset"):
        assert callable(getattr(eng, method))
    assert eng.label == label


def test_gossip_ignores_the_admm_bits_setting(tmp_path):
    eng = build_engine(node_config(tmp_path, "gossip", bits=2), trainer())
    assert eng.label == LABEL == "naive gossip baseline" and eng.bits == 64 and eng.dtype == "float64"


def test_unknown_engine_and_unsupported_bits_are_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown engine"):
        node_config(tmp_path, "q-gadmm")
    with pytest.raises(ValueError, match="bits"):
        build_engine(node_config(tmp_path, "qgadmm", bits=3), trainer())
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
