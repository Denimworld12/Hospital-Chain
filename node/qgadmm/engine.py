"""QGADMMEngine: Q-GADMM (and full-precision GADMM) behind the Engine interface.

One round for node n (the scheduler decides *when* each call happens):

    apply_neighbor_update(packets)   rebuild mirrors theta_hat_{n-1}, theta_hat_{n+1} (Eq. 13)
    train_round(r)                   primal step (Eq. 14/16), inexact via Adam, on
                                       local_loss(theta)                        (sum over my samples)
                                     + lambda_{n-1,n}^T (theta_hat_{n-1} - theta) + rho/2 ||theta_hat_{n-1} - theta||^2
                                     + lambda_{n,n+1}^T (theta - theta_hat_{n+1}) + rho/2 ||theta - theta_hat_{n+1}||^2
                                     (a boundary node has only the side it has)
    package_update(r)                quantize theta - theta_hat, then rebuild my own theta_hat
                                     from that same packet so it matches what neighbours rebuild
    dual_update(ok)                  lambda_e += alpha*rho*(theta_hat_left - theta_hat_right)  (Eq. 18)
                                     for each edge e whose exchange completed this round
    residuals()                      ||theta_hat_left - theta_hat_right||_2 per edge I touch

Every packet carries a fingerprint of the snapshot it was built on and of the
snapshot it produces. A receiver applies a packet only when its mirror matches
the base fingerprint, and checks the result against the produced fingerprint, so
a mirror that drifts by a single bit is detected on the wire and resynced
instead of silently bending the convergence curve.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from typing import Sequence

import numpy as np
import torch

from .. import wire
from ..engine import Engine, Evaluation, LocalRoundResult, NeighborUpdate, UpdatePackage
from . import quantizer
from .state import ChainPosition, NodeState, edge_key, load_checkpoint, save_checkpoint

log = logging.getLogger("medchain.qgadmm")

DTYPE = "qgadmm-packet"  # value of the X-MedChain-Dtype header for Q-GADMM / GADMM packets
LOGISTIC = "logistic"
LINEAR = "linear"


def fingerprint(v: np.ndarray) -> str:
    """Exact-bytes fingerprint of a float64 vector (differs if any bit differs)."""
    return hashlib.sha256(np.ascontiguousarray(v, dtype="<f8").tobytes()).hexdigest()[:32]


def engine_label(bits: int) -> str:
    if bits == quantizer.FULL_PRECISION_BITS:
        return "GADMM full precision (float32 deltas)"
    return f"Q-GADMM (b={bits})"


@dataclass(frozen=True)
class ApplyResult:
    applied: tuple[str, ...]  # senders whose packet was folded into the mirror
    duplicate: tuple[str, ...]  # packet already reflected in the mirror (ignored)
    resync: tuple[str, ...]  # mirror does not match the packet's base: needs GET /snapshot


class QGADMMEngine(Engine):
    def __init__(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_test: np.ndarray,
        y_test: np.ndarray,
        *,
        position: ChainPosition,
        rho: float,
        alpha: float,
        bits: int,
        local_epochs: int,
        lr: float,
        seed: int,
        batch_size: int | None = None,
        task: str = LOGISTIC,
    ):
        if bits != quantizer.FULL_PRECISION_BITS and bits not in quantizer.QUANTIZED_BITS:
            raise ValueError(f"Q-GADMM bits must be 1..8 (config uses 2, 4 or 8), got {bits}")
        if task not in (LOGISTIC, LINEAR):
            raise ValueError(f"unknown task {task!r}")
        if rho <= 0 or alpha <= 0 or local_epochs < 1 or lr <= 0:
            raise ValueError("rho, alpha, lr must be > 0 and local_epochs >= 1")
        self.name = "gadmm_fp" if bits == quantizer.FULL_PRECISION_BITS else "qgadmm"
        self.label = engine_label(bits)
        self.pos = position
        self.rho = float(rho)
        self.alpha = float(alpha)
        self.bits = int(bits)
        self.local_epochs = int(local_epochs)
        self.lr = float(lr)
        self.seed = int(seed)
        self.task = task
        self.dtype = DTYPE
        self._X = torch.as_tensor(np.asarray(X_train, dtype=np.float64))
        self._y = torch.as_tensor(np.asarray(y_train, dtype=np.float64)).reshape(-1)
        self.X_test = np.asarray(X_test, dtype=np.float64)
        self.y_test = np.asarray(y_test)
        self.n_train = int(self._y.shape[0])
        self.batch_size = int(batch_size) if batch_size else self.n_train
        self.n_features = int(self._X.shape[1])
        self.d = self.n_features + 1  # weights + bias
        self.lock = threading.RLock()
        self.reset()

    # -- lifecycle ----------------------------------------------------------
    def reset(self, run_id: str | None = None) -> None:
        with self.lock:
            self.state = NodeState.zeros(self.d, self.pos, self.seed, run_id=run_id)
            self._build_model()

    def _build_model(self) -> None:
        torch.manual_seed(self.seed)
        self.model = torch.nn.Linear(self.n_features, 1, dtype=torch.float64)
        self._load_theta(self.state.theta)
        self.opt = torch.optim.Adam(self.model.parameters(), lr=self.lr)
        if self.state.optimizer:
            self.opt.load_state_dict(self.state.optimizer)

    def _load_theta(self, theta: np.ndarray) -> None:
        with torch.no_grad():
            self.model.weight.copy_(torch.as_tensor(theta[:-1]).reshape(1, -1))
            self.model.bias.copy_(torch.as_tensor(theta[-1:]))

    def _theta_tensor(self) -> torch.Tensor:
        return torch.cat([self.model.weight.reshape(-1), self.model.bias])

    @property
    def theta(self) -> np.ndarray:
        return self.state.theta

    # -- objective ----------------------------------------------------------
    def _local_loss_sum(self, X: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        out = self.model(X).reshape(-1)
        if self.task == LOGISTIC:
            return torch.nn.functional.binary_cross_entropy_with_logits(out, y, reduction="sum")
        return 0.5 * torch.sum((out - y) ** 2)

    def _coupling_terms(self, theta: torch.Tensor) -> dict[str, torch.Tensor]:
        """Dual and penalty terms, one pair per edge this node actually has."""
        terms: dict[str, torch.Tensor] = {}
        for m in self.pos.neighbours:
            lam = torch.as_tensor(self.state.lambdas[m])
            mh = torch.as_tensor(self.state.mirrors[m])
            if m == self.pos.left:  # edge (n-1, n): lambda^T (theta_hat_{n-1} - theta)
                gap = mh - theta
            else:  # edge (n, n+1): lambda^T (theta - theta_hat_{n+1})
                gap = theta - mh
            terms[f"dual:{m}"] = torch.dot(lam, gap)
            terms[f"penalty:{m}"] = 0.5 * self.rho * torch.dot(gap, gap)
        return terms

    def lagrangian_terms(self, theta: np.ndarray | None = None) -> dict[str, float]:
        """The primal objective broken into named terms (for tests and audit)."""
        with self.lock, torch.no_grad():
            if theta is not None:
                self._load_theta(np.asarray(theta, dtype=np.float64))
            t = self._theta_tensor()
            out = {"local_loss": float(self._local_loss_sum(self._X, self._y))}
            out.update({k: float(v) for k, v in self._coupling_terms(t).items()})
            self._load_theta(self.state.theta)
            return out

    # -- Engine interface ---------------------------------------------------
    def train_round(self, round_num: int) -> LocalRoundResult:
        """Primal step: E epochs of Adam on the augmented Lagrangian, warm-started from theta."""
        with self.lock:
            n, bs = self.n_train, self.batch_size
            for _ in range(self.local_epochs):
                order = self.state.rng.permutation(n) if bs < n else np.arange(n)
                for start in range(0, n, bs):
                    idx = torch.as_tensor(order[start:start + bs])
                    self.opt.zero_grad()
                    scale = n / len(idx)  # unbiased estimate of the full-sum local loss
                    loss = scale * self._local_loss_sum(self._X[idx], self._y[idx])
                    loss = loss + sum(self._coupling_terms(self._theta_tensor()).values())
                    loss.backward()
                    self.opt.step()
            with torch.no_grad():
                self.state.theta = self._theta_tensor().detach().numpy().astype(np.float64).copy()
                train_loss = float(self._local_loss_sum(self._X, self._y)) / n
            self.state.optimizer = self.opt.state_dict()
            self.state.round_no = round_num
            return LocalRoundResult(round=round_num, train_loss=train_loss, n_train=n)

    def package_update(self, round_num: int) -> UpdatePackage:
        """Quantize theta - theta_hat, then rebuild my own theta_hat from the very same packet."""
        with self.lock:
            base = self.state.theta_hat
            pkt = quantizer.quantize(self.state.theta, base, self.bits, self.state.rng)
            body = quantizer.encode(pkt)
            base_fp = fingerprint(base)
            self.state.theta_hat = quantizer.reconstruct(base, pkt)
            self.state.seq += 1
            return UpdatePackage(
                body=body,
                checksum=wire.checksum(body),
                dtype=self.dtype,
                n_params=self.d,
                bits=self.bits,
                full_precision_bytes=self.d * 8,
                seq=self.state.seq,
                base=base_fp,
                produces=fingerprint(self.state.theta_hat),
            )

    def apply_neighbor_update(self, updates: Sequence[NeighborUpdate]) -> ApplyResult:
        """Fold packets into mirrors, in sequence order, checking both fingerprints."""
        applied, duplicate, resync = [], [], []
        with self.lock:
            for u in sorted(updates, key=lambda u: (u.sender, u.seq or 0)):
                if u.sender not in self.state.mirrors:
                    raise ValueError(f"update from {u.sender}, which is not a chain neighbour of {self.pos.node_id}")
                outcome, new, seq = self._fold(self.state.mirrors[u.sender], self.state.mirror_seq[u.sender], u)
                if outcome == "duplicate":
                    duplicate.append(u.sender)
                elif outcome == "resync":
                    log.warning("packet seq %s from %s does not apply to my mirror (mirror seq %s)", u.seq, u.sender,
                                self.state.mirror_seq[u.sender])
                    resync.append(u.sender)
                else:
                    self.state.mirrors[u.sender], self.state.mirror_seq[u.sender] = new, seq
                    applied.append(u.sender)
        return ApplyResult(tuple(applied), tuple(duplicate), tuple(dict.fromkeys(resync)))

    def applies_cleanly(self, sender: str, updates: Sequence[NeighborUpdate]) -> bool:
        """True when these packets from `sender` fold into my mirror, in sequence order, without a resync.

        A dry run of apply_neighbor_update that changes nothing. It reads the mirror without taking the
        engine lock (mirrors are replaced, never mutated in place), so it does not wait for a primal step.
        """
        mirror, seq = self.state.mirrors.get(sender), self.state.mirror_seq.get(sender)
        if mirror is None:
            return False
        for u in sorted(updates, key=lambda u: u.seq or 0):
            outcome, new, new_seq = self._fold(mirror, seq, u)
            if outcome == "resync":
                return False
            if outcome == "applied":
                mirror, seq = new, new_seq
        return True

    def _fold(self, mirror: np.ndarray, mirror_seq: int, u: NeighborUpdate) -> tuple[str, np.ndarray, int]:
        """One packet against one mirror: ("duplicate" | "resync" | "applied", resulting mirror, its seq)."""
        if u.seq is not None and u.seq <= mirror_seq:
            return "duplicate", mirror, mirror_seq  # already reflected (e.g. overtaken by a snapshot resync)
        have = fingerprint(mirror)
        if u.produces and have == u.produces:
            return "duplicate", mirror, mirror_seq
        if u.base and have != u.base:
            return "resync", mirror, mirror_seq
        new = quantizer.reconstruct(mirror, quantizer.decode(u.body, self.d))
        if u.produces and fingerprint(new) != u.produces:
            log.error("mirror of %s drifted after seq %s", u.sender, u.seq)
            return "resync", mirror, mirror_seq
        return "applied", new, u.seq if u.seq is not None else mirror_seq + 1

    def dual_update(self, completed: set[str] | Sequence[str]) -> None:
        """Eq. 18 on each edge whose exchange completed both ways this round; the rest keep their lambda."""
        with self.lock:
            for m in self.pos.neighbours:
                if m not in completed:
                    continue
                left, right = self._hats(m)
                self.state.lambdas[m] = self.state.lambdas[m] + self.alpha * self.rho * (left - right)

    def _hats(self, m: str) -> tuple[np.ndarray, np.ndarray]:
        l, r = self.pos.edge_with(m)
        get = lambda nid: self.state.theta_hat if nid == self.pos.node_id else self.state.mirrors[nid]  # noqa: E731
        return get(l), get(r)

    def residuals(self) -> dict[str, float]:
        with self.lock:
            out = {}
            for m in self.pos.neighbours:
                left, right = self._hats(m)
                out[edge_key(self.pos.edge_with(m))] = float(np.linalg.norm(left - right))
            return out

    def evaluate(self) -> Evaluation:
        with self.lock:
            theta = self.state.theta
        with np.errstate(all="ignore"):  # spurious macOS Accelerate matmul warnings (see node/trainer.py)
            z = self.X_test @ theta[:-1] + theta[-1]
        if self.task == LOGISTIC:
            p = np.clip(1.0 / (1.0 + np.exp(-np.clip(z, -30, 30))), 1e-15, 1 - 1e-15)
            y = self.y_test
            loss = float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
            acc = float(np.mean((p >= 0.5).astype(int) == y))
        else:  # linear: mean squared error, and R^2 in the accuracy slot
            err = z - self.y_test
            loss = float(np.mean(err**2))
            var = float(np.var(self.y_test))
            acc = float(1 - np.mean(err**2) / var) if var > 0 else 0.0
        return Evaluation(loss=loss, accuracy=acc, n_test=len(self.y_test))

    def local_loss_mean(self, theta: np.ndarray | None = None) -> float:
        """Training objective per sample at theta (default: my current theta)."""
        with self.lock, torch.no_grad():
            if theta is not None:
                self._load_theta(np.asarray(theta, dtype=np.float64))
            v = float(self._local_loss_sum(self._X, self._y)) / self.n_train
            self._load_theta(self.state.theta)
            return v

    # -- resync (GET /snapshot) ---------------------------------------------
    def snapshot_for(self, requester: str) -> dict:
        """What a neighbour needs to resync with me: my theta_hat, my mirror of it, our edge's lambda.

        Model state only - never raw data. For a neighbour I am not linked to yet (a link that starts
        at the next boundary) only my theta_hat is meaningful: the mirror is unset and lambda is 0.
        """
        with self.lock:
            if requester not in self.state.mirrors:  # a link that starts at the next round boundary
                z = np.zeros(self.d, dtype=np.float64)
                return {"theta_hat": self.state.theta_hat.copy(), "seq": self.state.seq, "mirror": z,
                        "mirror_seq": -1, "lambda": z.copy()}
            return {
                "theta_hat": self.state.theta_hat.copy(),
                "seq": self.state.seq,
                "mirror": self.state.mirrors[requester].copy(),
                "mirror_seq": self.state.mirror_seq[requester],
                "lambda": self.state.lambdas[requester].copy(),
            }

    # -- membership changes (always at a round boundary) ----------------------
    def relink(self, position: ChainPosition) -> tuple[set[str], set[str]]:
        """Move to a new chain position. Returns (added, removed) neighbours.

        A removed link's mirror and lambda are discarded. A new link starts at lambda = 0 with an
        unusable mirror (seq -1): nothing from that neighbour can be decoded until a full theta_hat
        snapshot has been exchanged (adopt_mirror), exactly as after a rejoin. Links that survive keep
        their state: the chain only ever loses or gains members, it is never reordered, so a surviving
        edge keeps its (left, right) orientation and its lambda keeps its sign.
        """
        with self.lock:
            old, new = set(self.state.mirrors), set(position.neighbours)
            for m in old - new:
                for d in (self.state.mirrors, self.state.mirror_seq, self.state.lambdas):
                    d.pop(m, None)
            for m in new - old:
                self.state.mirrors[m] = np.zeros(self.d, dtype=np.float64)
                self.state.mirror_seq[m] = -1
                self.state.lambdas[m] = np.zeros(self.d, dtype=np.float64)
            self.pos = position
            return new - old, old - new

    def warm_start(self, theta_hat: np.ndarray) -> None:
        """A hospital joining mid-run starts from its new neighbour's published model, not from zero.

        Its own theta_hat (what neighbours will believe about it) starts at that same vector, seq 0;
        the neighbour still takes it from a full snapshot before decoding anything from this node.
        """
        with self.lock:
            v = np.asarray(theta_hat, dtype=np.float64)
            self.state.theta, self.state.theta_hat, self.state.seq = v.copy(), v.copy(), 0
            self._load_theta(self.state.theta)

    def adopt_mirror(self, sender: str, theta_hat: np.ndarray, seq: int) -> None:
        with self.lock:
            self.state.mirrors[sender] = np.asarray(theta_hat, dtype=np.float64).copy()
            self.state.mirror_seq[sender] = int(seq)

    def adopt_rejoin(self, snaps: dict[str, dict]) -> None:
        """After a restart: take each neighbour's view of our shared edge.

        My own theta_hat becomes the most advanced copy my neighbours hold (a neighbour
        with an older copy detects the gap on my next packet and resyncs from me).
        """
        with self.lock:
            for m, s in snaps.items():
                self.state.mirrors[m] = np.asarray(s["theta_hat"], dtype=np.float64).copy()
                self.state.mirror_seq[m] = int(s["seq"])
                self.state.lambdas[m] = np.asarray(s["lambda"], dtype=np.float64).copy()
            if snaps:
                best = max(snaps.values(), key=lambda s: s["mirror_seq"])
                self.state.theta_hat = np.asarray(best["mirror"], dtype=np.float64).copy()
                self.state.seq = int(best["mirror_seq"])

    # -- checkpointing ------------------------------------------------------
    def save(self, path) -> int:
        with self.lock:
            return save_checkpoint(self.state, path)

    def restore(self, path) -> bool:
        with self.lock:
            st = load_checkpoint(path)
            if st is None:
                return False
            if st.theta.shape != (self.d,):
                raise ValueError(f"checkpoint has d={st.theta.shape}, model has d={self.d}")
            self.state = st
            self._build_model()
            return True


def snapshot_body(snap: dict) -> bytes:
    """GET /snapshot body: theta_hat | mirror-of-requester | lambda, raw float64 (metadata in headers)."""
    return b"".join(wire.encode(snap[k]) for k in ("theta_hat", "mirror", "lambda"))


def parse_snapshot(body: bytes, d: int, seq: int, mirror_seq: int) -> dict:
    v = wire.decode(body)
    if v.size != 3 * d:
        raise ValueError(f"snapshot has {v.size} values, expected {3 * d}")
    return {"theta_hat": v[:d].copy(), "seq": seq, "mirror": v[d:2 * d].copy(), "mirror_seq": mirror_seq,
            "lambda": v[2 * d:].copy()}

