"""Node configuration, loaded from the YAML file named by $NODE_CONFIG.

Nothing in node/ hardcodes a hospital identity: node_id, neighbours, port, data
slice, shared secret and engine parameters all come from this file.

Engines (`engine:`): `gossip` (the naive baseline; `gossip-baseline` is accepted
as its legacy name), `qgadmm` (Q-GADMM, `bits` in {2, 4, 8}) and `gadmm_fp`
(full-precision GADMM). The ADMM engines also read `chain` (node order 1..N),
`rho`, `alpha`, `local_epochs`, `lr`, `epsilon`, `checkpoint_every`,
`checkpoint_dir` and `seed` (the node's RNG uses seed + its chain position).
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


GOSSIP = "gossip"
QGADMM = "qgadmm"
GADMM_FP = "gadmm_fp"
ENGINE_ALIASES = {"gossip": GOSSIP, "gossip-baseline": GOSSIP, "qgadmm": QGADMM, "gadmm_fp": GADMM_FP}
ADMM_ENGINES = (QGADMM, GADMM_FP)


@dataclass(frozen=True)
class NodeConfig:
    node_id: str
    display_name: str
    port: int
    advertise_url: str
    data_slice: str
    shared_secret: str
    neighbours: tuple[Neighbour, ...] = ()
    engine: str = GOSSIP
    rho: float = 1.0
    bits: int = 64
    host: str = "0.0.0.0"
    # ADMM engines (qgadmm / gadmm_fp); the gossip baseline ignores these
    chain: tuple[str, ...] = ()
    alpha: float = 1.0
    local_epochs: int = 50
    lr: float = 0.01
    epsilon: float = 1e-3
    checkpoint_every: int = 5
    checkpoint_dir: str = "/checkpoints"
    seed: int = 42
    training: TrainingConfig = field(default_factory=TrainingConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)

    def neighbour(self, node_id: str) -> Neighbour | None:
        return next((n for n in self.neighbours if n.id == node_id), None)

    @property
    def is_admm(self) -> bool:
        return self.engine in ADMM_ENGINES


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
    engine = str(raw.get("engine", GOSSIP))
    if engine not in ENGINE_ALIASES:
        raise ValueError(f"{path}: unknown engine {engine!r}; use one of {sorted(ENGINE_ALIASES)}")
    engine = ENGINE_ALIASES[engine]
    chain = tuple(str(c) for c in raw.get("chain") or ())
    if engine in ADMM_ENGINES:
        _check_chain(path, str(raw["node_id"]), chain, neighbours)
    return NodeConfig(
        node_id=str(raw["node_id"]),
        display_name=str(raw.get("display_name", raw["node_id"])),
        port=port,
        advertise_url=str(raw.get("advertise_url", f"http://{raw['node_id']}:{port}")).rstrip("/"),
        data_slice=str(raw["data_slice"]),
        shared_secret=str(raw["shared_secret"]),
        neighbours=neighbours,
        engine=engine,
        rho=float(raw.get("rho", 1.0)),
        bits=int(raw.get("bits", 64)),
        host=str(raw.get("host", "0.0.0.0")),
        chain=chain,
        alpha=float(raw.get("alpha", 1.0)),
        local_epochs=int(raw.get("local_epochs", 50)),
        lr=float(raw.get("lr", 0.01)),
        epsilon=float(raw.get("epsilon", 1e-3)),
        checkpoint_every=int(raw.get("checkpoint_every", 5)),
        checkpoint_dir=str(raw.get("checkpoint_dir", "/checkpoints")),
        seed=int(raw.get("seed", 42)),
        training=TrainingConfig(**(raw.get("training") or {})),
        timing=TimingConfig(**(raw.get("timing") or {})),
    )


def _check_chain(path, node_id: str, chain: tuple[str, ...], neighbours: tuple[Neighbour, ...]) -> None:
    """ADMM engines need the chain order, and the configured neighbours must be exactly my chain neighbours."""
    if not chain:
        raise ValueError(f"{path}: engine needs `chain:` (node ids in chain order 1..N)")
    if len(set(chain)) != len(chain) or node_id not in chain:
        raise ValueError(f"{path}: chain must list distinct node ids including {node_id}")
    i = chain.index(node_id)
    adjacent = {chain[j] for j in (i - 1, i + 1) if 0 <= j < len(chain)}
    if adjacent != {n.id for n in neighbours}:
        raise ValueError(f"{path}: neighbours {sorted(n.id for n in neighbours)} must be the chain neighbours {sorted(adjacent)}")
