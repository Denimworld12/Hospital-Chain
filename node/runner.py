"""Per-node round loop. Peer-to-peer: no central coordinator.

A run starts when any node receives POST /round/start; it floods the start to its
neighbours (who flood onward) and then every node runs its own loop:

    train locally -> POST /update to each neighbour -> wait (bounded) for the
    neighbours' updates for this round -> apply them -> evaluate -> log

A neighbour that is unreachable, or not taking part in the run, is simply not
waited for, so the surviving nodes keep training when one goes down. Only the
engine's serialized weight arrays ever leave this process.

Membership (who my neighbours are) is a `Membership` document (node/membership.py):
the static chain from my config, or the directory's signed documents. A new document
never changes anything mid-round: it waits for the round boundary it names
(`effective_round`), or applies at once when no run is in flight. A hospital that is
not in the new chain leaves the run at that boundary.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Mapping

import httpx

from . import pinning, security, wire
from .config import Neighbour, NodeConfig
from .engine import Engine, NeighborUpdate, UpdatePackage
from .membership import Member, Membership, link_key
from .writer import DbWriter

log = logging.getLogger("medchain.runner")

ACTIVE = ("running", "paused")


@dataclass
class RoundMetrics:
    round: int
    loss: float
    accuracy: float
    train_loss: float
    neighbours_heard: int
    neighbours_expected: int
    # ADMM engines only
    max_residual: float | None = None
    degraded: bool = False


def new_run_id() -> str:
    return f"run-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{secrets.token_hex(2)}"


def static_membership(cfg: NodeConfig, public_key: str | None = None, tls_pin: str | None = None) -> Membership:
    """Epoch 0: the chain written in my own config. With a directory: no chain until I have registered."""
    me = Member(cfg.node_id, cfg.advertise_url, public_key, cfg.display_name, tls_pin)
    if cfg.directory:
        return Membership(epoch=-1, chain=(), members={cfg.node_id: me}, reason="not registered with the directory yet")
    nbs = [n.id for n in cfg.neighbours]
    chain = cfg.chain or tuple(nbs[:1] + [cfg.node_id] + nbs[1:])
    members = {n.id: Member(n.id, n.url, n.public_key, tls_pin=n.tls_pin) for n in cfg.neighbours}
    members[cfg.node_id] = me
    for other in chain:  # chain members that are not my neighbours: no address needed, I never call them
        members.setdefault(other, Member(other, ""))
    return Membership(epoch=0, chain=tuple(chain), members=members, reason="static chain from the node config")


class Runner:
    INBOX_ROUNDS = 64  # rounds of neighbour updates kept buffered

    def __init__(self, cfg: NodeConfig, engine: Engine, db: DbWriter, client: httpx.AsyncClient, *,
                 signing_key=None, membership: Membership | None = None):
        self.cfg = cfg
        self.engine = engine
        self.db = db
        self.client = client
        self.signing_key = signing_key
        self.public_key = security.public_key_b64(signing_key) if signing_key is not None else None
        self.tls_pin = pinning.cert_file_pin(cfg.tls.cert) if cfg.tls else None
        self.state = "idle"
        self.run_id: str | None = None
        self.total_rounds = 0
        self.current_round = 0
        self.start_round = 1
        self.history: list[RoundMetrics] = []
        self.membership: Membership | None = None
        self._pending: list[Membership] = []
        self.epoch_start_round: int | None = None  # round the current membership took effect in this run
        self.neighbours: tuple[Neighbour, ...] = ()
        self.neighbour_status: dict[str, str] = {}
        self._inbox: dict[tuple[str, int], dict[str, NeighborUpdate]] = {}
        self._cond = asyncio.Condition()
        self._resume = asyncio.Event()
        self._resume.set()
        self._task: asyncio.Task | None = None
        self._finished_runs: list[str] = []
        self._seen_ops: set[str] = set()
        self._set_membership(membership or static_membership(cfg, self.public_key, self.tls_pin))

    # -- membership -----------------------------------------------------------
    @property
    def epoch(self) -> int:
        return self.membership.epoch if self.membership else -1

    @property
    def known_epoch(self) -> int:
        return max([self.epoch] + [d.epoch for d in self._pending])

    @property
    def in_chain(self) -> bool:
        return bool(self.membership and self.membership.has(self.cfg.node_id))

    def neighbour(self, node_id: str) -> Neighbour | None:
        return next((n for n in self.neighbours if n.id == node_id), None)

    def peer(self, node_id: str) -> Member | None:
        """A current or scheduled neighbour (the only senders whose packets I accept)."""
        for doc in [*reversed(self._pending), self.membership]:
            if doc and node_id in doc.neighbours_of(self.cfg.node_id):
                return doc.members[node_id]
        return None

    def member(self, node_id: str) -> Member | None:
        """Any member of my current or scheduled chain."""
        for doc in [*reversed(self._pending), self.membership]:
            if doc and node_id in doc.members:
                return doc.members[node_id]
        return None

    def known_parties(self) -> list[tuple[str, str, str, str | None]]:
        """(id, label, url, TLS pin) of every member of my current and scheduled chains: whom I may dial."""
        return [(m.id, m.display_name or m.id, m.url, m.tls_pin)
                for doc in [*reversed(self._pending), self.membership] if doc for m in doc.members.values()]

    def _set_membership(self, doc: Membership) -> None:
        self.membership = doc
        ids = doc.neighbours_of(self.cfg.node_id)
        self.neighbours = tuple(Neighbour(m, doc.members[m].url, doc.members[m].public_key, doc.members[m].tls_pin)
                                for m in ids)
        self.neighbour_status = {m: self.neighbour_status.get(m, "unknown") for m in ids}

    def offer_membership(self, doc: Membership) -> str:
        """Take a (verified) membership document: apply it now, or schedule it for its round boundary."""
        if doc.epoch <= self.known_epoch:
            return "stale"
        only_addresses = not self._pending and self.membership is not None and doc.same_chain(self.membership)
        if not self.active or only_addresses:
            for d in [*self._pending, doc]:  # nothing is in flight (or only addresses changed): apply now
                self._apply_membership(d, None)
            self._pending = []
            return "applied"
        # mid-run, a chain change always waits for a round boundary (the next one if none is named)
        self._pending = sorted(self._pending + [doc], key=lambda d: d.epoch)
        late = (doc.run_id == self.run_id and doc.effective_round is not None
                and doc.effective_round <= self.current_round)
        if late:
            log.warning("membership epoch %d was due at round %d but I am already in round %d; applying it at "
                        "the next boundary", doc.epoch, doc.effective_round, self.current_round)
            self.db.audit(node_id=self.cfg.node_id, event="membership_late", run_id=self.run_id,
                          round_num=self.current_round, detail={"epoch": doc.epoch, "due": doc.effective_round})
        return "late" if late else "scheduled"

    def _due(self, r: int) -> list[Membership]:
        """The pending documents to apply before round r, in epoch order: none overtakes an earlier one."""
        n = 0
        for d in self._pending:
            if d.run_id == self.run_id and d.effective_round is not None and d.effective_round > r:
                break
            n += 1
        due, self._pending = self._pending[:n], self._pending[n:]
        return due

    def _apply_membership(self, doc: Membership, r: int | None) -> None:
        """Switch to `doc` at the boundary before round r (r=None: between runs)."""
        old = self.membership
        before = set(old.neighbours_of(self.cfg.node_id)) if old else set()
        self._set_membership(doc)
        after = set(doc.neighbours_of(self.cfg.node_id))
        if r is not None:
            self.epoch_start_round = r
        detail = {"epoch": doc.epoch, "chain": list(doc.chain), "reason": doc.reason,
                  "effective_round": r, "added_links": sorted(link_key(self.cfg.node_id, m) for m in after - before),
                  "removed_links": sorted(link_key(self.cfg.node_id, m) for m in before - after),
                  "in_chain": self.in_chain}
        self.db.audit(node_id=self.cfg.node_id, event="membership_changed", run_id=self.run_id if r else None,
                      round_num=r, detail=detail)
        self.db.membership(node_id=self.cfg.node_id, epoch=doc.epoch, chain=list(doc.chain),
                           neighbours=sorted(after, key=list(doc.chain).index), in_chain=self.in_chain,
                           membership_round=r)
        log.info("membership epoch %d %s: chain %s, neighbours %s (%s)", doc.epoch,
                 f"from round {r}" if r else "now", " - ".join(doc.chain), sorted(after), doc.reason)
        if not self.in_chain and old and old.has(self.cfg.node_id):
            self.db.audit(node_id=self.cfg.node_id, event="left_chain", run_id=self.run_id if r else None,
                          round_num=r, detail={"epoch": doc.epoch, "reason": doc.reason})

    def _flush_pending(self) -> None:
        for d in self._pending:
            self._apply_membership(d, None)
        self._pending = []

    # -- inbound ------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    async def receive(self, run_id: str, update: NeighborUpdate, meta: Mapping[str, str] | None = None) -> dict:
        """Buffer a neighbour update (`meta`: engine-specific headers; unused by the gossip baseline).

        Returns {"participating": whether this node is taking part in run_id,
        "on_time": whether the update will be counted in its round}.
        """
        if run_id in self._finished_runs:
            return {"participating": False, "on_time": False}
        async with self._cond:
            self._inbox.setdefault((run_id, update.round), {})[update.sender] = update
            # keep the buffer bounded: drop stale rounds / other runs
            if len(self._inbox) > self.INBOX_ROUNDS:
                excess = len(self._inbox) - self.INBOX_ROUNDS
                for key in sorted(self._inbox, key=lambda k: (k[0] != self.run_id, k[1]))[:excess]:
                    self._inbox.pop(key, None)
            on_time = self._on_time(run_id, update)
            self._cond.notify_all()
        participating = self.active and run_id == self.run_id
        return {"participating": participating, "on_time": participating and on_time}

    def _on_time(self, run_id: str, update: NeighborUpdate) -> bool:
        return True

    def seen_op(self, op_id: str) -> bool:
        if op_id in self._seen_ops:
            return True
        self._seen_ops.add(op_id)
        if len(self._seen_ops) > 512:
            self._seen_ops = set(list(self._seen_ops)[-256:])
        return False

    # -- control --------------------------------------------------------------
    async def start(self, run_id: str, rounds: int, initiated_by: str) -> str:
        if self.active:
            return "already-running" if run_id == self.run_id else "busy"
        if run_id in self._finished_runs:
            return "already-finished"
        self._flush_pending()
        if not self.in_chain:
            return "not-in-chain"
        self.engine.reset()
        self.run_id = run_id
        self.total_rounds = rounds
        self.current_round = 0
        self.start_round = 1
        self.epoch_start_round = 1
        self.history = []
        self.state = "running"
        self._resume.set()
        self.db.create_run(run_id, rounds, self.engine.label, initiated_by)
        self.db.audit(node_id=self.cfg.node_id, event="run_started", run_id=run_id,
                      detail={"rounds": rounds, "engine": self.engine.label, "initiated_by": initiated_by,
                              "epoch": self.epoch, "chain": list(self.membership.chain)})
        self.db.heartbeat(self.cfg.node_id, run_id, self.state, 0)
        log.info("run %s started: %d rounds, engine=%s", run_id, rounds, self.engine.label)
        return "started"

    async def join_run(self, doc: Membership, attempts: int = 30, delay_s: float = 0.5) -> str:
        """I was just appended to a chain that is mid-run: join that run at the boundary the directory named.

        The run's id and length come from my new neighbour (GET /status); my links start from scratch
        (the engine adds lambda = 0 and a full snapshot exchange at the first round).
        """
        if self.active:
            return "busy"
        peers: dict[str, dict] = {}
        for _ in range(attempts):
            for n in self.neighbours:
                try:
                    s = (await self.client.get(f"{n.url}/status")).json()
                except (httpx.HTTPError, ValueError):
                    continue
                if s.get("run_id") == doc.run_id and s.get("run_state") in ACTIVE:
                    peers[n.id] = s
            if peers or self.active:
                break
            await asyncio.sleep(delay_s)
        if self.active:
            return "busy"
        now = max([int(s.get("current_round") or 0) for s in peers.values()] or [0])
        start = max(doc.effective_round or 1, now + 1)
        stops = [int(s["stop_at"]) for s in peers.values() if s.get("stop_at")]
        if not peers or (stops and min(stops) < start):
            log.info("joined the chain at epoch %d; its run %s ends before I could join, so I start with the next "
                     "run", doc.epoch, doc.run_id)
            return "run-over"
        self.run_id, self.total_rounds = doc.run_id, max(int(s.get("total_rounds") or 0) for s in peers.values())
        self.current_round, self.start_round, self.epoch_start_round = start - 1, start, start
        self.history = []
        self.engine.reset()
        await self._prepare_join(peers)
        self.state = "running"
        self._resume.set()
        self.db.create_run(self.run_id, self.total_rounds, self.engine.label, "join")
        self.db.audit(node_id=self.cfg.node_id, event="joined_run", run_id=self.run_id, round_num=start,
                      detail={"epoch": doc.epoch, "chain": list(doc.chain), "start_round": start,
                              "neighbours": sorted(peers), "engine": self.engine.label})
        self.db.heartbeat(self.cfg.node_id, self.run_id, self.state, self.current_round)
        log.warning("joined run %s at round %d as position %d of %d", self.run_id, start,
                    doc.chain.index(self.cfg.node_id) + 1, len(doc.chain))
        self.launch()
        return "joined"

    async def _prepare_join(self, peers: Mapping[str, dict]) -> None:
        """Engine-specific set-up before a mid-run join's first round."""

    def launch(self) -> None:
        self._task = asyncio.create_task(self._loop(), name=f"run-{self.run_id}")

    def pause(self) -> bool:
        if self.state != "running":
            return False
        self.state = "paused"
        self._resume.clear()
        self.db.audit(node_id=self.cfg.node_id, event="run_paused", run_id=self.run_id, round_num=self.current_round)
        self.db.heartbeat(self.cfg.node_id, self.run_id, self.state, self.current_round)
        return True

    def resume(self) -> bool:
        if self.state != "paused":
            return False
        self.state = "running"
        self._resume.set()
        self.db.audit(node_id=self.cfg.node_id, event="run_resumed", run_id=self.run_id, round_num=self.current_round)
        self.db.heartbeat(self.cfg.node_id, self.run_id, self.state, self.current_round)
        return True

    async def shutdown(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # -- the loop -------------------------------------------------------------
    async def _boundary(self, r: int) -> bool:
        """Round boundary: apply the membership documents due at round r. False if I left the chain."""
        for doc in self._due(r):
            self._apply_membership(doc, r)
        return self.in_chain

    def _left(self, run_id: str) -> None:
        self.state = "left"
        log.warning("left the chain at the start of round %d (epoch %d)", self.current_round + 1, self.epoch)

    async def _loop(self) -> None:
        run_id = self.run_id
        try:
            for r in range(self.start_round, self.total_rounds + 1):
                await self._resume.wait()
                if not await self._boundary(r):
                    self._left(run_id)
                    return
                self.current_round = r
                self.db.round_started(run_id, self.cfg.node_id, r, len(self.neighbours))

                local = await asyncio.to_thread(self.engine.train_round, r)
                pkg = await asyncio.to_thread(self.engine.package_update, r)
                sent = await asyncio.gather(*(self._send(n, pkg, run_id, r) for n in self.neighbours))
                expected = {n.id for n, participating in sent if participating}
                received = await self._collect(run_id, r, expected)
                await asyncio.to_thread(self.engine.apply_neighbor_update, list(received.values()))
                ev = await asyncio.to_thread(self.engine.evaluate)

                m = RoundMetrics(r, ev.loss, ev.accuracy, local.train_loss, len(received), len(expected))
                self.history.append(m)
                self.db.record_metrics(run_id=run_id, node_id=self.cfg.node_id, round_num=r, loss=ev.loss,
                                       accuracy=ev.accuracy, train_loss=local.train_loss, n_train=local.n_train,
                                       n_test=ev.n_test, epoch=self.epoch, chain_size=len(self.membership.chain))
                self.db.round_finished(run_id, self.cfg.node_id, r, len(received))
                self.db.heartbeat(self.cfg.node_id, run_id, self.state, r)
                log.info("round %d/%d loss=%.4f acc=%.3f heard %d/%d neighbours [%s]", r, self.total_rounds,
                         ev.loss, ev.accuracy, len(received), len(self.neighbours), self.engine.label)
                self._drop_inbox(run_id, r)
                if r < self.total_rounds:
                    await asyncio.sleep(self.cfg.timing.round_pause_s)
            self.state = "completed"
            self.db.audit(node_id=self.cfg.node_id, event="run_completed", run_id=run_id, round_num=self.total_rounds,
                          detail={"engine": self.engine.label, **(asdict(self.history[-1]) if self.history else {})})
            log.info("run %s completed [%s]", run_id, self.engine.label)
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except Exception as exc:  # noqa: BLE001
            self.state = "failed"
            log.exception("run %s failed", run_id)
            self.db.audit(node_id=self.cfg.node_id, event="run_failed", run_id=run_id, round_num=self.current_round,
                          detail={"error": str(exc)})
        finally:
            self._finished_runs = (self._finished_runs + [run_id])[-32:]
            self._flush_pending()
            self.db.heartbeat(self.cfg.node_id, run_id, self.state, self.current_round)

    def signed_headers(self, body: bytes, headers: dict[str, str], context: str = "update") -> dict[str, str]:
        if self.signing_key is not None:
            headers[wire.H_SIGNATURE] = security.sign(self.signing_key, body, headers, context)
        return headers

    async def _send(self, n: Neighbour, pkg: UpdatePackage, run_id: str, r: int,
                    extra_headers: dict[str, str] | None = None) -> tuple[Neighbour, bool]:
        n, participating, _ = await self._post_update(n, pkg, run_id, r, extra_headers)
        return n, participating

    async def _post_update(self, n: Neighbour, pkg: UpdatePackage, run_id: str, r: int,
                           extra_headers: dict[str, str] | None = None) -> tuple[Neighbour, bool, bool]:
        """POST one signed update; returns (neighbour, participating, on_time) and logs it to updates_meta."""
        headers = self.signed_headers(pkg.body, {
            wire.H_RUN: run_id,
            wire.H_ROUND: str(r),
            wire.H_SENDER: self.cfg.node_id,
            wire.H_RECEIVER: n.id,
            wire.H_EPOCH: str(self.epoch),
            wire.H_CHECKSUM: pkg.checksum,
            wire.H_DTYPE: pkg.dtype,
            **(extra_headers or {}),
        })
        headers |= {wire.H_SECRET: self.cfg.shared_secret, "Content-Type": "application/octet-stream"}
        participating = on_time = False
        try:
            resp = await self.client.post(f"{n.url}/update", content=pkg.body, headers=headers)
            if resp.status_code == 200:
                status = "delivered"
                answer = resp.json()
                participating = bool(answer.get("participating"))
                on_time = bool(answer.get("on_time", participating))
            else:
                status = f"rejected-{resp.status_code}"
        except httpx.HTTPError as exc:
            status = "tls-key-mismatch" if pinning.is_pin_error(exc) else "unreachable"
            if status == "unreachable":
                log.warning("round %d: neighbour %s unreachable (%s)", r, n.id, type(exc).__name__)
            else:
                self.tls_refused(n.id, exc, run_id, r)
        self.neighbour_status[n.id] = status
        self.db.update_sent(run_id=run_id, round_num=r, sender=self.cfg.node_id, receiver=n.id,
                            payload_bytes=pkg.payload_bytes if status == "delivered" else 0,
                            full_precision_bytes=pkg.full_precision_bytes, bits=pkg.bits, dtype=pkg.dtype,
                            checksum=pkg.checksum, status=status)
        if status not in ("delivered", "tls-key-mismatch"):
            self.db.audit(node_id=self.cfg.node_id, event="neighbour_unreachable" if status == "unreachable" else "update_rejected",
                          run_id=run_id, round_num=r, sender=self.cfg.node_id, receiver=n.id, detail={"status": status})
        return n, participating, on_time

    def tls_refused(self, peer: str, exc: BaseException, run_id: str | None = None, r: int | None = None) -> None:
        """A neighbour's certificate key is not the one on record: refused before anything was sent."""
        log.error("refused to send to %s: %s", peer, exc)
        self.db.audit(node_id=self.cfg.node_id, event="tls_pin_mismatch", run_id=run_id, round_num=r,
                      sender=self.cfg.node_id, receiver=peer, detail={"reason": str(exc)})

    async def _collect(self, run_id: str, r: int, expected: set[str]) -> dict[str, NeighborUpdate]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.timing.round_timeout_s
        neighbour_ids = {n.id for n in self.neighbours}
        async with self._cond:
            while True:
                got = self._inbox.get((run_id, r), {})
                remaining = deadline - loop.time()
                if expected <= got.keys() or remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._cond.wait(), remaining)
                except asyncio.TimeoutError:
                    break
            got = dict(self._inbox.get((run_id, r), {}))
        missing = expected - got.keys()
        if missing:
            log.warning("round %d: no update from %s within %.1fs; continuing without", r, sorted(missing),
                        self.cfg.timing.round_timeout_s)
        return {s: u for s, u in got.items() if s in neighbour_ids}

    def _drop_inbox(self, run_id: str, r: int) -> None:
        for key in [k for k in self._inbox if k[0] != run_id or k[1] <= r]:
            self._inbox.pop(key, None)
