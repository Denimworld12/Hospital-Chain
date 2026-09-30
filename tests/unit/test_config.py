from pathlib import Path

import pytest

from node.config import load_config

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = sorted((ROOT / "config").glob("*.yaml"))


def test_three_hospital_configs_load():
    cfgs = [load_config(p) for p in CONFIGS]
    assert len(cfgs) == 3
    assert len({c.node_id for c in cfgs}) == 3
    assert len({c.port for c in cfgs}) == 3
    for c in cfgs:
        assert c.engine == "qgadmm"
        assert (c.rho, c.alpha, c.bits, c.local_epochs, c.lr, c.epsilon, c.checkpoint_every) == (
            24.0, 1.0, 2, 50, 0.01, 0.001, 5)
        assert c.seed == 42 and c.checkpoint_dir == "/checkpoints"
        assert c.training.rounds == 200
        assert c.timing.heartbeat_interval_s == 5
    assert len({c.shared_secret for c in cfgs}) == 1, "all nodes must share one secret"
    assert len({c.chain for c in cfgs}) == 1, "all nodes must agree on the chain order"


def test_chain_order_matches_the_neighbour_links():
    cfgs = {c.node_id: c for c in (load_config(p) for p in CONFIGS)}
    chain = next(iter(cfgs.values())).chain
    assert set(chain) == set(cfgs)
    for i, node in enumerate(chain):
        adjacent = {chain[j] for j in (i - 1, i + 1) if 0 <= j < len(chain)}
        assert {n.id for n in cfgs[node].neighbours} == adjacent


def base_yaml(tmp_path, body):
    p = tmp_path / "c.yaml"
    p.write_text("node_id: b\nport: 1\ndata_slice: s.csv\nshared_secret: s\n" + body)
    return p


def test_admm_engines_need_a_consistent_chain(tmp_path):
    nb = "neighbours:\n  - id: a\n    url: http://a:1\n  - id: c\n    url: http://c:1\n"
    with pytest.raises(ValueError, match="chain"):
        load_config(base_yaml(tmp_path, "engine: qgadmm\n" + nb))
    with pytest.raises(ValueError, match="chain neighbours"):
        load_config(base_yaml(tmp_path, "engine: qgadmm\nchain: [a, c, b]\n" + nb))
    ok = load_config(base_yaml(tmp_path, "engine: gadmm_fp\nchain: [a, b, c]\n" + nb))
    assert ok.is_admm and ok.chain == ("a", "b", "c")
    # the gossip baseline does not need a chain; its legacy name still loads
    legacy = load_config(base_yaml(tmp_path, "engine: gossip-baseline\n" + nb))
    assert legacy.engine == "gossip" and not legacy.is_admm
    with pytest.raises(ValueError, match="unknown engine"):
        load_config(base_yaml(tmp_path, "engine: sgd\n" + nb))


def test_neighbour_links_are_symmetric_and_resolve():
    cfgs = {c.node_id: c for c in (load_config(p) for p in CONFIGS)}
    for c in cfgs.values():
        assert c.neighbours, f"{c.node_id} has no neighbours"
        for n in c.neighbours:
            assert n.id in cfgs, f"{c.node_id} lists unknown neighbour {n.id}"
            assert cfgs[n.id].neighbour(c.node_id) is not None, f"{n.id} does not list {c.node_id} back"
            assert n.url.endswith(f":{cfgs[n.id].port}")


def test_each_config_points_at_its_own_slice():
    for c in (load_config(p) for p in CONFIGS):
        name = Path(c.data_slice).name
        assert name == f"{c.node_id}.csv"
        assert (ROOT / "data" / "slices" / name).is_file()


def test_rejects_self_neighbour(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("node_id: x\nport: 1\ndata_slice: s.csv\nshared_secret: s\nneighbours:\n  - id: x\n    url: http://x:1\n")
    with pytest.raises(ValueError):
        load_config(p)
