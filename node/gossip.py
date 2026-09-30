"""Naive gossip baseline: plain neighbour weight averaging.

This is the comparison baseline and is labelled as such everywhere: each round a
node trains locally, sends its full-precision weights to its neighbours, then
replaces its weights with the uniform average of its own and whatever neighbour
weights arrived. No ADMM, no dual variables, no quantization; `rho` and `bits` are
ignored and weights always travel as float64. It stays selectable next to Q-GADMM
(node/qgadmm/) behind the Engine interface (node/engine.py): `engine: gossip`.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from . import wire
from .engine import Engine, Evaluation, LocalRoundResult, NeighborUpdate, UpdatePackage
from .trainer import LocalTrainer

LABEL = "naive gossip baseline"


class GossipBaselineEngine(Engine):
    name = "gossip"
    label = LABEL

    def __init__(self, trainer: LocalTrainer, *, bits: int = 64):
        if bits != 64:
            raise ValueError(
                f"the {LABEL} sends full-precision float64 weights only (got bits={bits}); "
                "quantization is the Q-GADMM engine (engine: qgadmm)."
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

