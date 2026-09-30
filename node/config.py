"""Node configuration, loaded from the YAML file named by $NODE_CONFIG.

Nothing in node/ hardcodes a hospital identity: node_id, neighbours, port, data
slice, shared secret and engine parameters all come from this file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Neighbour:
    id: str
    url: str


@dataclass(frozen=True)
class TrainingConfig:
    rounds: int = 20
    local_epochs: int = 2
    learning_rate: float = 0.005
    test_fraction: float = 0.25
    seed: int = 7


@dataclass(frozen=True)
class TimingConfig:
    heartbeat_interval_s: float = 5.0
    round_timeout_s: float = 6.0
    round_pause_s: float = 1.0


@dataclass(frozen=True)
class NodeConfig:
    node_id: str
    display_name: str
    port: int
    advertise_url: str
    data_slice: str
    shared_secret: str
    neighbours: tuple[Neighbour, ...] = ()
    engine: str = "gossip-baseline"
    rho: float = 1.0
    bits: int = 64
    host: str = "0.0.0.0"
    training: TrainingConfig = field(default_factory=TrainingConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)

    def neighbour(self, node_id: str) -> Neighbour | None:
        return next((n for n in self.neighbours if n.id == node_id), None)


def load_config(path: str | os.PathLike | None = None) -> NodeConfig:
    path = Path(path or os.environ.get("NODE_CONFIG", ""))
    if not path.is_file():
        raise FileNotFoundError(f"node config not found: {path!s} (set NODE_CONFIG)")
    raw = yaml.safe_load(path.read_text()) or {}
    missing = [k for k in ("node_id", "port", "data_slice", "shared_secret") if k not in raw]
    if missing:
        raise ValueError(f"{path}: missing required keys {missing}")
    neighbours = tuple(Neighbour(id=str(n["id"]), url=str(n["url"]).rstrip("/")) for n in raw.get("neighbours") or [])
    if any(n.id == raw["node_id"] for n in neighbours):
        raise ValueError(f"{path}: a node cannot be its own neighbour")
    if not str(raw["shared_secret"]):
        raise ValueError(f"{path}: shared_secret must not be empty")
    port = int(raw["port"])
    return NodeConfig(
        node_id=str(raw["node_id"]),
        display_name=str(raw.get("display_name", raw["node_id"])),
        port=port,
        advertise_url=str(raw.get("advertise_url", f"http://{raw['node_id']}:{port}")).rstrip("/"),
        data_slice=str(raw["data_slice"]),
        shared_secret=str(raw["shared_secret"]),
        neighbours=neighbours,
        engine=str(raw.get("engine", "gossip-baseline")),
        rho=float(raw.get("rho", 1.0)),
        bits=int(raw.get("bits", 64)),
        host=str(raw.get("host", "0.0.0.0")),
        training=TrainingConfig(**(raw.get("training") or {})),
        timing=TimingConfig(**(raw.get("timing") or {})),
    )
