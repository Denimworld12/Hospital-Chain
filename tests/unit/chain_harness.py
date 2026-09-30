"""Run a real chain of node apps over real HTTP in one process (uvicorn threads, no docker).

Each node is the production FastAPI app from node/main.py with its own YAML config,
its own data slice and its own checkpoint directory; only the database is a
recorder instead of Postgres. `NodeServer.stop()` shuts a node down (its state is
gone, its checkpoint stays on disk) and a fresh `NodeServer` on the same config
restarts it, which is how the rejoin path is exercised without docker.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn

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


def write_configs(tmp: Path, chain: list[str], *, engine: str = "qgadmm", bits: int = 2, rho: float = 24.0,
                  local_epochs: int = 50, round_timeout_s: float = 2.0, checkpoint_every: int = 5,
                  slices: dict[str, str] | None = None) -> dict[str, Path]:
    ports = dict(zip(chain, free_ports(len(chain))))
    slices = slices or {n: f"hospital-{'abc'[i % 3]}" for i, n in enumerate(chain)}
    out = {}
    for i, node in enumerate(chain):
        neighbours = [chain[j] for j in (i - 1, i + 1) if 0 <= j < len(chain)]
        nb = "".join(f"  - id: {m}\n    url: http://127.0.0.1:{ports[m]}\n" for m in neighbours)
        p = tmp / f"{node}.yaml"
        p.write_text(
            f"""node_id: {node}
port: {ports[node]}
advertise_url: http://127.0.0.1:{ports[node]}
chain: [{", ".join(chain)}]
neighbours:
{nb}shared_secret: {SECRET}
data_slice: {ROOT / "data" / "slices" / (slices[node] + ".csv")}
engine: {engine}
rho: {rho}
bits: {bits}
local_epochs: {local_epochs}
checkpoint_every: {checkpoint_every}
checkpoint_dir: {tmp / "ckpt"}
training: {{rounds: 300}}
timing: {{heartbeat_interval_s: 5, round_timeout_s: {round_timeout_s}, round_pause_s: 0}}
"""
        )
        out[node] = p
    return out


class NodeServer:
    def __init__(self, config_path: Path):
        self.cfg = load_config(config_path)
        self.db = RecordingDatabase()
        self.app = create_app(self.cfg, database=self.db)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.cfg.port,
                                                    log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.cfg.port}"

    @property
    def runner(self):
        return self.app.state.runner

    @property
    def engine(self):
        return self.runner.engine

    def start(self) -> "NodeServer":
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError(f"{self.cfg.node_id} did not start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=20)

    def status(self) -> dict:
        return httpx.get(f"{self.url}/status", timeout=5).json()

    def metrics(self) -> dict:
        return httpx.get(f"{self.url}/metrics", timeout=5).json()


def wait_until(pred, timeout: float, what: str, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")
