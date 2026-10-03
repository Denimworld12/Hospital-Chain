"""Queue DB writes onto a background thread so training never waits on Postgres."""

from __future__ import annotations

import logging
import queue
import threading

from .db import Database

log = logging.getLogger("medchain.db")

WRITE_METHODS = {
    "ensure_schema", "register_node", "heartbeat", "create_run", "round_started", "round_finished",
    "update_sent", "record_metrics", "audit", "membership",
}


class DbWriter:
    def __init__(self, db: Database, maxsize: int = 10_000):
        self.db = db
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread = threading.Thread(target=self._run, name="db-writer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        self.db.wait_until_ready(timeout_s=float("inf"))
        while True:
            item = self._q.get()
            if item is None:
                return
            name, kwargs = item
            try:
                getattr(self.db, name)(**kwargs)
            except Exception:  # noqa: BLE001
                log.exception("db %s failed", name)

    def stop(self) -> None:
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

    def __getattr__(self, name):
        if name not in WRITE_METHODS:
            raise AttributeError(name)

        def enqueue(*args, **kwargs):
            if args:
                import inspect

                params = list(inspect.signature(getattr(self.db, name)).parameters)
                kwargs = {**dict(zip(params, args)), **kwargs}
            try:
                self._q.put_nowait((name, kwargs))
            except queue.Full:
                log.warning("db queue full; dropping %s", name)

        return enqueue
