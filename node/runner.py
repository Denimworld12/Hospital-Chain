"""Per-node round loop. Peer-to-peer: no central coordinator.

A run starts when any node receives POST /round/start; it floods the start to its
neighbours (who flood onward) and then every node runs its own loop:

    train locally -> POST /update to each neighbour -> wait (bounded) for the
    neighbours' updates for this round -> apply them -> evaluate -> log

A neighbour that is unreachable, or not taking part in the run, is simply not
waited for, so the surviving nodes keep training when one goes down. Only the
engine's serialized weight arrays ever leave this process.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import asdict, dataclass

import httpx

from . import wire
from .config import Neighbour, NodeConfig
from .engine import Engine, NeighborUpdate, UpdatePackage
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


def new_run_id() -> str:
    return f"run-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{secrets.token_hex(2)}"


class Runner:
    def __init__(self, cfg: NodeConfig, engine: Engine, db: DbWriter, client: httpx.AsyncClient):
        self.cfg = cfg
        self.engine = engine
        self.db = db
        self.client = client
        self.state = "idle"
        self.run_id: str | None = None
        self.total_rounds = 0
        self.current_round = 0
        self.history: list[RoundMetrics] = []
        self.neighbour_status: dict[str, str] = {n.id: "unknown" for n in cfg.neighbours}
        self._inbox: dict[tuple[str, int], dict[str, NeighborUpdate]] = {}
        self._cond = asyncio.Condition()
        self._resume = asyncio.Event()
        self._resume.set()
        self._task: asyncio.Task | None = None
        self._finished_runs: list[str] = []
        self._seen_ops: set[str] = set()

    # -- inbound ------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    async def receive(self, run_id: str, update: NeighborUpdate) -> bool:
        """Buffer a neighbour update. Returns whether this node is taking part in run_id."""
        if run_id in self._finished_runs:
            return False
        async with self._cond:
            self._inbox.setdefault((run_id, update.round), {})[update.sender] = update
            # keep the buffer bounded: drop stale rounds / other runs
            if len(self._inbox) > 64:
                for key in sorted(self._inbox, key=lambda k: (k[0] != self.run_id, k[1]))[: len(self._inbox) - 64]:
                    self._inbox.pop(key, None)
            self._cond.notify_all()
        return self.active and run_id == self.run_id

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
        self.engine.reset()
        self.run_id = run_id
        self.total_rounds = rounds
        self.current_round = 0
        self.history = []
        self.state = "running"
        self._resume.set()
        self.db.create_run(run_id, rounds, self.engine.label, initiated_by)
        self.db.audit(node_id=self.cfg.node_id, event="run_started", run_id=run_id,
                      detail={"rounds": rounds, "engine": self.engine.label, "initiated_by": initiated_by})
        self.db.heartbeat(self.cfg.node_id, run_id, self.state, 0)
        log.info("run %s started: %d rounds, engine=%s", run_id, rounds, self.engine.label)
        return "started"

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
    async def _loop(self) -> None:
        run_id = self.run_id
        try:
            for r in range(1, self.total_rounds + 1):
                await self._resume.wait()
                self.current_round = r
                self.db.round_started(run_id, self.cfg.node_id, r, len(self.cfg.neighbours))

                local = await asyncio.to_thread(self.engine.train_round, r)
                pkg = await asyncio.to_thread(self.engine.package_update, r)
                sent = await asyncio.gather(*(self._send(n, pkg, run_id, r) for n in self.cfg.neighbours))
                expected = {n.id for n, participating in sent if participating}
                received = await self._collect(run_id, r, expected)
                await asyncio.to_thread(self.engine.apply_neighbor_update, list(received.values()))
                ev = await asyncio.to_thread(self.engine.evaluate)

                m = RoundMetrics(r, ev.loss, ev.accuracy, local.train_loss, len(received), len(expected))
                self.history.append(m)
                self.db.record_metrics(run_id=run_id, node_id=self.cfg.node_id, round_num=r, loss=ev.loss,
                                       accuracy=ev.accuracy, train_loss=local.train_loss, n_train=local.n_train,
                                       n_test=ev.n_test)
                self.db.round_finished(run_id, self.cfg.node_id, r, len(received))
                self.db.heartbeat(self.cfg.node_id, run_id, self.state, r)
                log.info("round %d/%d loss=%.4f acc=%.3f heard %d/%d neighbours [%s]", r, self.total_rounds,
                         ev.loss, ev.accuracy, len(received), len(self.cfg.neighbours), self.engine.label)
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
            self.db.heartbeat(self.cfg.node_id, run_id, self.state, self.current_round)

    async def _send(self, n: Neighbour, pkg: UpdatePackage, run_id: str, r: int) -> tuple[Neighbour, bool]:
        headers = {
            wire.H_RUN: run_id,
            wire.H_ROUND: str(r),
            wire.H_SENDER: self.cfg.node_id,
            wire.H_CHECKSUM: pkg.checksum,
            wire.H_DTYPE: pkg.dtype,
            wire.H_SECRET: self.cfg.shared_secret,
            "Content-Type": "application/octet-stream",
        }
        participating = False
        try:
            resp = await self.client.post(f"{n.url}/update", content=pkg.body, headers=headers)
            if resp.status_code == 200:
                status = "delivered"
                participating = bool(resp.json().get("participating"))
            else:
                status = f"rejected-{resp.status_code}"
        except httpx.HTTPError as exc:
            status = "unreachable"
            log.warning("round %d: neighbour %s unreachable (%s)", r, n.id, type(exc).__name__)
        self.neighbour_status[n.id] = status
        self.db.update_sent(run_id=run_id, round_num=r, sender=self.cfg.node_id, receiver=n.id,
                            payload_bytes=pkg.payload_bytes if status == "delivered" else 0,
                            full_precision_bytes=pkg.full_precision_bytes, bits=pkg.bits, dtype=pkg.dtype,
                            checksum=pkg.checksum, status=status)
        if status != "delivered":
            self.db.audit(node_id=self.cfg.node_id, event="neighbour_unreachable" if status == "unreachable" else "update_rejected",
                          run_id=run_id, round_num=r, sender=self.cfg.node_id, receiver=n.id, detail={"status": status})
        return n, participating

    async def _collect(self, run_id: str, r: int, expected: set[str]) -> dict[str, NeighborUpdate]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.timing.round_timeout_s
        neighbour_ids = {n.id for n in self.cfg.neighbours}
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
