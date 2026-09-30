"""Naive gossip baseline: plain neighbour weight averaging.

This is a placeholder engine and is labelled as such everywhere: each round a
node trains locally, sends its full-precision weights to its neighbours, then
replaces its weights with the uniform average of its own and whatever neighbour
weights arrived. No ADMM, no dual variables, no quantization; `rho` is ignored
and weights always travel as float64. Q-GADMM replaces it in a later phase
behind the Engine interface (node/engine.py).
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from . import wire
from .engine import Engine, Evaluation, LocalRoundResult, NeighborUpdate, UpdatePackage
from .trainer import LocalTrainer

LABEL = "naive gossip baseline"


class GossipBaselineEngine(Engine):
    name = "gossip-baseline"
    label = LABEL

    def __init__(self, trainer: LocalTrainer, *, bits: int = 64):
        if bits != 64:
            raise ValueError(
                f"the {LABEL} sends full-precision float64 weights only; set bits: 64 (got {bits}). "
                "Quantization arrives with the Q-GADMM engine in a later phase."
            )
        self.trainer = trainer
        self.dtype = "float64"
        self.bits = 64

    def reset(self) -> None:
        self.trainer.reset()

    def train_round(self, round_num: int) -> LocalRoundResult:
        train_loss = self.trainer.fit_round(round_num)
        return LocalRoundResult(round=round_num, train_loss=train_loss, n_train=len(self.trainer.y_train))

    def package_update(self, round_num: int) -> UpdatePackage:
        w = self.trainer.weights
        body = wire.encode(w, self.dtype)
        return UpdatePackage(
            body=body,
            checksum=wire.checksum(body),
            dtype=self.dtype,
            n_params=int(w.size),
            bits=self.bits,
            full_precision_bytes=int(w.size) * 8,
        )

    def apply_neighbor_update(self, updates: Sequence[NeighborUpdate]) -> None:
        own = self.trainer.weights
        stack = [own]
        for u in updates:
            w = wire.decode(u.body, u.dtype)
            if w.shape != own.shape:
                raise ValueError(f"update from {u.sender} has shape {w.shape}, expected {own.shape}")
            stack.append(w)
        self.trainer.weights = np.mean(np.stack(stack), axis=0)

    def evaluate(self) -> Evaluation:
        t = self.trainer
        return Evaluation(loss=t.loss(t.X_test, t.y_test), accuracy=t.accuracy(t.X_test, t.y_test), n_test=len(t.y_test))


def build_engine(name: str, trainer: LocalTrainer, *, bits: int, rho: float) -> Engine:
    """Engine registry. Add the ADMM-based engine here when it lands."""
    if name == GossipBaselineEngine.name:
        return GossipBaselineEngine(trainer, bits=bits)
    raise ValueError(f"unknown engine {name!r}; available: {GossipBaselineEngine.name}")
