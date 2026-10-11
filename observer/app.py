"""MedChain observer: the one place telemetry lands. Nodes push to it; it never calls them.

    POST /ingest   a batch of a node's writes, {"node_id", "items": [{"kind", "payload"}, ...]}, with
                   `Authorization: Bearer <token>`: the network's OBSERVER_TOKEN (kit hospitals), or
                   the token a hospital got when it enrolled with a join code, which only ever
                   writes as that hospital (directory/enroll.py). Each item is one Database write
                   (node/db.py: heartbeat, audit, record_metrics, ...), applied with the same
                   methods, so the Postgres schema and the dashboard are unchanged.
    POST /proof    a node's signed proof-of-origin message (node/proof.py): accepted only if its
                   Ed25519 signature verifies against the key the directory has on record for that
                   hospital (hospital_keys); anything else is refused and written to the audit log
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

from node import proof, security, wire
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
PROOF_MAX_BYTES = 16 * 1024
PROOF_MAX_SKEW_S = 600  # a message older (or newer) than this is refused, whatever the laptops' clocks say


# the field that says whose write an item is: the node that sent the update for update_sent, else node_id
# (an audit row names its writer as node_id; its sender may be a neighbour, as on a received /update)
IDENTITY = {"update_sent": "sender"}


def foreign(item: Item, owner: str) -> bool:
    """True if this item would write as a hospital other than `owner` (a kind with no such field is not)."""
    field = IDENTITY.get(item.kind, "node_id")
    return field in item.payload and item.payload[field] != owner


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

    async def authorize(request: Request, node_id: str) -> str | None:
        """None for the network token; else the hospital whose own token this is (it must be `node_id`)."""
        auth = request.headers.get("Authorization", "")
        given = auth[7:].encode() if auth.startswith("Bearer ") else b""
        if given and hmac.compare_digest(given, token):
            return None  # the network token (kit hospitals, the single-machine stack)
        owner = await asyncio.to_thread(token_owner, hashlib.sha256(given).hexdigest()) if given else None
        if owner is None:
            raise HTTPException(401, detail="a valid observer token is required")
        if owner != node_id:
            raise HTTPException(403, detail=f"this token belongs to {owner}: it writes only as {owner}")
        return owner

    last_seen: dict[str, tuple[str, int]] = {}  # node -> (boot id, seq) of its last accepted proof

    def check_proof(sender: str, body: bytes, headers) -> tuple[dict, str]:
        """The verified message and the fingerprint of the key that signed it (raises ValueError with why not)."""
        rows = db.fetch("SELECT signing_public_key FROM hospital_keys WHERE node_id = %s", (sender,))
        if not rows or not rows[0][0]:
            raise ValueError(f"no signing key is on record for {sender} (it has not registered with the directory)")
        try:
            msg = proof.verify(rows[0][0], body, headers)
        except security.SignatureError as exc:
            raise ValueError(f"signature: {exc} with {sender}'s key on record") from exc
        if abs(time.time() - float(msg.get("sent_at", 0))) > PROOF_MAX_SKEW_S:
            raise ValueError("the message is more than 10 minutes old (or from the future)")
        boot, seq = str(msg.get("boot_id")), int(msg.get("seq", 0))
        prev = last_seen.get(sender)
        if prev and prev[0] == boot and seq <= prev[1]:
            raise ValueError(f"replayed message (sequence {seq}, already had {prev[1]})")
        last_seen[sender] = (boot, seq)
        return msg, hashlib.sha256(rows[0][0].encode()).hexdigest()[:16]

    def apply(batch: Batch, owner: str | None) -> tuple[int, list[str]]:
        """Apply each item; refuse (only) the ones of an unknown kind, or that write as another hospital."""
        applied, rejected = 0, []
        for i, item in enumerate(batch.items):
            if item.kind not in KINDS:
                rejected.append(f"{i}: unknown kind {item.kind!r}")
                continue
            if owner is not None and foreign(item, owner):
                rejected.append(f"{i}: {item.kind}: {owner}'s token writes only as {owner}")
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
        owner = await authorize(request, batch.node_id)
        async with write_lock:  # one batch at a time, in arrival order
            applied, rejected = await asyncio.to_thread(apply, batch, owner)
        if rejected:
            log.warning("batch from %s: %d rejected (%s)", batch.node_id, len(rejected), "; ".join(rejected[:3]))
        return {"applied": applied, "rejected": rejected}

    @app.post("/proof")
    async def proof_of_origin(request: Request):
        sender = request.headers.get(wire.H_SENDER, "")
        body = await request.body()
        if not sender or len(body) > PROOF_MAX_BYTES:
            raise HTTPException(400, detail="a proof needs an X-MedChain-Sender header and at most 16 KiB")

        def audit_refusal(reason: str) -> None:
            log.warning("proof from %s rejected: %s", sender, reason)
            db.audit(node_id=sender, event="proof_rejected", sender=sender,
                     detail={"reason": reason, "client": request.client.host if request.client else None})
            if db.fetch("SELECT 1 FROM hospital_keys WHERE node_id = %s", (sender,)):
                db.proof_rejected(node_id=sender, reason=reason)

        try:
            await authorize(request, sender)
            msg, fingerprint = await asyncio.to_thread(check_proof, sender, body, request.headers)
        except HTTPException as exc:
            await asyncio.to_thread(audit_refusal, f"token: {exc.detail}")
            raise
        except (ValueError, TypeError) as exc:
            await asyncio.to_thread(audit_refusal, str(exc))
            raise HTTPException(403, detail=str(exc))
        async with write_lock:
            await asyncio.to_thread(lambda: db.proof_accepted(
                node_id=sender, sent_at=float(msg["sent_at"]), boot_id=str(msg["boot_id"]), seq=int(msg["seq"]),
                message=msg, key_fingerprint=fingerprint))
        return {"verified": True, "node_id": sender, "key_fingerprint": fingerprint}

    return app
