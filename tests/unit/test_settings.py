"""node/settings.py: environment > YAML > defaults, into the same frozen NodeConfig."""

from pathlib import Path

import pytest

from node.config import NodeConfig, load_config

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = ["NODE_CONFIG", "NODE_ID", "BIND_HOST", "NODE_PORT", "ADVERTISE_ADDR", "MEDCHAIN_ADVERTISE_URL",
            "DIRECTORY_URL", "MEDCHAIN_DIRECTORY_URL", "OBSERVER_URL", "OBSERVER_TOKEN", "OBSERVER_TLS_PIN", "DATA_PATH",
            "ALGORITHM", "BITS", "EPSILON", "ROUND_TIMEOUT_S", "IMAGE_VERSION", "SHARED_SECRET", "TLS_CERT", "TLS_KEY",
            "TLS_CA", "SIGNING_KEY", "DIRECTORY_CREDENTIAL_FILE", "DIRECTORY_PUBLIC_KEY", "DIRECTORY_TLS_PIN",
            "HTTP_CONNECT_TIMEOUT_S", "HTTP_READ_TIMEOUT_S", "ROUND_PAUSE_S", "CHECKPOINT_DIR", "DATA_SLICE"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in SETTINGS:
        monkeypatch.delenv(k, raising=False)


def test_environment_overrides_the_yaml_which_overrides_the_defaults(monkeypatch):
    path = ROOT / "config" / "hospital-b.yaml"
    yaml_only = load_config(path)
    assert isinstance(yaml_only, NodeConfig) and yaml_only.bits == 2 and yaml_only.timing.round_timeout_s == 6
    assert yaml_only.http.connect_timeout_s == 1.5 and yaml_only.image_version == "dev"  # defaults
    monkeypatch.setenv("NODE_CONFIG", str(path))
    for k, v in {"BITS": "4", "EPSILON": "0.0005", "ROUND_TIMEOUT_S": "20", "ROUND_PAUSE_S": "0.5",
                 "ALGORITHM": "gadmm_fp", "IMAGE_VERSION": "v1.0.0", "HTTP_CONNECT_TIMEOUT_S": "4",
                 "HTTP_READ_TIMEOUT_S": "9", "BIND_HOST": "100.101.1.11", "ADVERTISE_ADDR": "100.101.1.11:8002"}.items():
        monkeypatch.setenv(k, v)
    c = load_config()
    assert (c.bits, c.epsilon, c.engine, c.image_version) == (4, 0.0005, "gadmm_fp", "v1.0.0")
    assert (c.timing.round_timeout_s, c.timing.round_pause_s, c.timing.heartbeat_interval_s) == (20, 0.5, 5)
    assert (c.http.connect_timeout_s, c.http.read_timeout_s) == (4, 9)
    assert c.host == "100.101.1.11" and c.advertise_url == "https://100.101.1.11:8002"
    assert (c.node_id, c.rho, c.directory.url) == ("hospital-b", 5.0, "https://directory:8100")  # from the YAML


def test_a_node_configured_from_the_environment_alone(monkeypatch, tmp_path):
    """An `.env` and the kit's key folder are enough: no YAML at all."""
    pki = ROOT / "config"  # any existing files will do for paths that are only read when used
    env = {"NODE_ID": "hospital-b", "SHARED_SECRET": "s", "DATA_PATH": str(tmp_path), "NODE_PORT": "9002",
           "ADVERTISE_ADDR": "hospital-b.tail1234.ts.net", "DIRECTORY_URL": "directory.tail1234.ts.net:8100",
           "DIRECTORY_CREDENTIAL_FILE": "/pki/hospital-b/credential", "DIRECTORY_PUBLIC_KEY": "file:/pki/d.pub",
           "DIRECTORY_TLS_PIN": "sha256:" + "ab" * 32, "TLS_CERT": str(pki / "c.crt"), "TLS_KEY": "k", "TLS_CA": "ca",
           "SIGNING_KEY": "/pki/hospital-b/signing.key", "ALGORITHM": "qgadmm", "BITS": "2",
           "OBSERVER_URL": "hub.tail1234.ts.net:8200", "OBSERVER_TOKEN": "t", "OBSERVER_TLS_PIN": "sha256:" + "cd" * 32}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    c = load_config()
    assert c.port == 9002 and c.advertise_url == "https://hospital-b.tail1234.ts.net:9002", "one NODE_PORT"
    assert c.directory.url == "https://directory.tail1234.ts.net:8100"
    assert c.directory.credential_ref == "file:/pki/hospital-b/credential" and c.directory.tls_pin == "ab" * 32
    assert c.data_slice == str(tmp_path / "hospital-b.csv") and c.checkpoint_dir == str(tmp_path / "checkpoints")
    assert c.state_dir == str(tmp_path)
    assert c.observer.url == "https://hub.tail1234.ts.net:8200" and c.observer.token == "t"
    assert c.observer.tls_pin == "cd" * 32 and c.tls.cert == str(pki / "c.crt")


def test_advertise_addr_is_required_with_a_directory(monkeypatch, tmp_path):
    p = tmp_path / "b.yaml"
    p.write_text((ROOT / "config" / "hospital-b.yaml").read_text().replace("advertise_url: https://hospital-b:8002\n", ""))
    with pytest.raises(ValueError, match="ADVERTISE_ADDR"):
        load_config(p)  # no more falling back to the node id as a hostname
    monkeypatch.setenv("ADVERTISE_ADDR", "192.168.1.20")
    assert load_config(p).advertise_url == "https://192.168.1.20:8002"


def test_the_medchain_names_stay_aliases(monkeypatch):
    monkeypatch.setenv("MEDCHAIN_ADVERTISE_URL", "https://hospital-b.tail1234.ts.net:8002")
    monkeypatch.setenv("MEDCHAIN_DIRECTORY_URL", "https://100.101.1.99:8100")
    c = load_config(ROOT / "config" / "hospital-b.yaml")
    assert c.advertise_url == "https://hospital-b.tail1234.ts.net:8002" and c.directory.url == "https://100.101.1.99:8100"
    monkeypatch.setenv("ADVERTISE_ADDR", "https://other:8002")  # the spec's name wins over the alias
    assert load_config(ROOT / "config" / "hospital-b.yaml").advertise_url == "https://other:8002"


def test_an_observer_needs_a_token_and_with_tls_a_pin(monkeypatch):
    monkeypatch.setenv("OBSERVER_URL", "https://observer:8200")
    with pytest.raises(ValueError, match="token"):
        load_config(ROOT / "config" / "hospital-b.yaml")
    monkeypatch.setenv("OBSERVER_TOKEN", "t")
    with pytest.raises(ValueError, match="tls_pin"):
        load_config(ROOT / "config" / "hospital-b.yaml")
    monkeypatch.setenv("OBSERVER_TLS_PIN", "file:/pki/observer/tls.crt")
    assert load_config(ROOT / "config" / "hospital-b.yaml").observer.tls_pin_ref == "file:/pki/observer/tls.crt"
    monkeypatch.setenv("OBSERVER_URL", "http://observer:8200")
    with pytest.raises(ValueError, match="must be https://"):
        load_config(ROOT / "config" / "hospital-b.yaml")


def test_missing_yaml_named_by_node_config_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("NODE_CONFIG", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError):
        load_config()
