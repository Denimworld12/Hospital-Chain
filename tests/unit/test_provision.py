"""scripts/provision.py: one self-contained kit per hospital plus the directory's, all on TLS."""

import hashlib
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml
from cryptography import x509

import provision
from node.config import load_config
from node.security import load_public_key, load_signing_key, public_key_b64

ROOT = Path(__file__).resolve().parents[2]

PEERS = ("hospital-a=100.101.1.10:8001,"
         "hospital-b=hospital-b.tail1234.ts.net:8002,"
         "hospital-c=https://hospital-c.example.org:9443")
DIRECTORY = "100.101.1.99:8100"
SECRET = "kit-test-secret"


def run(tmp_path, *extra):
    return provision.main(["--peers", PEERS, "--directory", DIRECTORY, "--secret", SECRET, "--out", str(tmp_path),
                           *extra])


def sans(crt: Path) -> set[str]:
    ext = x509.load_pem_x509_certificate(crt.read_bytes()).extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return {str(v) for v in ext.value.get_values_for_type(x509.DNSName)} | {
        str(v) for v in ext.value.get_values_for_type(x509.IPAddress)}


def kit_config(kit: Path, monkeypatch):
    """The config the kit's node loads inside deploy/node.yml: its `.env` plus the file's container paths."""
    env = provision.container_env(provision.read_env((kit / ".env").read_text()))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return load_config(kit / "config" / "node.yaml")


def test_kit_has_every_file_with_the_real_addresses_and_only_its_own_keys(tmp_path, monkeypatch):
    assert run(tmp_path, "--hospital", "hospital-b", "--zip") == 0
    kit = tmp_path / "hospital-b"
    for rel in ("README.md", ".env", "deploy/node.yml", "config/node.yaml", "data/hospital-b.csv",
                "pki/ca.crt", "pki/directory/signing.pub", "pki/hospital-b/tls.crt", "pki/hospital-b/tls.key",
                "pki/hospital-b/signing.key", "pki/hospital-b/credential",
                "pki/observer/tls.crt", "pki/observer/token"):
        assert (kit / rel).is_file(), rel
    # the one-file deployment, not a compose file of its own, and no build context: the image is published
    assert sorted(p.name for p in kit.iterdir()) == [".env", "README.md", "config", "data", "deploy", "pki"]
    assert (kit / "deploy/node.yml").read_bytes() == (ROOT / "deploy/node.yml").read_bytes()
    assert [p.name for p in (kit / "data").iterdir()] == ["hospital-b.csv"], "only its own slice"
    assert not (tmp_path / "hospital-a").exists() and not (tmp_path / "directory").exists()
    # no other party's private key, and not the CA key or the directory's signing key
    assert sorted(p.name for p in (kit / "pki").iterdir()) == ["ca.crt", "directory", "hospital-b", "observer"]
    # the directory's public halves only: its document key, and its certificate (this kit pins its TLS key)
    assert sorted(p.name for p in (kit / "pki" / "directory").iterdir()) == ["signing.pub", "tls.crt"]
    # the observer's certificate (pinned) and its ingest token, not its private key
    assert sorted(p.name for p in (kit / "pki" / "observer").iterdir()) == ["tls.crt", "token"]
    assert (kit / "data" / "hospital-b.csv").read_bytes() == (ROOT / "data/slices/hospital-b.csv").read_bytes()

    # `.env` is laid out like deploy/node.env.example: the same variables, a Tailscale and a hotspot block
    env = provision.read_env((kit / ".env").read_text())
    assert set(env) == set(provision.read_env((ROOT / "deploy/node.env.example").read_text()))
    assert env["NODE_ID"] == "hospital-b" and env["NODE_PORT"] == "8002"
    assert env["ADVERTISE_ADDR"] == "hospital-b.tail1234.ts.net:8002"
    assert env["IMAGE_VERSION"] == provision.IMAGE_VERSION and "MEDCHAIN_IMAGE" not in env
    text = (kit / ".env").read_text()
    assert "Tailscale" in text and "#DIRECTORY_URL=https://HUB-HOTSPOT-IP:8100" in text
    assert "#ADVERTISE_ADDR=THIS-LAPTOPS-HOTSPOT-IP:8002" in text

    cfg = kit_config(kit, monkeypatch)
    assert cfg.node_id == "hospital-b" and cfg.display_name == "Hospital B"
    assert cfg.advertise_url == "https://hospital-b.tail1234.ts.net:8002"
    assert cfg.port == 8002 and cfg.host == "0.0.0.0"
    assert cfg.chain == () and cfg.neighbours == (), "the directory places the hospital"
    assert cfg.directory.url == "https://100.101.1.99:8100"
    assert cfg.directory.credential_ref == "file:/pki/hospital-b/credential"
    assert cfg.directory.tls_pin_ref == "file:/pki/directory/tls.crt"
    assert cfg.tls.ca == "/pki/ca.crt" and cfg.signing_key == "/pki/hospital-b/signing.key"
    assert cfg.shared_secret == SECRET and cfg.data_slice == "/data/hospital-b.csv"
    assert cfg.checkpoint_dir == "/data/checkpoints" and cfg.state_dir == "/data"
    assert cfg.image_version == provision.IMAGE_VERSION
    # telemetry is pushed to the observer on the hub (the directory's host by default), pinned, with its token
    assert cfg.observer.url == "https://100.101.1.99:8200"
    assert cfg.observer.token_ref == "file:/pki/observer/token"
    assert cfg.observer.tls_pin_ref == "file:/pki/observer/tls.crt"
    assert {"100.101.1.99", "localhost"} <= sans(kit / "pki/observer/tls.crt")
    repo = load_config(ROOT / "config" / "hospital-b.yaml")
    assert (cfg.engine, cfg.rho, cfg.bits, cfg.training) == (repo.engine, repo.rho, repo.bits, repo.training)
    assert cfg.timing.round_timeout_s == 15, "a real network gets a longer round timeout"
    # config/node.yaml carries the engine only: nothing about where the node runs or its keys
    engine = yaml.safe_load((kit / "config/node.yaml").read_text())
    assert not set(engine) & {"node_id", "port", "advertise_url", "directory", "tls", "shared_secret", "data_slice"}
    # its certificate is for the address its peers use; its signing key pair matches
    assert {"hospital-b.tail1234.ts.net", "localhost", "127.0.0.1"} <= sans(kit / "pki/hospital-b/tls.crt")
    key = load_signing_key(kit / "pki/hospital-b/signing.key")
    assert public_key_b64(key) == public_key_b64(load_public_key(kit / "pki/hospital-b/signing.pub"))

    readme = (kit / "README.md").read_text()
    assert "docker compose --project-directory . -f deploy/node.yml up -d" in readme
    assert "exec node python scripts/preflight.py" in readme and "exec node python -m node.leave" in readme
    for text in ("https://100.101.1.99:8100", "https://100.101.1.99:8200", SECRET, "docs/MULTI_LAPTOP.md"):
        assert text in readme
    with zipfile.ZipFile(tmp_path / "hospital-b.zip") as z:
        assert {"hospital-b/README.md", "hospital-b/.env", "hospital-b/deploy/node.yml",
                "hospital-b/pki/hospital-b/signing.key"} <= set(z.namelist())


def test_one_run_provisions_every_hospital_and_the_hub(tmp_path, monkeypatch):
    assert provision.main(["--peers", PEERS, "--directory", DIRECTORY, "--out", str(tmp_path),
                           "--image", "registry.example:5000/medchain-node:1"]) == 0
    ids = ("hospital-a", "hospital-b", "hospital-c")
    cfgs = {n: kit_config(tmp_path / n, monkeypatch) for n in ids}
    assert len({c.shared_secret for c in cfgs.values()}) == 1 and cfgs["hospital-a"].shared_secret != "medchain-demo-secret"
    assert cfgs["hospital-c"].port == 9443
    env = provision.read_env((tmp_path / "hospital-a/.env").read_text())
    assert (env["MEDCHAIN_IMAGE"], env["IMAGE_VERSION"]) == ("registry.example:5000/medchain-node", "1")
    creds = {n: (tmp_path / n / "pki" / n / "credential").read_text().strip() for n in ids}
    assert len(set(creds.values())) == 3, "each hospital has its own credential"
    # the directory kit: its allow-list holds only hashes, in placement order
    d = tmp_path / "directory"
    allowed = yaml.safe_load((d / "pki/directory/hospitals.yaml").read_text())
    assert [h["id"] for h in allowed] == list(ids)
    assert all(h["credential_sha256"] == hashlib.sha256(creds[h["id"]].encode()).hexdigest() for h in allowed)
    assert not any((d / "pki" / n).exists() for n in ids), "the directory gets no hospital's private keys"
    dcfg = yaml.safe_load((d / "config/directory.yaml").read_text())
    assert dcfg["port"] == 8100 and dcfg["hospitals_file"] == "/pki/directory/hospitals.yaml"
    # the hub signs join-code hospitals' certificates: its kit, and only its kit, holds the CA key
    assert dcfg["enrollment"]["ca_key"] == "/pki/ca.key" and dcfg["enrollment"]["observer_port"] == 8200
    assert (d / "pki/ca.key").read_bytes() == (tmp_path / ".pki/ca.key").read_bytes()
    assert not any((tmp_path / n / "pki/ca.key").exists() for n in ids)
    assert {"100.101.1.99", "localhost"} <= sans(d / "pki/directory/tls.crt")
    # its hub.env runs deploy/hub.yml: laid out like the example, listening on the hub's Tailscale IP
    hub = provision.read_env((d / "hub.env").read_text())
    assert set(hub) - {"MEDCHAIN_IMAGE"} == set(provision.read_env((ROOT / "deploy/hub.env.example").read_text()))
    assert hub["HUB_KIT"] == str(d.resolve()) and hub["MEDCHAIN_SHARED_SECRET"] == cfgs["hospital-a"].shared_secret
    assert (hub["DIRECTORY_PORT"], hub["OBSERVER_PORT"], hub["HUB_BIND_IP"]) == ("8100", "8200", "100.101.1.99")
    assert "#HUB_BIND_IP=0.0.0.0" in (d / "hub.env").read_text()
    assert not list(d.glob("*.yaml")) and not (d / "source").exists(), "the hub runs deploy/hub.yml from the repo"
    # every kit trusts the same CA and the same directory key
    assert len({(tmp_path / n / "pki/ca.crt").read_bytes() for n in [*ids, "directory"]}) == 1
    assert len({(tmp_path / n / "pki/directory/signing.pub").read_bytes() for n in [*ids, "directory"]}) == 1
    # every kit shares one observer token and pins one observer key; only the hub kit can run the observer
    tokens = {(tmp_path / n / "pki/observer/token").read_text() for n in [*ids, "directory"]}
    assert len(tokens) == 1 and tokens != {""}
    assert len({(tmp_path / n / "pki/observer/tls.crt").read_bytes() for n in [*ids, "directory"]}) == 1
    assert (d / "pki/observer/tls.key").is_file() and not (tmp_path / "hospital-a" / "pki/observer/tls.key").exists()


def test_hub_kit_inside_the_repo_is_named_relative_to_it_and_a_magicdns_hub_listens_everywhere(tmp_path):
    out = ROOT / "kits" / f"test-{tmp_path.name}"
    try:
        assert provision.main(["--peers", PEERS, "--directory", "hub.tail1234.ts.net:8100", "--out", str(out),
                               "--hospital", "directory", "--pki", str(tmp_path / "pki")]) == 0
        hub = provision.read_env((out / "directory/hub.env").read_text())
        assert hub["HUB_KIT"] == f"./kits/test-{tmp_path.name}/directory"
        assert hub["HUB_BIND_IP"] == "0.0.0.0", "a name is not an address to listen on"
        assert provision.main(["--peers", PEERS, "--directory", "hub.tail1234.ts.net:8100", "--out", str(out),
                               "--hospital", "directory", "--pki", str(tmp_path / "pki"), "--force",
                               "--bind-ip", "100.101.1.99"]) == 0
        assert provision.read_env((out / "directory/hub.env").read_text())["HUB_BIND_IP"] == "100.101.1.99"
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_observer_address_and_token_flags(tmp_path, monkeypatch):
    assert run(tmp_path, "--hospital", "hospital-a", "--observer", "hub.tail1234.ts.net:9200",
               "--observer-token", "tok-1") == 0
    kit = tmp_path / "hospital-a"
    cfg = kit_config(kit, monkeypatch)
    assert cfg.observer.url == "https://hub.tail1234.ts.net:9200"
    assert (kit / "pki/observer/token").read_text().strip() == "tok-1"
    assert "hub.tail1234.ts.net" in sans(kit / "pki/observer/tls.crt")
    # the token is kept with the network: a later run without --observer-token reuses it
    assert run(tmp_path / "again", "--hospital", "hospital-b", "--pki", str(tmp_path / ".pki")) == 0
    assert (tmp_path / "again/hospital-b/pki/observer/token").read_text().strip() == "tok-1"
    assert provision.main(["--peers", PEERS, "--directory", DIRECTORY, "--observer", "http://hub:8200",
                           "--out", str(tmp_path / "bad")]) == 2


def test_the_node_yml_environment_and_the_example_env_load_as_a_node(monkeypatch):
    """deploy/node.yml + deploy/node.env.example, as Compose would hand them to the node (no files are read)."""
    env = provision.container_env(provision.read_env((ROOT / "deploy/node.env.example").read_text()))
    assert env["TLS_CERT"] == "/pki/hospital-b/tls.crt" and env["DATA_PATH"] == "/data"
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NODE_CONFIG")  # the kit's config/node.yaml only adds the engine settings
    cfg = load_config()
    assert cfg.advertise_url == "https://hospital-b-laptop.tail1234.ts.net:8002" and cfg.port == 8002
    assert cfg.directory.url == "https://organiser-laptop.tail1234.ts.net:8100" and cfg.observer is not None
    with pytest.raises(ValueError, match="NODE_ID"):
        provision.interpolate("${NODE_ID:?set NODE_ID}", {})


def test_adding_a_hospital_later_reuses_the_network_and_admits_it(tmp_path, monkeypatch):
    assert run(tmp_path) == 0
    ca = (tmp_path / ".pki" / "ca.crt").read_bytes()
    a_key = (tmp_path / ".pki" / "hospital-a" / "signing.key").read_bytes()
    data = tmp_path / "e.csv"
    data.write_bytes((ROOT / "data/slices/hospital-e.csv").read_bytes())
    peers = PEERS + ",hospital-e=10.0.0.5:8105"
    assert provision.main(["--peers", peers, "--directory", DIRECTORY, "--hospital", "hospital-e", "--secret", SECRET,
                           "--data", f"hospital-e={data}", "--out", str(tmp_path)]) == 0
    assert (tmp_path / ".pki" / "ca.crt").read_bytes() == ca, "same CA"
    assert (tmp_path / ".pki" / "hospital-a" / "signing.key").read_bytes() == a_key, "nobody else is re-keyed"
    assert (tmp_path / "hospital-e" / "pki" / "ca.crt").read_bytes() == ca
    cfg = kit_config(tmp_path / "hospital-e", monkeypatch)
    assert (cfg.node_id, cfg.port, cfg.engine) == ("hospital-e", 8105, "qgadmm")
    allowed = yaml.safe_load((tmp_path / ".pki" / "directory" / "hospitals.yaml").read_text())
    assert [h["id"] for h in allowed] == ["hospital-a", "hospital-b", "hospital-c", "hospital-e"]
    # a new hospital without a repo config needs a port in its address
    assert provision.main(["--peers", PEERS + ",hospital-z=10.0.0.9", "--directory", DIRECTORY, "--hospital",
                           "hospital-z", "--data", f"hospital-z={data}", "--out", str(tmp_path / "z")]) == 2


def test_a_changed_address_gets_a_new_certificate(tmp_path):
    assert run(tmp_path, "--hospital", "hospital-a") == 0
    moved = PEERS.replace("100.101.1.10", "100.101.1.77")
    assert provision.main(["--peers", moved, "--directory", DIRECTORY, "--secret", SECRET, "--hospital", "hospital-a",
                           "--out", str(tmp_path), "--force"]) == 0
    assert "100.101.1.77" in sans(tmp_path / "hospital-a" / "pki/hospital-a/tls.crt")


@pytest.mark.parametrize("peers, extra", [
    ("hospital-a=10.0.0.1:8001", []),                                              # not a chain
    ("hospital-a=10.0.0.1:8001,hospital-a=10.0.0.2:8001", []),                    # duplicate id
    ("hospital-a=ftp://10.0.0.1:8001,hospital-b=10.0.0.2:8002", []),              # not https
    ("hospital-a=http://10.0.0.1:8001,hospital-b=10.0.0.2:8002", []),             # plain HTTP is not allowed
    ("hospital-a=10.0.0.1:8001,directory=10.0.0.2:8002", []),                     # reserved name
    ("hospital-a=10.0.0.1:8001,hospital-b=10.0.0.2:8002", ["--hospital", "hospital-c"]),
])
def test_rejects_bad_peer_lists(tmp_path, peers, extra):
    assert provision.main(["--peers", peers, "--directory", DIRECTORY, "--out", str(tmp_path),
                           *extra]) == 2


def test_rejects_a_plain_http_directory(tmp_path):
    assert provision.main(["--peers", PEERS, "--directory", "http://10.0.0.9:8100", "--out", str(tmp_path)]) == 2


def test_refuses_to_overwrite_a_kit_unless_forced(tmp_path):
    assert run(tmp_path, "--hospital", "hospital-a") == 0
    assert run(tmp_path, "--hospital", "hospital-a") == 2
    assert run(tmp_path, "--hospital", "hospital-a", "--force") == 0
