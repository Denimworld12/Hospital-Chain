"""The swappable training-engine interface.

A node's round loop (node/runner.py) only talks to an Engine:

    1. train_round(r)             - local training on this hospital's own data
    2. package_update(r)          - serialize what gets sent to neighbours
    3. apply_neighbor_update(us)  - fold in the updates received this round
    4. evaluate()                 - metrics on the local held-out split

The current implementation is the naive gossip baseline (node/gossip.py). A real
ADMM-based engine (Q-GADMM) replaces it in a later phase behind this same
interface; select it with `engine:` in the node config.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class LocalRoundResult:
    round: int
    train_loss: float
    n_train: int


@dataclass(frozen=True)
class UpdatePackage:
    """Exactly what goes on the wire as the /update HTTP body, plus honest accounting."""

    body: bytes
    checksum: str  # sha-256 hex of body
    dtype: str
    n_params: int
    bits: int
    full_precision_bytes: int  # what the same parameters cost as float64

    @property
    def payload_bytes(self) -> int:
        return len(self.body)


@dataclass(frozen=True)
class NeighborUpdate:
    sender: str
    round: int
    body: bytes
    checksum: str
    dtype: str


@dataclass(frozen=True)
class Evaluation:
    loss: float
    accuracy: float
    n_test: int


class Engine(ABC):
    #: machine id used in config (`engine:`)
    name: str
    #: human label shown in logs and the UI; must describe the method honestly
    label: str

    @abstractmethod
    def reset(self) -> None:
        """Start a fresh model for a new training run."""

    @abstractmethod
    def train_round(self, round_num: int) -> LocalRoundResult:
        """Train locally on this node's own data for one round."""

    @abstractmethod
    def package_update(self, round_num: int) -> UpdatePackage:
        """Serialize this node's update for its neighbours (model parameters only, never data)."""

    @abstractmethod
    def apply_neighbor_update(self, updates: Sequence[NeighborUpdate]) -> None:
        """Fold in the neighbour updates received for the current round (may be empty)."""

    @abstractmethod
    def evaluate(self) -> Evaluation:
        """Metrics on this node's local held-out split."""
