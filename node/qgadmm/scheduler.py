"""Head/tail two-phase round scheduling for Q-GADMM.

Every round, per the paper:

    HEAD phase   heads (odd positions) rebuild mirrors from pending packets, take
                 their primal step, quantize, and send to both neighbours - all
                 heads in parallel
    barrier      each tail waits for the packets of both head neighbours (or times
                 out: the round is degraded and the stale mirror is reused)
    TAIL phase   tails do the same, sending back to their heads
    all nodes    dual update on every edge, then log the primal residual
                 ||theta_hat_n - theta_hat_{n+1}||_2 per edge

Convergence: stop once the largest edge residual stays below epsilon for
`CONVERGED_STREAK` consecutive rounds, or at the round limit.

This module holds the choreography twice: `simulate()` runs a whole chain in one
process (tests, experiments/compare_engines.py), and `ChainRoundScheduler` runs
one node's side of it over HTTP inside the node container. Both call the same
engine methods in the same order.

Dual-update rule in the networked version: the edge (m, n) takes its round-r dual
step only if *both* directions of that round's exchange arrived on time. Each end
knows both facts - whether it accepted the neighbour's packet on time, and whether
the neighbour said it accepted ours on time (the /update response) - so both ends
make the same decision and their copies of lambda stay identical even when a
neighbour dies or is slow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..engine import NeighborUpdate, UpdatePackage
from .state import HEAD, TAIL, all_edges

CONVERGED_STREAK = 3


def as_update(sender: str, round_num: int, pkg: UpdatePackage) -> NeighborUpdate:
    return NeighborUpdate(sender=sender, round=round_num, body=pkg.body, checksum=pkg.checksum, dtype=pkg.dtype,
                          seq=pkg.seq, base=pkg.base, produces=pkg.produces)


def converged(history: Sequence[dict[str, float] | None], edges: Sequence[str], epsilon: float,
              streak: int = CONVERGED_STREAK) -> bool:
    """True when the last `streak` rounds each have every edge's residual known and below epsilon."""
    if len(history) < streak:
        return False
    for rnd in history[-streak:]:
        if not rnd or any(e not in rnd for e in edges) or max(rnd[e] for e in edges) >= epsilon:
            return False
    return True


@dataclass
class SimRound:
    round: int
    residuals: dict[str, float]
    max_residual: float
    objective: float  # sum over nodes of local loss at each node's own theta, per training sample
    eval_loss: dict[str, float]
    payload_bytes: int  # this round, all messages
    cumulative_bits: int
    messages: int


@dataclass
class SimResult:
    rounds: list[SimRound] = field(default_factory=list)
    converged_at: int | None = None

    @property
    def final(self) -> SimRound:
        return self.rounds[-1]


def simulate(engines: Sequence, rounds: int, *, epsilon: float = 1e-3, stop_on_convergence: bool = True,
             on_round: Callable[[SimRound], None] | None = None) -> SimResult:
    """Run a Q-GADMM / GADMM chain in-process: no Docker, no HTTP, every packet on time."""
    order = sorted(engines, key=lambda e: e.pos.index)
    chain = order[0].pos.chain
    if [e.pos.node_id for e in order] != list(chain):
        raise ValueError("simulate() needs exactly one engine per chain position")
    by_id = {e.pos.node_id: e for e in order}
    heads = [e for e in order if e.pos.group == HEAD]
    tails = [e for e in order if e.pos.group == TAIL]
    edges = all_edges(chain)
    total_samples = sum(e.n_train for e in order)
    result = SimResult()
    history: list[dict[str, float]] = []
    cum_bits = 0
    for e in order:
        e.reset()

    for r in range(1, rounds + 1):
        inbox: dict[str, list[NeighborUpdate]] = {e.pos.node_id: [] for e in order}
        round_bytes = messages = 0

        def phase(group):
            nonlocal round_bytes, messages
            for e in group:
                e.apply_neighbor_update(inbox[e.pos.node_id])
                inbox[e.pos.node_id] = []
            for e in group:  # "in parallel": no node in a group reads another's output
                e.train_round(r)
                pkg = e.package_update(r)
                for m in e.pos.neighbours:
                    inbox[m].append(as_update(e.pos.node_id, r, pkg))
                    round_bytes += pkg.payload_bytes
                    messages += 1

        phase(heads)
        phase(tails)
        for e in heads:
            e.apply_neighbor_update(inbox[e.pos.node_id])
            inbox[e.pos.node_id] = []
        for e in order:
            res = e.apply_neighbor_update([])  # nothing pending; keeps the call order explicit
            assert not res.resync
            e.dual_update(set(e.pos.neighbours))
        residuals: dict[str, float] = {}
        for e in order:
            residuals.update(e.residuals())
        history.append(residuals)
        cum_bits += round_bytes * 8
        objective = sum(e.local_loss_mean() * e.n_train for e in order) / total_samples
        rec = SimRound(round=r, residuals=residuals, max_residual=max(residuals.values()) if residuals else 0.0,
                       objective=objective, eval_loss={e.pos.node_id: e.evaluate().loss for e in order},
                       payload_bytes=round_bytes, cumulative_bits=cum_bits, messages=messages)
        result.rounds.append(rec)
        if on_round:
            on_round(rec)
        if converged(history, edges, epsilon):
            result.converged_at = result.converged_at or r
            if stop_on_convergence:
                break
    return result
