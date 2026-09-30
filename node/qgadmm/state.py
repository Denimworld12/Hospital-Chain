"""Per-node Q-GADMM state and checkpointing.

Chain positions are 1-based: odd = HEAD group, even = TAIL group. Node n only
ever touches the edges (n-1, n) and (n, n+1); the boundary nodes 1 and N have a
single edge, and therefore a single mirror, a single dual variable and a single
penalty term. Everything that is per-edge is keyed by the neighbour's node id,
so a missing side is simply absent rather than a zero placeholder.

    theta             my weights (never leave this process)
    theta_hat, seq    my quantized self-snapshot: what my neighbours believe about me,
                      and how many packets built it
    mirrors[m], mirror_seq[m]
                      what I believe about neighbour m (bit-identical to m's theta_hat
                      when mirror_seq[m] == m's seq)
    lambdas[m]        dual variable of the edge I share with m (both ends hold an
                      identical copy and apply identical updates)
"""

from __future__ import annotations

import os
import pickle
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HEAD = "head"
TAIL = "tail"
CHECKPOINT_VERSION = 1


@dataclass(frozen=True)
class ChainPosition:
    chain: tuple[str, ...]
    node_id: str

    def __post_init__(self):
        if self.node_id not in self.chain:
            raise ValueError(f"{self.node_id} is not in the chain {list(self.chain)}")
        if len(set(self.chain)) != len(self.chain):
            raise ValueError(f"chain has duplicate nodes: {list(self.chain)}")

    @property
    def index(self) -> int:
        return self.chain.index(self.node_id)

    @property
    def position(self) -> int:
        """1-based position along the chain."""
        return self.index + 1

    @property
    def n_nodes(self) -> int:
        return len(self.chain)

    @property
    def group(self) -> str:
        return HEAD if self.position % 2 == 1 else TAIL

    @property
    def left(self) -> str | None:
        return self.chain[self.index - 1] if self.index > 0 else None

    @property
    def right(self) -> str | None:
        return self.chain[self.index + 1] if self.index < len(self.chain) - 1 else None

    @property
    def neighbours(self) -> tuple[str, ...]:
        return tuple(n for n in (self.left, self.right) if n is not None)

    def edge_with(self, neighbour: str) -> tuple[str, str]:
        """The edge (left, right) in chain order that I share with `neighbour`."""
        if neighbour == self.left:
            return (neighbour, self.node_id)
        if neighbour == self.right:
            return (self.node_id, neighbour)
        raise KeyError(f"{neighbour} is not a chain neighbour of {self.node_id}")

    @property
    def edges(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.edge_with(n) for n in self.neighbours)


def edge_key(edge: tuple[str, str]) -> str:
    return f"{edge[0]}|{edge[1]}"


def all_edges(chain: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(edge_key((a, b)) for a, b in zip(chain, chain[1:]))


@dataclass
class NodeState:
    theta: np.ndarray
    theta_hat: np.ndarray
    seq: int
    mirrors: dict[str, np.ndarray]
    mirror_seq: dict[str, int]
    lambdas: dict[str, np.ndarray]
    rng: np.random.Generator
    round_no: int = 0
    run_id: str | None = None
    optimizer: dict | None = None  # Adam moments, persisted across rounds
    extra: dict = field(default_factory=dict)

    @classmethod
    def zeros(cls, d: int, pos: ChainPosition, seed: int, run_id: str | None = None) -> "NodeState":
        z = lambda: np.zeros(d, dtype=np.float64)  # noqa: E731
        return cls(
            theta=z(),
            theta_hat=z(),
            seq=0,
            mirrors={m: z() for m in pos.neighbours},
            mirror_seq={m: 0 for m in pos.neighbours},
            lambdas={m: z() for m in pos.neighbours},
            rng=np.random.default_rng(seed),
            run_id=run_id,
        )

    # -- checkpointing ------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": CHECKPOINT_VERSION,
            "theta": self.theta.copy(),
            "theta_hat": self.theta_hat.copy(),
            "seq": self.seq,
            "mirrors": {k: v.copy() for k, v in self.mirrors.items()},
            "mirror_seq": dict(self.mirror_seq),
            "lambdas": {k: v.copy() for k, v in self.lambdas.items()},
            "rng_state": self.rng.bit_generator.state,
            "round_no": self.round_no,
            "run_id": self.run_id,
            "optimizer": self.optimizer,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "NodeState":
        if raw.get("version") != CHECKPOINT_VERSION:
            raise ValueError(f"unsupported checkpoint version {raw.get('version')!r}")
        rng = np.random.default_rng()
        rng.bit_generator.state = raw["rng_state"]
        return cls(
            theta=raw["theta"],
            theta_hat=raw["theta_hat"],
            seq=raw["seq"],
            mirrors=raw["mirrors"],
            mirror_seq=raw["mirror_seq"],
            lambdas=raw["lambdas"],
            rng=rng,
            round_no=raw["round_no"],
            run_id=raw["run_id"],
            optimizer=raw.get("optimizer"),
            extra=raw.get("extra") or {},
        )


def checkpoint_path(directory: str | os.PathLike, node_id: str) -> Path:
    return Path(directory) / f"{node_id}.ckpt.pkl"


def save_checkpoint(state: NodeState, path: str | os.PathLike) -> int:
    """Pickle atomically (write + rename) so a kill mid-write never leaves a torn file. Returns bytes written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = pickle.dumps(state.to_dict(), protocol=pickle.HIGHEST_PROTOCOL)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return len(blob)


def load_checkpoint(path: str | os.PathLike) -> NodeState | None:
    path = Path(path)
    if not path.is_file():
        return None
    with path.open("rb") as fh:
        return NodeState.from_dict(pickle.load(fh))  # noqa: S301 - our own file on our own volume


def clear_checkpoint(path: str | os.PathLike) -> None:
    Path(path).unlink(missing_ok=True)
