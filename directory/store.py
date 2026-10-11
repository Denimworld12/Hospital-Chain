"""The directory's own datastore: one SQLite file, nothing shared with the Postgres the dashboard reads.

It holds every membership document the directory has issued (the latest one is the
current chain), a log of registrations, joins and leaves, the join codes the organiser
created (only a hash of each), and the hospitals that enrolled with one (directory/enroll.py).
No model state, no metrics, no patient data: the directory never receives any of it.
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
CREATE TABLE IF NOT EXISTS join_codes (
    code_id     TEXT PRIMARY KEY,  -- the code's first group, how it is looked up
    code_key    TEXT NOT NULL,     -- SHA-256 of the whole code (node/enroll.py code_key); never the code
    created_at  REAL NOT NULL,
    created_by  TEXT,
    expires_at  REAL NOT NULL,
    data_slice  TEXT,              -- the data slice it hands out (NULL: the least used one)
    used_at     REAL,
    used_by     TEXT,
    revoked_at  REAL,
    revoked_by  TEXT,
    failures    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS enrolled (
    node_id               TEXT PRIMARY KEY,
    display_name          TEXT NOT NULL,
    name_key              TEXT NOT NULL UNIQUE,  -- two hospitals never share a display name (directory/enroll.py)
    credential_sha256     TEXT NOT NULL,
    signing_public_key    TEXT NOT NULL,
    tls_pin               TEXT NOT NULL,
    observer_token_sha256 TEXT NOT NULL,
    data_slice            TEXT NOT NULL,
    code_id               TEXT NOT NULL,
    enrolled_at           REAL NOT NULL
);
"""

CODE_COLUMNS = ("code_id", "created_at", "created_by", "expires_at", "data_slice", "used_at", "used_by", "revoked_at",
                "revoked_by", "failures")
ENROLLED_COLUMNS = ("node_id", "display_name", "name_key", "credential_sha256", "signing_public_key", "tls_pin",
                    "observer_token_sha256", "data_slice", "code_id", "enrolled_at")
MAX_FAILURES = 5  # wrong proofs for one code before it is revoked


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

    # -- join codes ---------------------------------------------------------------------------
    def add_code(self, code_id: str, code_key: str, created_by: str | None, expires_at: float,
                 data_slice: str | None) -> bool:
        """False if a code with this id already exists."""
        with self._lock:
            try:
                self._conn.execute("INSERT INTO join_codes (code_id, code_key, created_at, created_by, expires_at, "
                                   "data_slice) VALUES (?, ?, ?, ?, ?, ?)",
                                   (code_id, code_key, time.time(), created_by, expires_at, data_slice))
                return True
            except sqlite3.IntegrityError:
                return False

    def code(self, code_id: str) -> tuple[dict, str] | None:
        """(the code's row without its key, its key), or None."""
        with self._lock:
            row = self._conn.execute(f"SELECT {', '.join(CODE_COLUMNS)}, code_key FROM join_codes WHERE code_id = ?",
                                     (code_id,)).fetchone()
        return (dict(zip(CODE_COLUMNS, row[:-1])), row[-1]) if row else None

    def codes(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {', '.join(CODE_COLUMNS)} FROM join_codes ORDER BY created_at DESC "
                                      "LIMIT ?", (limit,)).fetchall()
        return [dict(zip(CODE_COLUMNS, r)) for r in rows]

    def consume_code(self, code_id: str, node_id: str) -> bool:
        """Mark the code used by `node_id`, only if it is still usable (one winner if two race)."""
        with self._lock:
            cur = self._conn.execute("UPDATE join_codes SET used_at = ?, used_by = ? WHERE code_id = ? AND used_at IS "
                                     "NULL AND revoked_at IS NULL AND expires_at > ?",
                                     (time.time(), node_id, code_id, time.time()))
            return cur.rowcount == 1

    def revoke_code(self, code_id: str, by: str | None) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE join_codes SET revoked_at = ?, revoked_by = ? WHERE code_id = ? AND "
                                     "used_at IS NULL AND revoked_at IS NULL", (time.time(), by, code_id))
            return cur.rowcount == 1

    def code_failure(self, code_id: str) -> bool:
        """Count a wrong proof for the code; True if that revoked it."""
        with self._lock:
            self._conn.execute("UPDATE join_codes SET failures = failures + 1 WHERE code_id = ?", (code_id,))
            cur = self._conn.execute("UPDATE join_codes SET revoked_at = ?, revoked_by = 'too many wrong attempts' "
                                     "WHERE code_id = ? AND failures >= ? AND revoked_at IS NULL AND used_at IS NULL",
                                     (time.time(), code_id, MAX_FAILURES))
            return cur.rowcount == 1

    # -- hospitals enrolled with a join code ------------------------------------------------------
    def add_enrolled(self, row: dict) -> None:
        with self._lock:
            self._conn.execute(f"INSERT INTO enrolled ({', '.join(ENROLLED_COLUMNS)}) VALUES "
                               f"({', '.join('?' for _ in ENROLLED_COLUMNS)})", [row[c] for c in ENROLLED_COLUMNS])

    def remove_enrolled(self, node_id: str) -> bool:
        """Forget an enrolled hospital: its credential stops working and its display name is free again."""
        with self._lock:
            return self._conn.execute("DELETE FROM enrolled WHERE node_id = ?", (node_id,)).rowcount == 1

    def enrolled(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {', '.join(ENROLLED_COLUMNS)} FROM enrolled ORDER BY enrolled_at").fetchall()
        return [dict(zip(ENROLLED_COLUMNS, r)) for r in rows]
