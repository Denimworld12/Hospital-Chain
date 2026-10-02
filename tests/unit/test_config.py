from pathlib import Path

import pytest

from node.config import load_config, peer_url_env

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
    # the gossip baseline does not need a chain; only the three engine names load
    gossip = load_config(base_yaml(tmp_path, "engine: gossip\n" + nb))
    assert gossip.engine == "gossip" and not gossip.is_admm
    for name in ("gossip-baseline", "sgd"):
        with pytest.raises(ValueError, match="unknown engine"):
            load_config(base_yaml(tmp_path, f"engine: {name}\n" + nb))


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


def test_peer_urls_and_advertise_url_come_from_the_environment(monkeypatch):
    """The same YAML points at real hosts (LAN IP, Tailscale name) with no edit, only env."""
    monkeypatch.setenv("MEDCHAIN_PEER_HOSPITAL_A_URL", "http://192.168.1.20:8001/")
    monkeypatch.setenv("MEDCHAIN_PEER_HOSPITAL_C_URL", "https://hospital-c.tail1234.ts.net:8003")
    monkeypatch.setenv("MEDCHAIN_ADVERTISE_URL", "http://hospital-b.tail1234.ts.net:8002")
    c = load_config(ROOT / "config" / "hospital-b.yaml")
    assert c.neighbour("hospital-a").url == "http://192.168.1.20:8001"
    assert c.neighbour("hospital-c").url == "https://hospital-c.tail1234.ts.net:8003"
    assert c.advertise_url == "http://hospital-b.tail1234.ts.net:8002"
    # an override for another node's peer does not leak into this one
    a = load_config(ROOT / "config" / "hospital-a.yaml")
    assert a.neighbour("hospital-b").url == "http://hospital-b:8002"


def test_without_overrides_the_yaml_addresses_stand(monkeypatch):
    monkeypatch.setenv("MEDCHAIN_PEER_HOSPITAL_B_URL", "  ")
    c = load_config(ROOT / "config" / "hospital-a.yaml")
    assert c.neighbour("hospital-b").url == "http://hospital-b:8002"
    assert c.advertise_url == "http://hospital-a:8001"


def test_peer_url_env_names():
    assert peer_url_env("hospital-b") == "MEDCHAIN_PEER_HOSPITAL_B_URL"
    assert peer_url_env("st.mary's--east") == "MEDCHAIN_PEER_ST_MARY_S_EAST_URL"


def test_rejects_an_override_that_is_not_a_url(monkeypatch):
    monkeypatch.setenv("MEDCHAIN_PEER_HOSPITAL_B_URL", "192.168.1.21:8002")
    with pytest.raises(ValueError, match="MEDCHAIN_PEER_HOSPITAL_B_URL"):
        load_config(ROOT / "config" / "hospital-a.yaml")
