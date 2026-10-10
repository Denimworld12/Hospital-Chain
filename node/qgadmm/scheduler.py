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
neighbour dies or is slow. A receiver decides "on time" atomically: once it stops
waiting for (round, sender), later packets for that round are answered
`on_time: false` and applied at its next phase as pending packets. It also answers
`on_time: false` for a packet that does not fold cleanly into its mirror (the
mirror needs a GET /snapshot resync first), so an edge whose mirror is out of sync
never takes the dual step at either end, whether or not the resync succeeds.

Networked convergence: each node only computes the residuals of its own edges, so
nodes gossip their recent per-edge residuals in a header. The first node that sees
every edge below epsilon for 3 consecutive rounds proposes a stop round a chain's
length ahead; the proposal travels with the packets and every node stops after the
same round.

Membership changes (node/membership.py) land only at a round boundary, the same
round on every node, so the head/tail schedule never breaks: positions are
recomputed from the new chain (1, 3, 5, ... heads; 2, 4, ... tails), a removed
link's lambda and mirror are dropped, and a new link starts at lambda = 0 and
decodes nothing until both ends have fetched the other's full theta_hat with GET
/snapshot - the same resync rule a rejoin uses. No quantized packet is sent on a
link before that exchange. Consensus detection restarts with the new chain: only
rounds of the current membership epoch count toward the streak, and a stop agreed
under the old chain is dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Callable, Mapping, Sequence

import httpx

from .. import pinning, security, wire
from ..engine import NeighborUpdate, UpdatePackage
from ..runner import ACTIVE, RoundMetrics, Runner
from . import engine as qengine
from .state import HEAD, TAIL, ChainPosition, all_edges, clear_checkpoint, edge_key

log = logging.getLogger("medchain.qgadmm")

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


# ---------------------------------------------------------------------------
# networked: one node's side of the choreography, inside the node container
# ---------------------------------------------------------------------------


class ChainRoundScheduler(Runner):
    """Round loop for a Q-GADMM / GADMM node: head/tail phases over HTTP, checkpoints, rejoin."""

    # A lagging neighbour's pending packets are still needed (each builds on the last), so keep far more
    # than the gossip baseline does; a packet that is dropped anyway is recovered by a snapshot resync.
    INBOX_ROUNDS = 1024

    def __init__(self, cfg, engine: "qengine.QGADMMEngine", db, client: httpx.AsyncClient, *, checkpoint_path, **kw):
        self.pos = engine.pos
        self._needs_link: set[str] = set()  # new links whose full snapshot exchange has not happened yet
        super().__init__(cfg, engine, db, client, **kw)
        self.checkpoint_path = checkpoint_path
        self._reset_run_state()

    # -- membership -------------------------------------------------------------
    def _set_membership(self, doc) -> None:
        super()._set_membership(doc)
        if doc.has(self.cfg.node_id):
            pos = ChainPosition(doc.chain, self.cfg.node_id)
            added, _ = self.engine.relink(pos)
            self.pos = pos
            self._needs_link |= added
            self._needs_link &= set(pos.neighbours)
        self.edges = all_edges(doc.chain)

    def _apply_membership(self, doc, r) -> None:
        old = set(self.pos.neighbours)
        changed = self.membership is None or not doc.same_chain(self.membership)
        super()._apply_membership(doc, r)
        if not self.in_chain or r is None:
            return
        new = set(self.pos.neighbours)
        for m in old - new:
            for d in (self._closed_upto, self._peer_round, self._peer_alive):
                d.pop(m, None)
        for m in new - old:
            self._closed_upto[m] = r - 1
            self._peer_round.setdefault(m, 0)
            self._peer_alive[m] = True
        if changed:  # consensus must be re-established on the new chain
            self.stop_at = self.converged_round = None

    async def _link_new_neighbours(self, run_id: str, r: int) -> None:
        """Full theta_hat snapshot from each new neighbour before any quantized packet crosses the link."""
        for m in sorted(self._needs_link):
            if await self.resync_mirror(run_id, r, m, reason="new link: full snapshot exchange before any "
                                        "quantized update (lambda starts at 0)", rejoin_round=r):
                self._needs_link.discard(m)

    async def _prepare_join(self, peers) -> None:
        self._reset_run_state(start_round=self.start_round)
        self._needs_link = set(self.pos.neighbours)
        for s in peers.values():
            if s.get("stop_at"):
                self._propose_stop(int(s["stop_at"]))
        # start from the new neighbour's published model rather than from zero
        got = await self._fetch_snapshot(self.pos.left or self.pos.right)
        if got is not None:
            await asyncio.to_thread(self.engine.warm_start, got[0]["theta_hat"])

    def _reset_run_state(self, start_round: int = 1) -> None:
        self.start_round = start_round
        self.stop_at: int | None = None
        self.converged_round: int | None = None
        self.view: dict[int, dict[str, float]] = {}  # round -> edge -> residual (mine + gossiped)
        self._closed_upto = {m: start_round - 1 for m in self.pos.neighbours}  # windows closed up to this round
        self._peer_round = {m: 0 for m in self.pos.neighbours}  # latest round seen from each neighbour
        self._peer_alive = {m: True for m in self.pos.neighbours}
        self._acked: set[tuple[int, str]] = set()  # (round, sender) packets answered on_time

    # -- inbound --------------------------------------------------------------
    def _on_time(self, run_id: str, update: NeighborUpdate) -> bool:
        if run_id != self.run_id or update.round <= self._closed_upto.get(update.sender, 0):
            return False
        buffered = [ups[update.sender] for (rid, _), ups in self._inbox.items()
                    if rid == run_id and update.sender in ups]
        if not self.engine.applies_cleanly(update.sender, buffered):
            return False
        self._acked.add((update.round, update.sender))
        return True

    async def receive(self, run_id: str, update: NeighborUpdate, meta: Mapping[str, str] | None = None) -> dict:
        if run_id == self.run_id and self.active and update.sender in self._peer_round:
            self._peer_round[update.sender] = max(self._peer_round[update.sender], update.round)
            self._peer_alive[update.sender] = True
            if update.round >= (self.epoch_start_round or 1):  # a stop agreed under an older chain is void
                self._absorb(meta or {})
        return await super().receive(run_id, update)

    def _absorb(self, meta: Mapping[str, str]) -> None:
        raw = meta.get(wire.H_VIEW)
        if raw:
            try:
                for rnd, edges in json.loads(raw).items():
                    mine = self.view.setdefault(int(rnd), {})
                    for e, v in edges.items():
                        if e in self.edges and e not in mine:
                            mine[e] = float(v)
            except (ValueError, TypeError, AttributeError):
                log.warning("ignoring malformed residual view header")
        stop = meta.get(wire.H_STOP)
        if stop and stop.isdigit():
            self._propose_stop(int(stop))

    def _propose_stop(self, r: int) -> None:
        if self.stop_at is None or r < self.stop_at:
            self.stop_at = r

    def note_rejoin(self, requester: str, rejoin_round: int) -> None:
        """A restarted neighbour resumes at rejoin_round: it will never send packets for earlier rounds.

        (_peer_round[m] > r means "m is past round r"; _wait never waits for such a packet.)"""
        self._peer_round[requester] = max(self._peer_round.get(requester, 0), rejoin_round)
        self._peer_alive[requester] = True

    # -- control ----------------------------------------------------------------
    async def start(self, run_id: str, rounds: int, initiated_by: str) -> str:
        outcome = await super().start(run_id, rounds, initiated_by)
        if outcome == "started":
            self.engine.reset(run_id)
            clear_checkpoint(self.checkpoint_path)
            self._reset_run_state()
            self._needs_link = set()  # every node starts from zero: all mirrors already agree
        return outcome

    # -- the loop -------------------------------------------------------------
    async def _loop(self) -> None:
        run_id = self.run_id
        try:
            r = self.start_round
            while r <= self.total_rounds:
                await self._resume.wait()
                if not await self._boundary(r):
                    self._left(run_id)
                    return
                self.current_round = r
                await self._link_new_neighbours(run_id, r)
                self.db.round_started(run_id, self.cfg.node_id, r, len(self.neighbours))
                if self.pos.group == HEAD:
                    local, completed, heard, missing = await self._head_round(run_id, r)
                else:
                    local, completed, heard, missing = await self._tail_round(run_id, r)
                await self._finish_round(run_id, r, local, completed, heard, missing)
                if self.stop_at is not None and r >= self.stop_at:
                    break
                r += 1
                if r <= self.total_rounds:
                    await asyncio.sleep(self.cfg.timing.round_pause_s)
            self.state = "completed"
            last = asdict(self.history[-1]) if self.history else {}
            self.db.audit(node_id=self.cfg.node_id, event="run_completed", run_id=run_id, round_num=self.current_round,
                          detail={"engine": self.engine.label, "converged_round": self.converged_round,
                                  "stopped_at": self.current_round, **last})
            log.info("run %s completed at round %d (consensus detected at %s) [%s]", run_id, self.current_round,
                     self.converged_round, self.engine.label)
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except Exception as exc:  # noqa: BLE001
            self.state = "failed"
            log.exception("run %s failed", run_id)
            self.db.audit(node_id=self.cfg.node_id, event="run_failed", run_id=run_id, round_num=self.current_round,
                          detail={"error": str(exc), "engine": self.engine.label})
        finally:
            self._finished_runs = (self._finished_runs + [run_id])[-32:]
            self._flush_pending()
            self.db.heartbeat(self.cfg.node_id, run_id, self.state, self.current_round)

    async def _head_round(self, run_id: str, r: int):
        # rebuild mirrors from any pending (late) tail packets of earlier rounds, then primal step,
        # quantize, send to both. Round-r packets stay buffered until round r's window closes, so every
        # packet answered on_time is exactly one that _wait returns.
        await self._apply(run_id, r, self._take(run_id, r - 1))
        local = await asyncio.to_thread(self.engine.train_round, r)
        send_ok, participating = await self._send_all(run_id, r)
        got = await self._wait(run_id, r, participating)
        current = await self._apply(run_id, r, self._take(run_id, r))
        received_ok = self._counted(r, got) & current
        missing = sorted(set(self.pos.neighbours) - set(got))
        return local, send_ok & received_ok, len(got), missing

    async def _tail_round(self, run_id: str, r: int):
        # barrier: wait for both heads' round-r packets (skip neighbours known to be down)
        expected = {m for m in self.pos.neighbours if self._peer_alive[m]}
        got = await self._wait(run_id, r, expected)
        current = await self._apply(run_id, r, self._take(run_id, r))
        received_ok = self._counted(r, got) & current
        local = await asyncio.to_thread(self.engine.train_round, r)
        send_ok, _ = await self._send_all(run_id, r)
        missing = sorted(set(self.pos.neighbours) - set(got))
        return local, send_ok & received_ok, len(got), missing

    async def _send_all(self, run_id: str, r: int) -> tuple[set[str], set[str]]:
        pkg = await asyncio.to_thread(self.engine.package_update, r)
        extra = {wire.H_SEQ: str(pkg.seq), wire.H_BASE: pkg.base or "", wire.H_PRODUCES: pkg.produces or "",
                 wire.H_VIEW: self._view_header()}
        if self.stop_at is not None:
            extra[wire.H_STOP] = str(self.stop_at)
        # no quantized packet on a link whose full snapshot exchange has not happened yet
        sent = await asyncio.gather(*(self._post_update(n, pkg, run_id, r, extra) for n in self.neighbours
                                      if n.id not in self._needs_link))
        for n, participating, _ in sent:
            self._peer_alive[n.id] = participating
        return {n.id for n, _, on_time in sent if on_time}, {n.id for n, participating, _ in sent if participating}

    def _view_header(self) -> str:
        keep = sorted(self.view)[-(CONVERGED_STREAK + len(self.pos.chain)):]
        return json.dumps({str(k): {e: round(v, 12) for e, v in self.view[k].items()} for k in keep},
                          separators=(",", ":"))

    async def _wait(self, run_id: str, r: int, expected: set[str]) -> dict[str, NeighborUpdate]:
        """Wait (bounded) for the round-r packets of `expected`, then close round r's window for everyone.

        A neighbour that has already moved past round r will never send its round-r packet, so it is not
        waited for. Time spent paused does not count against the timeout.
        """
        loop = asyncio.get_running_loop()
        timeout = self.cfg.timing.round_timeout_s
        deadline = loop.time() + timeout
        async with self._cond:
            while True:
                got = self._inbox.get((run_id, r), {})
                pending = {m for m in expected if m not in got and self._peer_round[m] <= r}
                if not pending:
                    break
                if self.state == "paused":
                    deadline = loop.time() + timeout
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._cond.wait(), min(remaining, 0.5))
                except asyncio.TimeoutError:
                    pass
            got = {s: u for s, u in self._inbox.get((run_id, r), {}).items() if s in self._closed_upto}
            for m in self._closed_upto:
                self._closed_upto[m] = max(self._closed_upto[m], r)
            late = {m for m in expected - got.keys() if self._peer_round[m] <= r}
        if late:
            log.warning("round %d: no packet from %s within %.1fs; reusing the stale mirror", r, sorted(late), timeout)
        return got

    def _counted(self, r: int, got: Mapping[str, NeighborUpdate]) -> set[str]:
        """Senders of round-r packets I answered on_time (the only ones the sender counts too)."""
        counted = {m for m in got if (r, m) in self._acked}
        self._acked = {k for k in self._acked if k[0] > r}
        return counted

    def _take(self, run_id: str, upto: int) -> list[NeighborUpdate]:
        """Buffered packets of this run up to round `upto`, from current neighbours only (a removed link's are dropped)."""
        out = []
        current = set(self.pos.neighbours)
        for key in sorted(k for k in self._inbox if k[0] != run_id or k[1] <= upto):
            ups = self._inbox.pop(key)
            if key[0] == run_id:
                out.extend(u for u in ups.values() if u.sender in current)
        return out

    async def _apply(self, run_id: str, r: int, updates: list[NeighborUpdate]) -> set[str]:
        """Fold packets into the mirrors; resync any mirror that no longer matches. Returns in-sync senders."""
        res = await asyncio.to_thread(self.engine.apply_neighbor_update, updates)
        ok = set(res.applied) | set(res.duplicate)
        for m in res.resync:
            if await self.resync_mirror(run_id, r, m, reason="packet base does not match my mirror"):
                ok.add(m)
                self._needs_link.discard(m)
            else:
                ok.discard(m)
        return ok

    async def _finish_round(self, run_id: str, r: int, local, completed: set[str], heard: int, missing: list[str]):
        await asyncio.to_thread(self.engine.dual_update, completed)
        residuals = await asyncio.to_thread(self.engine.residuals)
        self.view.setdefault(r, {}).update(residuals)
        for old in [k for k in self.view if k < r - 4 * len(self.pos.chain)]:
            self.view.pop(old)
        ev = await asyncio.to_thread(self.engine.evaluate)
        degraded = bool(missing)
        stale = sorted(edge_key(self.pos.edge_with(m)) for m in self.pos.neighbours if m not in completed)
        if degraded:
            self.db.audit(node_id=self.cfg.node_id, event="round_degraded", run_id=run_id, round_num=r,
                          detail={"missing": missing, "phase": self.pos.group, "stale_edges": stale,
                                  "reason": "no packet within round_timeout_s or neighbour down; reused the stale mirror",
                                  "engine": self.engine.label})
        if self.converged_round is None and self._detect_consensus():
            self._propose_stop(r + len(self.pos.chain) + 1)
            self.db.audit(node_id=self.cfg.node_id, event="consensus_detected", run_id=run_id, round_num=r,
                          detail={"converged_round": self.converged_round, "stop_at": self.stop_at,
                                  "epsilon": self.cfg.epsilon, "engine": self.engine.label, "epoch": self.epoch,
                                  "chain": list(self.pos.chain)})
        max_res = max(residuals.values()) if residuals else None
        m = RoundMetrics(r, ev.loss, ev.accuracy, local.train_loss, heard, len(self.neighbours),
                         max_residual=max_res, degraded=degraded)
        self.history.append(m)
        self.db.record_metrics(run_id=run_id, node_id=self.cfg.node_id, round_num=r, loss=ev.loss,
                               accuracy=ev.accuracy, train_loss=local.train_loss, n_train=local.n_train,
                               n_test=ev.n_test, epoch=self.epoch, chain_size=len(self.pos.chain),
                               residuals={"edges": residuals, "exchanged": sorted(completed), "stale_edges": stale,
                                          "degraded": degraded, "epsilon": self.cfg.epsilon,
                                          "group": self.pos.group, "bits": self.engine.bits, "epoch": self.epoch})
        self.db.round_finished(run_id, self.cfg.node_id, r, heard)
        self.db.heartbeat(self.cfg.node_id, run_id, self.state, r)
        if self.cfg.checkpoint_every and r % self.cfg.checkpoint_every == 0:
            size = await asyncio.to_thread(self.engine.save, self.checkpoint_path)
            log.info("round %d: checkpoint saved (%d bytes)", r, size)
        log.info("round %d/%d [%s] loss=%.4f max residual=%s exchanged=%s%s", r, self.total_rounds, self.pos.group,
                 ev.loss, f"{max_res:.2e}" if max_res is not None else "-", sorted(completed),
                 f" DEGRADED missing={missing}" if degraded else "")

    def _detect_consensus(self) -> bool:
        since = self.epoch_start_round or 1  # only rounds run on the current chain count
        full = [k for k in sorted(self.view) if k >= since and all(e in self.view[k] for e in self.edges)]
        if len(full) < CONVERGED_STREAK:
            return False
        last = full[-CONVERGED_STREAK:]
        if last != list(range(last[0], last[0] + CONVERGED_STREAK)):
            return False
        if converged([self.view[k] for k in last], self.edges, self.cfg.epsilon):
            self.converged_round = last[-1]
            return True
        return False

    # -- resync and rejoin (GET /snapshot) --------------------------------------
    async def _fetch_snapshot(self, m: str, rejoin_round: int | None = None) -> tuple[dict, int] | None:
        n = self.peer(m)
        if n is None:
            return None
        params = {"requester": self.cfg.node_id}
        if rejoin_round is not None:
            params["rejoin_round"] = str(rejoin_round)
        try:
            resp = await self.client.get(f"{n.url}/snapshot", params=params,
                                         headers={wire.H_SECRET: self.cfg.shared_secret})
        except httpx.HTTPError as exc:
            if pinning.is_pin_error(exc):
                self.tls_refused(m, exc, self.run_id, self.current_round)
            else:
                log.warning("snapshot from %s failed: %s", m, type(exc).__name__)
            return None
        if resp.status_code != 200:
            log.warning("snapshot from %s refused: %s", m, resp.status_code)
            return None
        body = resp.content
        if self.signing_key is not None:  # the snapshot replaces my mirror wholesale: it must really be m's
            try:
                security.verify(security.parse_public_key(n.public_key or ""), resp.headers.get(wire.H_SIGNATURE), body,
                                {k: v for k, v in resp.headers.items() if k.lower().startswith("x-medchain-")}, "snapshot")
            except (security.SignatureError, ValueError) as exc:
                log.error("snapshot from %s rejected: %s", m, exc)
                self.db.audit(node_id=self.cfg.node_id, event="snapshot_rejected", run_id=self.run_id, sender=m,
                              receiver=self.cfg.node_id, detail={"reason": str(exc)})
                return None
        snap = qengine.parse_snapshot(body, self.engine.d, int(resp.headers[wire.H_SEQ]),
                                      int(resp.headers[wire.H_MIRROR_SEQ]))
        return snap, len(body)

    async def resync_mirror(self, run_id: str, r: int, m: str, *, reason: str, rejoin_round: int | None = None) -> bool:
        got = await self._fetch_snapshot(m, rejoin_round)
        if got is None:
            return False
        snap, size = got
        await asyncio.to_thread(self.engine.adopt_mirror, m, snap["theta_hat"], snap["seq"])
        self.db.audit(node_id=self.cfg.node_id, event="snapshot_resync", run_id=run_id, round_num=r, sender=m,
                      receiver=self.cfg.node_id, payload_bytes=size,
                      detail={"reason": reason, "seq": snap["seq"], "bytes": size,
                              "note": "full theta_hat snapshot (model state only, never raw data)",
                              "engine": self.engine.label})
        log.warning("round %d: resynced mirror of %s from a full snapshot (%d bytes): %s", r, m, size, reason)
        return True

    async def rejoin(self, attempts: int = 20, delay_s: float = 1.0) -> str:
        """After a container restart: reload the checkpoint and rejoin the run the neighbours are still in."""
        if self.active:
            return "busy"
        try:
            restored = self.engine.restore(self.checkpoint_path)  # synchronously: no /round/start can interleave
        except Exception as exc:  # noqa: BLE001 - e.g. a checkpoint from a differently sized model
            log.warning("ignoring unreadable checkpoint %s: %s", self.checkpoint_path, exc)
            self.engine.reset()
            self.db.audit(node_id=self.cfg.node_id, event="checkpoint_not_resumed",
                          detail={"reason": f"unreadable checkpoint: {exc}", "engine": self.engine.label})
            return "unreadable-checkpoint"
        if not restored:
            return "no-checkpoint"
        st = self.engine.state
        run_id, ckpt_round = st.run_id, st.round_no
        added, _ = self.engine.relink(self.pos)  # the chain may have changed while I was down
        peers: dict[str, dict] = {}
        for _ in range(attempts):
            for n in self.neighbours:
                try:
                    s = (await self.client.get(f"{n.url}/status")).json()
                except (httpx.HTTPError, ValueError):
                    continue
                if s.get("run_id") == run_id and s.get("run_state") in ACTIVE:
                    peers[n.id] = s
            if peers or self.active:
                break
            await asyncio.sleep(delay_s)
        if self.active:
            return "busy"  # a new run started meanwhile
        if not peers:
            self.engine.reset()
            self.db.audit(node_id=self.cfg.node_id, event="checkpoint_not_resumed", run_id=run_id,
                          round_num=ckpt_round, detail={"reason": "no neighbour is still in this run",
                                                        "engine": self.engine.label})
            return "run-over"
        rejoin_round = max(int(s.get("current_round") or 0) for s in peers.values()) + 1
        total = max(int(s.get("total_rounds") or 0) for s in peers.values())
        self.run_id, self.total_rounds, self.current_round = run_id, total, rejoin_round - 1
        self.history = []
        self._reset_run_state(start_round=rejoin_round)
        self.epoch_start_round = rejoin_round
        self._needs_link = set(added)
        for s in peers.values():
            if s.get("stop_at"):
                self._propose_stop(int(s["stop_at"]))
        self.state = "running"
        self._resume.set()
        snaps, total_bytes = {}, 0
        for m in peers:
            got = await self._fetch_snapshot(m, rejoin_round)
            if got is not None:
                snaps[m], size = got
                total_bytes += size
                self.db.audit(node_id=self.cfg.node_id, event="snapshot_resync", run_id=run_id,
                              round_num=rejoin_round, sender=m, receiver=self.cfg.node_id, payload_bytes=size,
                              detail={"reason": "rejoin after restart", "seq": snaps[m]["seq"], "bytes": size,
                                      "note": "full theta_hat snapshot (model state only, never raw data)",
                                      "engine": self.engine.label})
        await asyncio.to_thread(self.engine.adopt_rejoin, snaps)
        self.db.audit(node_id=self.cfg.node_id, event="rejoined_from_checkpoint", run_id=run_id,
                      round_num=rejoin_round,
                      detail={"checkpoint_round": ckpt_round, "rejoin_round": rejoin_round,
                              "resynced_from": sorted(snaps), "resync_bytes": total_bytes,
                              "engine": self.engine.label})
        self.db.heartbeat(self.cfg.node_id, run_id, self.state, self.current_round)
        log.warning("rejoined run %s at round %d from the round-%d checkpoint (resync %d bytes from %s)", run_id,
                    rejoin_round, ckpt_round, total_bytes, sorted(snaps))
        self.launch()
        return "rejoined"
