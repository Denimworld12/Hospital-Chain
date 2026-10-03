"""Run a real chain of node apps over real HTTP in one process (uvicorn threads, no docker).

Each node is the production FastAPI app from node/main.py with its own YAML config,
its own data slice and its own checkpoint directory; only the database is a
recorder instead of Postgres. `NodeServer.stop()` shuts a node down (its state is
gone, its checkpoint stays on disk) and a fresh `NodeServer` on the same config
restarts it, which is how the rejoin path is exercised without docker.

`secure=True` gives every node a TLS certificate from a throwaway CA and an Ed25519
signing key (scripts/pki.py), exactly as the compose stack does. `DirectoryServer`
runs the directory service (directory/) the same way, and `directory_configs` writes
node configs that take their chain from it instead of from the YAML.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn

import pki
from directory.app import create_app as create_directory_app, load_config as load_directory_config
from node import wire
from node.config import load_config
from node.db import NullDatabase
from node.main import create_app

ROOT = Path(__file__).resolve().parents[2]
SECRET = "chain-test-secret"
AUTH = {wire.H_SECRET: SECRET}


class RecordingDatabase(NullDatabase):
    def __init__(self):
        super().__init__()
        self.audits: list[dict] = []
        self.metrics: list[dict] = []
        self.sent: list[dict] = []

    def audit(self, **kw):
        self.audits.append(kw)

    def record_metrics(self, **kw):
        self.metrics.append(kw)

    def update_sent(self, **kw):
        self.sent.append(kw)

    def events(self, name: str) -> list[dict]:
        return [a for a in self.audits if a["event"] == name]


def free_ports(n: int) -> list[int]:
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


def issue_pki(tmp: Path, hospitals: list[str]) -> Path:
    out = tmp / "pki"
    pki.issue(out, ("directory", ["127.0.0.1", "localhost"]), [(h, ["127.0.0.1", "localhost"]) for h in hospitals])
    return out


def security_yaml(pki_dir: Path, node: str) -> str:
    return (f"tls: {{cert: {pki_dir / node / 'tls.crt'}, key: {pki_dir / node / 'tls.key'}, ca: {pki_dir / 'ca.crt'}}}\n"
            f"signing_key: {pki_dir / node / 'signing.key'}\n")


def write_configs(tmp: Path, chain: list[str], *, engine: str = "qgadmm", bits: int = 2, rho: float = 24.0,
                  local_epochs: int = 50, round_timeout_s: float = 2.0, round_pause_s: float = 0.0,
                  checkpoint_every: int = 5, rounds: int = 300, secure: bool = False,
                  slices: dict[str, str] | None = None) -> dict[str, Path]:
    """A static chain (each node's neighbours written in its YAML)."""
    ports = dict(zip(chain, free_ports(len(chain))))
    scheme = "https" if secure else "http"
    pki_dir = issue_pki(tmp, chain) if secure else None
    out = {}
    for i, node in enumerate(chain):
        neighbours = [chain[j] for j in (i - 1, i + 1) if 0 <= j < len(chain)]
        key = (lambda m: f"    public_key: file:{pki_dir / m / 'signing.pub'}\n") if secure else (lambda m: "")
        nb = "".join(f"  - id: {m}\n    url: {scheme}://127.0.0.1:{ports[m]}\n{key(m)}" for m in neighbours)
        p = tmp / f"{node}.yaml"
        p.write_text(
            f"""node_id: {node}
port: {ports[node]}
advertise_url: {scheme}://127.0.0.1:{ports[node]}
chain: [{", ".join(chain)}]
neighbours:
{nb}""" + _common(tmp, node, i, engine=engine, bits=bits, rho=rho, local_epochs=local_epochs, rounds=rounds,
                     round_timeout_s=round_timeout_s, round_pause_s=round_pause_s, checkpoint_every=checkpoint_every,
                     slices=slices) + (security_yaml(pki_dir, node) if secure else ""))
        out[node] = p
    return out


def directory_configs(tmp: Path, hospitals: list[str], *, margin_rounds: int = 3, **kw) -> tuple[Path, dict[str, Path]]:
    """TLS + signed updates + a directory: node configs carry no chain, the directory places them."""
    pki_dir = issue_pki(tmp, hospitals)
    dport, *ports = free_ports(len(hospitals) + 1)
    d = tmp / "directory.yaml"
    d.write_text(f"""port: {dport}
signing_key: {pki_dir / 'directory' / 'signing.key'}
hospitals_file: {pki_dir / 'directory' / 'hospitals.yaml'}
db_path: {tmp / 'directory.sqlite'}
margin_rounds: {margin_rounds}
tls: {{cert: {pki_dir / 'directory' / 'tls.crt'}, key: {pki_dir / 'directory' / 'tls.key'}, ca: {pki_dir / 'ca.crt'}}}
""")
    out = {}
    for i, (node, port) in enumerate(zip(hospitals, ports)):
        p = tmp / f"{node}.yaml"
        p.write_text(f"""node_id: {node}
port: {port}
advertise_url: https://127.0.0.1:{port}
directory:
  url: https://127.0.0.1:{dport}
  credential_file: {pki_dir / node / 'credential'}
  public_key: file:{pki_dir / 'directory' / 'signing.pub'}
""" + _common(tmp, node, i, **kw) + security_yaml(pki_dir, node))
        out[node] = p
    return d, out


def _common(tmp: Path, node: str, i: int, *, engine: str = "qgadmm", bits: int = 2, rho: float = 24.0,
            local_epochs: int = 50, round_timeout_s: float = 2.0, round_pause_s: float = 0.0,
            checkpoint_every: int = 5, rounds: int = 300, slices: dict[str, str] | None = None) -> str:
    data = (slices or {}).get(node, f"hospital-{'abc'[i % 3]}")
    return f"""shared_secret: {SECRET}
data_slice: {ROOT / "data" / "slices" / (data + ".csv")}
engine: {engine}
rho: {rho}
bits: {bits}
local_epochs: {local_epochs}
checkpoint_every: {checkpoint_every}
checkpoint_dir: {tmp / "ckpt"}
training: {{rounds: {rounds}}}
timing: {{heartbeat_interval_s: 5, round_timeout_s: {round_timeout_s}, round_pause_s: {round_pause_s}}}
"""


class _Server:
    """One uvicorn server on a thread (HTTPS when given a certificate)."""

    def _serve(self, app, port: int, cert: str | None = None, key: str | None = None):
        tls = {"ssl_certfile": cert, "ssl_keyfile": key} if cert else {}
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                                    lifespan="on", **tls))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError(f"{self.url} did not start")
            time.sleep(0.02)
        return self

    def stop(self, *, abrupt: bool = False) -> None:
        """Shut down; abrupt=True drops in-flight connections instead of draining them (a kill)."""
        self.server.force_exit = abrupt
        self.server.should_exit = True
        self.thread.join(timeout=20)


class DirectoryServer(_Server):
    def __init__(self, config_path: Path):
        self.cfg = load_directory_config(config_path)
        self.app = create_directory_app(self.cfg)
        self.verify = self.cfg.ca or True
        self._serve(self.app, self.cfg.port, self.cfg.tls_cert, self.cfg.tls_key)

    @property
    def url(self) -> str:
        return f"{'https' if self.cfg.tls_cert else 'http'}://127.0.0.1:{self.cfg.port}"

    @property
    def store(self):
        return self.app.state.store


class NodeServer(_Server):
    def __init__(self, config_path: Path):
        self.cfg = load_config(config_path)
        self.db = RecordingDatabase()
        self.app = create_app(self.cfg, database=self.db)
        self.verify = self.cfg.tls.ca if self.cfg.tls else True
        self._serve(self.app, self.cfg.port, *((self.cfg.tls.cert, self.cfg.tls.key) if self.cfg.tls else ()))

    @property
    def url(self) -> str:
        return f"{self.cfg.scheme}://127.0.0.1:{self.cfg.port}"

    @property
    def runner(self):
        return self.app.state.runner

    @property
    def engine(self):
        return self.runner.engine

    def status(self) -> dict:
        return httpx.get(f"{self.url}/status", timeout=5, verify=self.verify).json()

    def metrics(self) -> dict:
        return httpx.get(f"{self.url}/metrics", timeout=5, verify=self.verify).json()

    def post(self, path: str, **kw) -> httpx.Response:
        return httpx.post(f"{self.url}{path}", headers=AUTH, timeout=15, verify=self.verify, **kw)


def wait_until(pred, timeout: float, what: str, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")
