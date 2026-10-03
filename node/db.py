"""Postgres writes: nodes, runs, rounds, updates_meta, metrics, audit_logs.

Schema additions since the MVP are all nullable columns added in place, so an
existing database upgrades on start: metrics.residuals (per-edge primal residuals,
written by the ADMM engines; NULL for the gossip baseline), metrics.epoch and
metrics.chain_size (the membership epoch and chain length of that round), and
nodes.epoch / chain / in_chain / membership_round (each node's current view of the
chain, so the dashboard can draw it; the directory's own store is separate).

Nodes are the only writers. The dashboard reads these tables and never writes
them. Writes are best-effort: a database outage is logged and training carries
on, because the database is an observation channel, not a coordinator.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

log = logging.getLogger("medchain.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id        TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    url            TEXT NOT NULL,
    patients       INTEGER NOT NULL,
    neighbours     JSONB NOT NULL DEFAULT '[]',
    engine         TEXT NOT NULL,
    registered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat TIMESTAMPTZ,
    run_id         TEXT,
    run_state      TEXT NOT NULL DEFAULT 'idle',
    current_round  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    total_rounds  INTEGER NOT NULL,
    engine        TEXT NOT NULL,
    initiated_by  TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rounds (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              TEXT NOT NULL,
    node_id             TEXT NOT NULL,
    round               INTEGER NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    neighbours_expected INTEGER,
    neighbours_heard    INTEGER,
    UNIQUE (run_id, node_id, round)
);
CREATE TABLE IF NOT EXISTS updates_meta (
    id                   BIGSERIAL PRIMARY KEY,
    run_id               TEXT NOT NULL,
    round                INTEGER NOT NULL,
    sender               TEXT NOT NULL,
    receiver             TEXT NOT NULL,
    payload_bytes        INTEGER NOT NULL,
    full_precision_bytes INTEGER NOT NULL,
    bits                 INTEGER NOT NULL,
    dtype                TEXT NOT NULL,
    checksum             TEXT NOT NULL,
    status               TEXT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS updates_meta_run_round ON updates_meta (run_id, round);
CREATE TABLE IF NOT EXISTS metrics (
    id         BIGSERIAL PRIMARY KEY,
    run_id     TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    round      INTEGER NOT NULL,
    loss       DOUBLE PRECISION NOT NULL,
    accuracy   DOUBLE PRECISION NOT NULL,
    train_loss DOUBLE PRECISION NOT NULL,
    n_train    INTEGER NOT NULL,
    n_test     INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, node_id, round)
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    node_id       TEXT NOT NULL,
    event         TEXT NOT NULL,
    run_id        TEXT,
    round         INTEGER,
    sender        TEXT,
    receiver      TEXT,
    payload_bytes INTEGER,
    checksum      TEXT,
    detail        JSONB NOT NULL DEFAULT '{}'
);
-- ADMM engines: per-edge primal residuals for the round (NULL for the gossip baseline)
ALTER TABLE metrics ADD COLUMN IF NOT EXISTS residuals JSONB;
-- dynamic membership: the epoch and chain length each round ran under, and each node's view of the chain
ALTER TABLE metrics ADD COLUMN IF NOT EXISTS epoch INTEGER;
ALTER TABLE metrics ADD COLUMN IF NOT EXISTS chain_size INTEGER;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS epoch INTEGER;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS chain JSONB;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS in_chain BOOLEAN;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS membership_round INTEGER;
"""

SCHEMA_LOCK_KEY = 4242_0001


class Database:
    """Single psycopg connection guarded by a lock; call from worker threads."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn = None
        self._lock = threading.Lock()

    # -- connection ---------------------------------------------------------
    def _connect(self):
        import psycopg

        conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=3)
        return conn

    def _execute(self, sql: str, params: tuple | dict | None = None) -> None:
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._conn = self._connect()
                    with self._conn.cursor() as cur:
                        cur.execute(sql, params)
                    return
                except Exception as exc:  # noqa: BLE001 - best-effort observation channel
                    try:
                        if self._conn is not None:
                            self._conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._conn = None
                    if attempt == 2:
                        log.warning("db write failed: %s", exc)

    def wait_until_ready(self, timeout_s: float = 60) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                with self._lock:
                    if self._conn is None or self._conn.closed:
                        self._conn = self._connect()
                return True
            except Exception as exc:  # noqa: BLE001
                log.info("waiting for postgres: %s", exc)
                time.sleep(1)
        return False

    def ensure_schema(self) -> None:
        # Advisory lock: all nodes start together and race on CREATE TABLE.
        self._execute(f"SELECT pg_advisory_lock({SCHEMA_LOCK_KEY}); {SCHEMA} SELECT pg_advisory_unlock({SCHEMA_LOCK_KEY});")

    # -- writes ---------------------------------------------------------------
    def register_node(self, *, node_id: str, display_name: str, url: str, patients: int, neighbours: list[str], engine: str) -> None:
        self._execute(
            """
            INSERT INTO nodes (node_id, display_name, url, patients, neighbours, engine, last_heartbeat, run_state, current_round)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, now(), 'idle', 0)
            ON CONFLICT (node_id) DO UPDATE SET
                display_name = EXCLUDED.display_name, url = EXCLUDED.url, patients = EXCLUDED.patients,
                neighbours = EXCLUDED.neighbours, engine = EXCLUDED.engine, last_heartbeat = now(),
                run_id = NULL, run_state = 'idle', current_round = 0
            """,
            (node_id, display_name, url, patients, json.dumps(neighbours), engine),
        )

    def membership(self, *, node_id: str, epoch: int, chain: list[str], neighbours: list[str], in_chain: bool,
                   membership_round: int | None) -> None:
        self._execute(
            """UPDATE nodes SET epoch = %s, chain = %s::jsonb, neighbours = %s::jsonb, in_chain = %s,
                      membership_round = %s WHERE node_id = %s""",
            (epoch, json.dumps(chain), json.dumps(neighbours), in_chain, membership_round, node_id),
        )

    def heartbeat(self, node_id: str, run_id: str | None, run_state: str, current_round: int) -> None:
        self._execute(
            "UPDATE nodes SET last_heartbeat = now(), run_id = %s, run_state = %s, current_round = %s WHERE node_id = %s",
            (run_id, run_state, current_round, node_id),
        )

    def create_run(self, run_id: str, total_rounds: int, engine: str, initiated_by: str) -> None:
        self._execute(
            "INSERT INTO runs (run_id, total_rounds, engine, initiated_by) VALUES (%s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING",
            (run_id, total_rounds, engine, initiated_by),
        )

    def round_started(self, run_id: str, node_id: str, round_num: int, expected: int) -> None:
        self._execute(
            """INSERT INTO rounds (run_id, node_id, round, neighbours_expected) VALUES (%s, %s, %s, %s)
               ON CONFLICT (run_id, node_id, round) DO NOTHING""",
            (run_id, node_id, round_num, expected),
        )

    def round_finished(self, run_id: str, node_id: str, round_num: int, heard: int) -> None:
        self._execute(
            "UPDATE rounds SET finished_at = now(), neighbours_heard = %s WHERE run_id = %s AND node_id = %s AND round = %s",
            (heard, run_id, node_id, round_num),
        )

    def update_sent(self, *, run_id: str, round_num: int, sender: str, receiver: str, payload_bytes: int,
                    full_precision_bytes: int, bits: int, dtype: str, checksum: str, status: str) -> None:
        self._execute(
            """INSERT INTO updates_meta (run_id, round, sender, receiver, payload_bytes, full_precision_bytes, bits, dtype, checksum, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (run_id, round_num, sender, receiver, payload_bytes, full_precision_bytes, bits, dtype, checksum, status),
        )

    def record_metrics(self, *, run_id: str, node_id: str, round_num: int, loss: float, accuracy: float,
                       train_loss: float, n_train: int, n_test: int, residuals: dict | None = None,
                       epoch: int | None = None, chain_size: int | None = None) -> None:
        self._execute(
            """INSERT INTO metrics (run_id, node_id, round, loss, accuracy, train_loss, n_train, n_test, residuals,
                                    epoch, chain_size)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) ON CONFLICT (run_id, node_id, round) DO NOTHING""",
            (run_id, node_id, round_num, loss, accuracy, train_loss, n_train, n_test,
             None if residuals is None else json.dumps(residuals), epoch, chain_size),
        )

    def audit(self, *, node_id: str, event: str, run_id: str | None = None, round_num: int | None = None,
              sender: str | None = None, receiver: str | None = None, payload_bytes: int | None = None,
              checksum: str | None = None, detail: dict[str, Any] | None = None) -> None:
        self._execute(
            """INSERT INTO audit_logs (node_id, event, run_id, round, sender, receiver, payload_bytes, checksum, detail)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
            (node_id, event, run_id, round_num, sender, receiver, payload_bytes, checksum, json.dumps(detail or {})),
        )


class NullDatabase(Database):
    """Used when no DATABASE_URL is set (unit tests, local experiments)."""

    def __init__(self):
        super().__init__("")

    def _execute(self, sql, params=None):  # noqa: D401
        return None

    def wait_until_ready(self, timeout_s: float = 60) -> bool:
        return True
