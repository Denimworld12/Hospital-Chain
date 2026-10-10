"""The directory's own datastore: one SQLite file, nothing shared with the Postgres the dashboard reads.

It holds every membership document the directory has issued (the latest one is the
current chain) and a log of registrations, joins and leaves. No model state, no
metrics, no patient data: the directory never receives any of it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from node.membership import Membership

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    epoch      INTEGER PRIMARY KEY,
    document   TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    event    TEXT NOT NULL,
    node_id  TEXT,
    detail   TEXT NOT NULL DEFAULT '{}'
);
"""


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def latest(self) -> Membership | None:
        with self._lock:
            row = self._conn.execute("SELECT document FROM documents ORDER BY epoch DESC LIMIT 1").fetchone()
        return Membership.from_dict(json.loads(row[0])) if row else None

    def recent(self, n: int) -> list[Membership]:
        """The last n documents, newest first."""
        with self._lock:
            rows = self._conn.execute("SELECT document FROM documents ORDER BY epoch DESC LIMIT ?", (n,)).fetchall()
        return [Membership.from_dict(json.loads(r[0])) for r in rows]

    def save(self, doc: Membership) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO documents (epoch, document, created_at) VALUES (?, ?, ?)",
                               (doc.epoch, json.dumps(doc.to_dict()), time.time()))

    def log(self, event: str, node_id: str | None, detail: dict | None = None) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events (ts, event, node_id, detail) VALUES (?, ?, ?, ?)",
                               (time.time(), event, node_id, json.dumps(detail or {})))

    def events(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT ts, event, node_id, detail FROM events ORDER BY id DESC LIMIT ?",
                                      (limit,)).fetchall()
        return [{"ts": ts, "event": e, "node_id": n, "detail": json.loads(d)} for ts, e, n, d in rows]
