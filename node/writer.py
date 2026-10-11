"""Telemetry off the round loop: every write is queued and sent by a background thread, so training never waits.

The call sites (`db.heartbeat(...)`, `db.audit(...)`, ...) are the same as for the
Database class in node/db.py, whatever the sink:

- `ObserverSink`: batches POSTed to the observer service (observer/) at OBSERVER_URL,
  with its ingest token. A node on another machine needs only that URL and token,
  never Postgres credentials. This is the default, on one machine and across machines.
- any `Database` (node/db.py): Postgres directly, used when DATABASE_URL is set and
  OBSERVER_URL is not, or a recorder in the tests.
- `SqliteAudit`: a copy of every audit() call in `<data_path>/audit.db`, kept on the
  hospital's own machine whatever the observer does.

Each sink has its own bounded queue (a deque). When a queue is full the oldest item is
dropped, never the newest, with a rate-limited warning. While the observer is
unreachable its sender retries the same batch with a growing pause, and new items
keep queueing behind it. Nothing here is ever awaited by a round.
"""

from __future__ import annotations

import inspect
import json
import logging
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

import httpx

from .db import Database

log = logging.getLogger("medchain.db")

WRITE_METHODS = {
    "ensure_schema", "register_node", "heartbeat", "create_run", "round_started", "round_finished",
    "update_sent", "record_metrics", "audit", "membership", "hospital_key", "hospital_forgotten",
}
# writes only the hub's own services make: never accepted from a node through the observer's /ingest
HUB_ONLY = {"ensure_schema", "hospital_key", "hospital_forgotten"}
BATCH = 200


def _jsonable(o):
    if hasattr(o, "item"):  # numpy scalars
        return o.item()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"{type(o).__name__} is not JSON serializable")


class ObserverSink:
    """POST batches of writes to the observer's /ingest. Raises on failure so the batch is retried."""

    def __init__(self, url: str, token: str, node_id: str, client: httpx.Client):
        self.url, self.token, self.node_id, self.client = url.rstrip("/"), token, node_id, client

    def write_batch(self, items: list[tuple[str, dict]]) -> None:
        body = json.dumps({"node_id": self.node_id, "items": [{"kind": k, "payload": p} for k, p in items]},
                          default=_jsonable)
        resp = self.client.post(f"{self.url}/ingest", content=body,
                                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        if resp.status_code != 200:
            raise RuntimeError(f"observer answered {resp.status_code}: {resp.text[:200]}")

    def close(self) -> None:
        self.client.close()


class SqliteAudit:
    """The node's own audit trail in a local SQLite file (same columns as the observer's audit_logs)."""

    SCHEMA = """CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, node_id TEXT NOT NULL, event TEXT NOT NULL,
        run_id TEXT, round INTEGER, sender TEXT, receiver TEXT, payload_bytes INTEGER, checksum TEXT,
        detail TEXT NOT NULL DEFAULT '{}')"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:  # opened on the writer thread
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(self.SCHEMA)
        return self._conn

    def write_batch(self, items: list[tuple[str, dict]]) -> None:
        rows = [(time.time(), kw["node_id"], kw["event"], kw.get("run_id"), kw.get("round_num"), kw.get("sender"),
                 kw.get("receiver"), kw.get("payload_bytes"), kw.get("checksum"),
                 json.dumps(kw.get("detail") or {}, default=_jsonable)) for name, kw in items if name == "audit"]
        db = self._db()
        db.execute("BEGIN")
        db.executemany("INSERT INTO audit_logs (ts, node_id, event, run_id, round, sender, receiver, payload_bytes, "
                       "checksum, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        db.execute("COMMIT")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()


def _one_by_one(db) -> Callable[[list[tuple[str, dict]]], None]:
    """A Database-like sink without batches: call each write method (they log their own failures)."""

    def write(items):
        for name, kwargs in items:
            try:
                getattr(db, name)(**kwargs)
            except Exception:  # noqa: BLE001
                log.exception("db %s failed", name)

    return write


class _Lane:
    """One sink: a bounded deque (drop-oldest) drained in batches by a daemon thread."""

    def __init__(self, name: str, sink, maxsize: int, retry_max_s: float = 5.0):
        self.name, self.sink, self.maxsize, self.retry_max_s = name, sink, maxsize, retry_max_s
        self._write = sink.write_batch if hasattr(sink, "write_batch") else _one_by_one(sink)
        self._q: deque = deque(maxlen=maxsize)
        self._cond = threading.Condition()
        self._stopping = False
        self.dropped = 0
        self._warned_at = 0.0
        self._thread = threading.Thread(target=self._run, name=f"telemetry-{name}", daemon=True)

    def put(self, item) -> None:
        with self._cond:
            if len(self._q) == self.maxsize:  # the deque drops the oldest on append
                self.dropped += 1
                now = time.monotonic()
                if now - self._warned_at > 10:
                    self._warned_at = now
                    log.warning("%s queue full (%d); dropping the oldest writes (%d so far)", self.name,
                                self.maxsize, self.dropped)
            self._q.append(item)
            self._cond.notify()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify()

    def _run(self) -> None:
        if hasattr(self.sink, "wait_until_ready"):
            self.sink.wait_until_ready(timeout_s=float("inf"))
        pause = 0.5
        batch: list = []
        while True:
            if not batch:
                with self._cond:
                    while not self._q and not self._stopping:
                        self._cond.wait()
                    if not self._q:
                        return
                    batch = [self._q.popleft() for _ in range(min(BATCH, len(self._q)))]
            try:
                self._write(batch)
                batch, pause = [], 0.5
            except Exception as exc:  # noqa: BLE001 - observation channel: retry, never raise into training
                log.warning("%s write of %d items failed (%s); retrying in %.1fs", self.name, len(batch), exc, pause)
                with self._cond:
                    if self._stopping:
                        return
                    self._cond.wait(pause)
                pause = min(pause * 2, self.retry_max_s)


class DbWriter:
    def __init__(self, db, maxsize: int = 10_000, *, audit_db: str | Path | None = None):
        self.db = db
        self._lane = _Lane("observer" if isinstance(db, ObserverSink) else "db", db, maxsize)
        # a local disk keeps up; its own queue, so an observer outage never costs the local audit trail
        self._audit = _Lane("audit.db", SqliteAudit(audit_db), max(maxsize, 10_000)) if audit_db else None

    def start(self) -> None:
        self._lane.start()
        if self._audit:
            self._audit.start()

    def stop(self) -> None:
        self._lane.stop()
        if self._audit:
            self._audit.stop()

    @property
    def dropped(self) -> int:
        return self._lane.dropped

    def __getattr__(self, name):
        if name not in WRITE_METHODS:
            raise AttributeError(name)

        def enqueue(*args, **kwargs):
            if args:
                params = list(inspect.signature(getattr(Database, name)).parameters)[1:]  # without self
                kwargs = {**dict(zip(params, args)), **kwargs}
            self._lane.put((name, kwargs))
            if name == "audit" and self._audit:
                self._audit.put((name, kwargs))

        return enqueue

