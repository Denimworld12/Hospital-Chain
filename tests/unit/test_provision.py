"""scripts/provision.py: one self-contained kit per hospital plus the directory's, all on TLS."""

import hashlib
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


def test_kit_has_every_file_with_the_real_addresses_and_only_its_own_keys(tmp_path):
    assert run(tmp_path, "--hospital", "hospital-b", "--zip",
               "--observer-db", "postgresql://medchain:medchain@100.101.1.99:5432/medchain") == 0
    kit = tmp_path / "hospital-b"
    for rel in ("README.md", "docker-compose.hospital.yaml", "config/hospital-b.yaml", "data/hospital-b.csv",
                "pki/ca.crt", "pki/directory/signing.pub", "pki/hospital-b/tls.crt", "pki/hospital-b/tls.key",
                "pki/hospital-b/signing.key", "pki/hospital-b/credential",
                "source/node/Dockerfile", "source/node/requirements.txt", "source/node/__main__.py",
                "source/directory/app.py", "source/scripts/pki.py",
                "source/models/ecg-resnet18.onnx", "source/data/cardio/cardio_train.csv"):
        assert (kit / rel).is_file(), rel
    assert not list(kit.rglob("__pycache__"))
    assert not (tmp_path / "hospital-a").exists() and not (tmp_path / "directory").exists()
    # no other party's private key, and not the CA key or the directory's signing key
    assert sorted(p.name for p in (kit / "pki").iterdir()) == ["ca.crt", "directory", "hospital-b"]
    assert [p.name for p in (kit / "pki" / "directory").iterdir()] == ["signing.pub"]
    assert (kit / "data" / "hospital-b.csv").read_bytes() == (ROOT / "data/slices/hospital-b.csv").read_bytes()

    cfg = load_config(kit / "config" / "hospital-b.yaml")
    assert cfg.node_id == "hospital-b" and cfg.display_name == "Hospital B"
    assert cfg.advertise_url == "https://hospital-b.tail1234.ts.net:8002"
    assert cfg.port == 8002 and cfg.host == "0.0.0.0"
    assert cfg.chain == () and cfg.neighbours == (), "the directory places the hospital"
    assert cfg.directory.url == "https://100.101.1.99:8100"
    assert cfg.directory.credential_ref == "file:/pki/hospital-b/credential"
    assert cfg.tls.ca == "/pki/ca.crt" and cfg.signing_key == "/pki/hospital-b/signing.key"
    assert cfg.shared_secret == SECRET and cfg.data_slice == "/data/hospital-b.csv"
    repo = load_config(ROOT / "config" / "hospital-b.yaml")
    assert (cfg.engine, cfg.rho, cfg.bits, cfg.training, cfg.timing) == (
        repo.engine, repo.rho, repo.bits, repo.training, repo.timing)
    # its certificate is for the address its peers use; its signing key pair matches
    assert {"hospital-b.tail1234.ts.net", "localhost", "127.0.0.1"} <= sans(kit / "pki/hospital-b/tls.crt")
    key = load_signing_key(kit / "pki/hospital-b/signing.key")
    assert public_key_b64(key) == public_key_b64(load_public_key(kit / "pki/hospital-b/signing.pub"))

    compose = yaml.safe_load((kit / "docker-compose.hospital.yaml").read_text())
    svc = compose["services"]["hospital-b"]
    assert list(compose["services"]) == ["hospital-b"]
    assert svc["build"] == {"context": "./source", "dockerfile": "node/Dockerfile"} and svc["ports"] == ["8002:8002"]
    assert svc["environment"] == {"NODE_CONFIG": "/config/hospital-b.yaml",
                                  "DATABASE_URL": "postgresql://medchain:medchain@100.101.1.99:5432/medchain"}
    assert {"./config/hospital-b.yaml:/config/hospital-b.yaml:ro", "./data/hospital-b.csv:/data/hospital-b.csv:ro",
            "./pki:/pki:ro"} <= set(svc["volumes"])

    readme = (kit / "README.md").read_text()
    assert "docker compose -f docker-compose.hospital.yaml up -d --build --wait" in readme
    assert "python -m node.leave" in readme and "MEDCHAIN_DIRECTORY_URL" in readme
    for text in ("https://hospital-b.tail1234.ts.net:8002", "https://100.101.1.99:8100", SECRET):
        assert text in readme
    with zipfile.ZipFile(tmp_path / "hospital-b.zip") as z:
        assert {"hospital-b/README.md", "hospital-b/pki/hospital-b/signing.key"} <= set(z.namelist())


def test_one_run_provisions_every_hospital_and_the_directory(tmp_path):
    assert provision.main(["--peers", PEERS, "--directory", DIRECTORY, "--out", str(tmp_path),
                           "--image", "registry.example/medchain-node:1", "--bind-ip", "100.101.1.10"]) == 0
    ids = ("hospital-a", "hospital-b", "hospital-c")
    cfgs = {n: load_config(tmp_path / n / "config" / f"{n}.yaml") for n in ids}
    assert len({c.shared_secret for c in cfgs.values()}) == 1 and cfgs["hospital-a"].shared_secret != "medchain-demo-secret"
    assert cfgs["hospital-c"].port == 9443
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
    svc = yaml.safe_load((d / "docker-compose.directory.yaml").read_text())["services"]["directory"]
    assert svc["command"] == ["python", "-m", "directory"] and svc["image"] == "registry.example/medchain-node:1"
    assert {"100.101.1.99", "localhost"} <= sans(d / "pki/directory/tls.crt")
    # every kit trusts the same CA and the same directory key
    assert len({(tmp_path / n / "pki/ca.crt").read_bytes() for n in [*ids, "directory"]}) == 1
    assert len({(tmp_path / n / "pki/directory/signing.pub").read_bytes() for n in [*ids, "directory"]}) == 1
    svc = yaml.safe_load((tmp_path / "hospital-a" / "docker-compose.hospital.yaml").read_text())["services"]["hospital-a"]
    assert svc["image"] == "registry.example/medchain-node:1" and "build" not in svc
    assert svc["ports"] == ["100.101.1.10:8001:8001"] and "DATABASE_URL" not in svc["environment"]
    assert not (tmp_path / "hospital-a" / "source").exists(), "--image ships no build context"


def test_adding_a_hospital_later_reuses_the_network_and_admits_it(tmp_path):
    assert run(tmp_path, "--image", "x") == 0
    ca = (tmp_path / ".pki" / "ca.crt").read_bytes()
    a_key = (tmp_path / ".pki" / "hospital-a" / "signing.key").read_bytes()
    data = tmp_path / "e.csv"
    data.write_bytes((ROOT / "data/slices/hospital-e.csv").read_bytes())
    peers = PEERS + ",hospital-e=10.0.0.5:8105"
    assert provision.main(["--peers", peers, "--directory", DIRECTORY, "--hospital", "hospital-e", "--secret", SECRET,
                           "--data", f"hospital-e={data}", "--image", "x", "--out", str(tmp_path)]) == 0
    assert (tmp_path / ".pki" / "ca.crt").read_bytes() == ca, "same CA"
    assert (tmp_path / ".pki" / "hospital-a" / "signing.key").read_bytes() == a_key, "nobody else is re-keyed"
    assert (tmp_path / "hospital-e" / "pki" / "ca.crt").read_bytes() == ca
    cfg = load_config(tmp_path / "hospital-e" / "config" / "hospital-e.yaml")
    assert (cfg.node_id, cfg.port, cfg.engine) == ("hospital-e", 8105, "qgadmm")
    allowed = yaml.safe_load((tmp_path / ".pki" / "directory" / "hospitals.yaml").read_text())
    assert [h["id"] for h in allowed] == ["hospital-a", "hospital-b", "hospital-c", "hospital-e"]
    # a new hospital without a repo config needs a port in its address
    assert provision.main(["--peers", PEERS + ",hospital-z=10.0.0.9", "--directory", DIRECTORY, "--hospital",
                           "hospital-z", "--data", f"hospital-z={data}", "--out", str(tmp_path / "z")]) == 2


def test_a_changed_address_gets_a_new_certificate(tmp_path):
    assert run(tmp_path, "--hospital", "hospital-a", "--image", "x") == 0
    moved = PEERS.replace("100.101.1.10", "100.101.1.77")
    assert provision.main(["--peers", moved, "--directory", DIRECTORY, "--secret", SECRET, "--hospital", "hospital-a",
                           "--image", "x", "--out", str(tmp_path), "--force"]) == 0
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
    assert provision.main(["--peers", peers, "--directory", DIRECTORY, "--out", str(tmp_path), "--image", "x",
                           *extra]) == 2


def test_rejects_a_plain_http_directory(tmp_path):
    assert provision.main(["--peers", PEERS, "--directory", "http://10.0.0.9:8100", "--out", str(tmp_path)]) == 2


def test_refuses_to_overwrite_a_kit_unless_forced(tmp_path):
    assert run(tmp_path, "--hospital", "hospital-a", "--image", "x") == 0
    assert run(tmp_path, "--hospital", "hospital-a", "--image", "x") == 2
    assert run(tmp_path, "--hospital", "hospital-a", "--image", "x", "--force") == 0
