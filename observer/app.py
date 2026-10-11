"""MedChain observer: the one place telemetry lands. Nodes push to it; it never calls them.

    POST /ingest   a batch of a node's writes, {"node_id", "items": [{"kind", "payload"}, ...]}, with
                   `Authorization: Bearer <token>`: the network's OBSERVER_TOKEN (kit hospitals), or
                   the token a hospital got when it enrolled with a join code, which only ever
                   writes as that hospital (directory/enroll.py). Each item is one Database write
                   (node/db.py: heartbeat, audit, record_metrics, ...), applied with the same
                   methods, so the Postgres schema and the dashboard are unchanged.
    GET  /health   liveness

Nodes on other machines need only this service's URL and token, never Postgres
credentials, and Postgres itself can stay on the hub machine's loopback. Training never
waits for it: nodes queue their writes and send them from a background thread
(node/writer.py). Heartbeat freshness still uses Postgres `now()` here, so laptop
clock skew does not mark a node down.

Settings (environment): OBSERVER_PORT (8200), BIND_HOST, DATABASE_URL,
OBSERVER_TOKEN or OBSERVER_TOKEN_FILE, and TLS_CERT / TLS_KEY / TLS_CA for HTTPS.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from node.db import Database, NullDatabase
from node.writer import HUB_ONLY, WRITE_METHODS

log = logging.getLogger("medchain.observer")

KINDS = WRITE_METHODS - HUB_ONLY  # the observer owns the schema and the directory the keys: never a node


def _env(*names: str, default=None):
    return Field(default=default, validation_alias=AliasChoices(*names))


class ObserverSettings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, env_ignore_empty=True, extra="ignore")

    observer_port: int = _env("OBSERVER_PORT", default=8200)
    bind_host: str = _env("BIND_HOST", default="0.0.0.0")
    database_url: str | None = _env("DATABASE_URL")
    observer_token: str | None = _env("OBSERVER_TOKEN")
    observer_token_file: str | None = _env("OBSERVER_TOKEN_FILE")
    tls_cert: str | None = _env("TLS_CERT")
    tls_key: str | None = _env("TLS_KEY")
    tls_ca: str | None = _env("TLS_CA")

    @property
    def token(self) -> str:
        token = self.observer_token or (Path(self.observer_token_file).read_text().strip()
                                        if self.observer_token_file else "")
        if not token:
            raise ValueError("the observer needs OBSERVER_TOKEN or OBSERVER_TOKEN_FILE")
        return token


class Item(BaseModel):
    kind: str
    payload: dict


class Batch(BaseModel):
    node_id: str
    items: list[Item] = Field(max_length=1000)


OWNER_CACHE_S = 30.0


def own_writes(items: list[Item], node_id: str) -> bool:
    """A hospital's own token writes only as that hospital: no item may name another node (as node_id or sender)."""
    return all(item.payload.get(k, node_id) == node_id for item in items for k in ("node_id", "sender"))


def create_app(settings: ObserverSettings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or ObserverSettings()
    token = settings.token.encode()
    db = database or (Database(settings.database_url) if settings.database_url else NullDatabase())
    write_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if await asyncio.to_thread(db.wait_until_ready, 60):
            await asyncio.to_thread(db.ensure_schema)
        log.info("observer up on :%d (%s)", settings.observer_port, "https" if settings.tls_cert else "http")
        yield

    app = FastAPI(title="MedChain observer", lifespan=lifespan)

    owners: dict[str, tuple[float, str | None]] = {}  # token hash -> (when looked up, its hospital)

    def token_owner(digest: str) -> str | None:
        """The enrolled hospital this token hash belongs to (hospital_keys, written by the directory)."""
        hit = owners.get(digest)
        if hit and time.monotonic() - hit[0] < OWNER_CACHE_S:
            return hit[1]
        rows = db.fetch("SELECT node_id FROM hospital_keys WHERE observer_token_sha256 = %s", (digest,))
        if len(owners) > 1024:  # bounded: unknown tokens are remembered too
            owners.clear()
        owners[digest] = (time.monotonic(), rows[0][0] if rows else None)
        return owners[digest][1]

    async def authorize(request: Request, batch: Batch) -> None:
        auth = request.headers.get("Authorization", "")
        given = auth[7:].encode() if auth.startswith("Bearer ") else b""
        if given and hmac.compare_digest(given, token):
            return  # the network token (kit hospitals, the single-machine stack)
        owner = await asyncio.to_thread(token_owner, hashlib.sha256(given).hexdigest()) if given else None
        if owner is None:
            raise HTTPException(401, detail="a valid observer token is required")
        if owner != batch.node_id or not own_writes(batch.items, owner):
            raise HTTPException(403, detail=f"this token belongs to {owner}: it writes only as {owner}")

    def apply(batch: Batch) -> tuple[int, list[str]]:
        applied, rejected = 0, []
        for i, item in enumerate(batch.items):
            if item.kind not in KINDS:
                rejected.append(f"{i}: unknown kind {item.kind!r}")
                continue
            try:
                getattr(db, item.kind)(**item.payload)
                applied += 1
            except TypeError as exc:  # wrong or missing fields for that write
                rejected.append(f"{i}: {item.kind}: {exc}")
        return applied, rejected

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "observer"}

    @app.post("/ingest")
    async def ingest(batch: Batch, request: Request):
        await authorize(request, batch)
        async with write_lock:  # one batch at a time, in arrival order
            applied, rejected = await asyncio.to_thread(apply, batch)
        if rejected:
            log.warning("batch from %s: %d rejected (%s)", batch.node_id, len(rejected), "; ".join(rejected[:3]))
        return {"applied": applied, "rejected": rejected}

    return app
