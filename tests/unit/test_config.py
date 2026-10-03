from pathlib import Path

import pytest

from node.config import load_config, peer_url_env

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = sorted((ROOT / "config").glob("hospital-*.yaml"))


def test_hospital_configs_load_and_take_their_chain_from_the_directory():
    cfgs = [load_config(p) for p in CONFIGS]
    assert len(cfgs) == 5  # A-D start with the stack; E is the one that joins
    assert len({c.node_id for c in cfgs}) == 5
    assert len({c.port for c in cfgs}) == 5
    for c in cfgs:
        assert c.engine == "qgadmm"
        assert (c.rho, c.alpha, c.bits, c.local_epochs, c.lr, c.epsilon, c.checkpoint_every) == (
            5.0, 1.0, 2, 50, 0.01, 0.001, 5)
        assert c.seed == 42 and c.checkpoint_dir == "/checkpoints"
        assert c.training.rounds == 400
        assert c.timing.heartbeat_interval_s == 5
        # no chain written in the file: the directory places the hospital
        assert c.chain == () and c.neighbours == ()
        assert c.directory.url == "https://directory:8100"
        assert c.directory.credential_ref == f"file:/pki/{c.node_id}/credential", "each hospital its own credential"
        assert c.tls and c.signing_key == f"/pki/{c.node_id}/signing.key"
        assert c.advertise_url == f"https://{c.node_id}:{c.port}"
    assert len({c.shared_secret for c in cfgs}) == 1, "all nodes must share one secret"


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


STATIC = """node_id: hospital-b
port: 8002
advertise_url: http://hospital-b:8002
chain: [hospital-a, hospital-b, hospital-c]
neighbours:
  - id: hospital-a
    url: http://hospital-a:8001
  - id: hospital-c
    url: http://hospital-c:8003
shared_secret: s
data_slice: s.csv
engine: qgadmm
"""


def test_peer_urls_and_advertise_url_come_from_the_environment(monkeypatch, tmp_path):
    """The same YAML points at real hosts (LAN IP, Tailscale name) with no edit, only env."""
    p = tmp_path / "b.yaml"
    p.write_text(STATIC)
    monkeypatch.setenv("MEDCHAIN_PEER_HOSPITAL_A_URL", "http://192.168.1.20:8001/")
    monkeypatch.setenv("MEDCHAIN_PEER_HOSPITAL_C_URL", "http://hospital-c.tail1234.ts.net:8003")
    monkeypatch.setenv("MEDCHAIN_ADVERTISE_URL", "http://hospital-b.tail1234.ts.net:8002")
    c = load_config(p)
    assert c.neighbour("hospital-a").url == "http://192.168.1.20:8001"
    assert c.neighbour("hospital-c").url == "http://hospital-c.tail1234.ts.net:8003"
    assert c.advertise_url == "http://hospital-b.tail1234.ts.net:8002"
    monkeypatch.setenv("MEDCHAIN_PEER_HOSPITAL_A_URL", "  ")  # blank: the YAML address stands
    assert load_config(p).neighbour("hospital-a").url == "http://hospital-a:8001"


def test_directory_and_advertise_urls_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("MEDCHAIN_DIRECTORY_URL", "https://100.101.1.99:8100/")
    monkeypatch.setenv("MEDCHAIN_ADVERTISE_URL", "https://hospital-b.tail1234.ts.net:8002")
    c = load_config(ROOT / "config" / "hospital-b.yaml")
    assert c.directory.url == "https://100.101.1.99:8100"
    assert c.advertise_url == "https://hospital-b.tail1234.ts.net:8002"
    a = load_config(ROOT / "config" / "hospital-a.yaml")  # an override applies to this process only
    assert a.directory.url == "https://100.101.1.99:8100"


def test_url_schemes_must_match_tls(monkeypatch, tmp_path):
    p = tmp_path / "b.yaml"
    p.write_text(STATIC.replace("http://hospital-c:8003", "https://hospital-c:8003"))
    with pytest.raises(ValueError, match="must be http://"):
        load_config(p)
    monkeypatch.setenv("MEDCHAIN_ADVERTISE_URL", "http://hospital-b:8002")
    with pytest.raises(ValueError, match="must be https://"):
        load_config(ROOT / "config" / "hospital-b.yaml")


def test_a_directory_config_carries_no_static_chain(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text((ROOT / "config" / "hospital-a.yaml").read_text() + "chain: [hospital-a, hospital-b]\n")
    with pytest.raises(ValueError, match="directory sets the chain"):
        load_config(p)
    p.write_text((ROOT / "config" / "hospital-a.yaml").read_text().replace("  credential_file: /pki/hospital-a/credential\n", ""))
    with pytest.raises(ValueError, match="credential"):
        load_config(p)


def test_peer_url_env_names():
    assert peer_url_env("hospital-b") == "MEDCHAIN_PEER_HOSPITAL_B_URL"
    assert peer_url_env("st.mary's--east") == "MEDCHAIN_PEER_ST_MARY_S_EAST_URL"


def test_rejects_an_override_that_is_not_a_url(monkeypatch, tmp_path):
    p = tmp_path / "b.yaml"
    p.write_text(STATIC)
    monkeypatch.setenv("MEDCHAIN_PEER_HOSPITAL_A_URL", "192.168.1.21:8001")
    with pytest.raises(ValueError, match="MEDCHAIN_PEER_HOSPITAL_A_URL"):
        load_config(p)
