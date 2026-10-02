"""scripts/provision.py: one self-contained kit per hospital, with the real peer addresses filled in."""

import zipfile
from pathlib import Path

import pytest
import yaml

import provision
from node.config import load_config

ROOT = Path(__file__).resolve().parents[2]

PEERS = ("hospital-a=http://100.101.1.10:8001,"
         "hospital-b=hospital-b.tail1234.ts.net:8002,"
         "hospital-c=http://hospital-c.example.org:9443")
SECRET = "kit-test-secret"


def run(tmp_path, *extra):
    return provision.main(["--peers", PEERS, "--secret", SECRET, "--out", str(tmp_path), *extra])


def test_kit_has_every_file_with_the_real_addresses_substituted(tmp_path):
    assert run(tmp_path, "--hospital", "hospital-b", "--zip",
               "--observer-db", "postgresql://medchain:medchain@100.101.1.99:5432/medchain") == 0
    kit = tmp_path / "hospital-b"
    for rel in ("README.md", "docker-compose.hospital.yaml", "config/hospital-b.yaml", "data/hospital-b.csv",
                "source/node/Dockerfile", "source/node/requirements.txt", "source/node/__main__.py",
                "source/models/ecg-resnet18.onnx", "source/data/cardio/cardio_train.csv"):
        assert (kit / rel).is_file(), rel
    assert not list(kit.rglob("__pycache__"))
    assert not (tmp_path / "hospital-a").exists(), "only the requested hospital is provisioned"
    assert (kit / "data" / "hospital-b.csv").read_bytes() == (ROOT / "data/slices/hospital-b.csv").read_bytes()

    cfg = load_config(kit / "config" / "hospital-b.yaml")
    assert cfg.node_id == "hospital-b" and cfg.display_name == "Hospital B"
    assert cfg.advertise_url == "http://hospital-b.tail1234.ts.net:8002"
    assert cfg.port == 8002 and cfg.host == "0.0.0.0"
    assert cfg.chain == ("hospital-a", "hospital-b", "hospital-c")
    assert {n.id: n.url for n in cfg.neighbours} == {"hospital-a": "http://100.101.1.10:8001",
                                                     "hospital-c": "http://hospital-c.example.org:9443"}
    assert cfg.shared_secret == SECRET and cfg.data_slice == "/data/hospital-b.csv"
    # engine settings come from the repo's own config for that hospital
    repo = load_config(ROOT / "config" / "hospital-b.yaml")
    assert (cfg.engine, cfg.rho, cfg.bits, cfg.training, cfg.timing) == (
        repo.engine, repo.rho, repo.bits, repo.training, repo.timing)

    compose = yaml.safe_load((kit / "docker-compose.hospital.yaml").read_text())
    assert list(compose["services"]) == ["hospital-b"]
    svc = compose["services"]["hospital-b"]
    assert svc["build"] == {"context": "./source", "dockerfile": "node/Dockerfile"}
    assert svc["ports"] == ["8002:8002"]
    assert svc["environment"] == {"NODE_CONFIG": "/config/hospital-b.yaml",
                                  "DATABASE_URL": "postgresql://medchain:medchain@100.101.1.99:5432/medchain"}
    assert "./config/hospital-b.yaml:/config/hospital-b.yaml:ro" in svc["volumes"]
    assert "./data/hospital-b.csv:/data/hospital-b.csv:ro" in svc["volumes"]

    readme = (kit / "README.md").read_text()
    assert "docker compose -f docker-compose.hospital.yaml up -d --build --wait" in readme
    for addr in ("http://100.101.1.10:8001", "http://hospital-b.tail1234.ts.net:8002",
                 "http://hospital-c.example.org:9443", "MEDCHAIN_PEER_HOSPITAL_A_URL", SECRET):
        assert addr in readme

    with zipfile.ZipFile(tmp_path / "hospital-b.zip") as z:
        names = set(z.namelist())
    assert {"hospital-b/README.md", "hospital-b/docker-compose.hospital.yaml",
            "hospital-b/config/hospital-b.yaml", "hospital-b/data/hospital-b.csv"} <= names


def test_one_run_provisions_the_whole_chain_consistently(tmp_path):
    assert provision.main(["--peers", PEERS, "--out", str(tmp_path), "--image", "registry.example/medchain-node:1",
                           "--bind-ip", "100.101.1.10"]) == 0
    cfgs = {n: load_config(tmp_path / n / "config" / f"{n}.yaml") for n in ("hospital-a", "hospital-b", "hospital-c")}
    assert len({c.shared_secret for c in cfgs.values()}) == 1, "a generated secret is shared by every kit"
    assert cfgs["hospital-a"].shared_secret != "medchain-demo-secret"
    for c in cfgs.values():
        for n in c.neighbours:  # every node reaches each neighbour at the address that neighbour advertises
            assert n.url == cfgs[n.id].advertise_url
    assert [n.id for n in cfgs["hospital-a"].neighbours] == ["hospital-b"]
    assert cfgs["hospital-c"].port == 9443
    svc = yaml.safe_load((tmp_path / "hospital-a" / "docker-compose.hospital.yaml").read_text())["services"]["hospital-a"]
    assert svc["image"] == "registry.example/medchain-node:1" and "build" not in svc
    assert svc["ports"] == ["100.101.1.10:8001:8001"]
    assert "DATABASE_URL" not in svc["environment"]
    assert not (tmp_path / "hospital-a" / "source").exists(), "--image ships no build context"


def test_a_new_hospital_uses_the_template_and_needs_a_port(tmp_path):
    peers = "hospital-a=http://10.0.0.1:8001,hospital-d=http://10.0.0.4:8104"
    data = tmp_path / "d.csv"
    data.write_bytes((ROOT / "data/slices/hospital-c.csv").read_bytes())
    assert provision.main(["--peers", peers, "--hospital", "hospital-d", "--secret", SECRET,
                           "--data", f"hospital-d={data}", "--image", "x", "--out", str(tmp_path)]) == 0
    cfg = load_config(tmp_path / "hospital-d" / "config" / "hospital-d.yaml")
    assert (cfg.node_id, cfg.port, cfg.chain) == ("hospital-d", 8104, ("hospital-a", "hospital-d"))
    assert cfg.engine == "qgadmm"
    assert provision.main(["--peers", "hospital-a=http://10.0.0.1:8001,hospital-d=http://10.0.0.4",
                           "--hospital", "hospital-d", "--data", f"hospital-d={data}", "--out", str(tmp_path / "x")]) == 2


@pytest.mark.parametrize("peers, extra", [
    ("hospital-a=http://10.0.0.1:8001", []),                                     # not a chain
    ("hospital-a=http://10.0.0.1:8001,hospital-a=http://10.0.0.2:8001", []),    # duplicate id
    ("hospital-a=ftp://10.0.0.1:8001,hospital-b=http://10.0.0.2:8002", []),     # not http
    ("hospital-a=https://10.0.0.1:8001,hospital-b=http://10.0.0.2:8002", []),   # node serves plain HTTP only
    ("hospital-a=http://10.0.0.1:8001,hospital-b=http://10.0.0.2:8002", ["--hospital", "hospital-c"]),
])
def test_rejects_bad_peer_lists(tmp_path, peers, extra):
    assert provision.main(["--peers", peers, "--out", str(tmp_path), "--image", "x", *extra]) == 2


def test_refuses_to_overwrite_a_kit_unless_forced(tmp_path):
    assert run(tmp_path, "--hospital", "hospital-a", "--image", "x") == 0
    assert run(tmp_path, "--hospital", "hospital-a", "--image", "x") == 2
    assert run(tmp_path, "--hospital", "hospital-a", "--image", "x", "--force") == 0
