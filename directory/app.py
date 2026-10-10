"""MedChain directory service: decides chain membership. Never sees model weights, never runs training.

What it does, and only this:

    POST /register   a hospital proves who it is (its own pre-shared credential) and
                     says where it can be reached, which key signs its updates and which
                     key its TLS certificate carries (its pin, node/pinning.py; it must
                     connect with that very certificate). A hospital already in the
                     chain gets the current document back; a new one is placed in the
                     chain and every member is told.
    POST /leave      planned departure: the hospital is taken out and its two neighbours
                     are told to bridge to each other.
    GET  /membership the current signed membership document.

So it does coordinate membership: it decides where a new hospital goes and tells
the existing hospitals who to connect to. Placement: while no run is in progress,
hospitals sit in the order its `hospitals` list gives; during a run a new hospital
is only ever appended at the end, because inserting one mid-chain would flip the
head/tail role of every hospital after it. Every change is scheduled for a round
boundary a few rounds ahead of the furthest member (`margin_rounds`), learned from
the members' public GET /status, so all members switch on the same round.

It is a single point of failure for joins and leaves, not for training: nothing in
a node's round loop calls it, so if it goes down the hospitals already in the chain
keep training. Its storage (directory/store.py, SQLite) is separate from the
Postgres the dashboard reads. Every document it issues is signed with its Ed25519
key, so nodes can pass documents to each other and nobody can forge one.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from node import pinning, security
from node.discovery import push_membership
from node.membership import Member, Membership

from .store import Store

log = logging.getLogger("medchain.directory")

ACTIVE = ("running", "paused")
MIN_CHAIN = 2


@dataclass(frozen=True)
class DirectoryConfig:
    port: int
    signing_key: str
    hospitals_file: str  # YAML list, in placement order: [{id, credential_sha256}, ...]
    db_path: str
    host: str = "0.0.0.0"
    margin_rounds: int = 3
    tls_cert: str | None = None
    tls_key: str | None = None
    ca: str | None = None  # verifies the nodes' certificates when it calls them


def load_config(path: str | os.PathLike | None = None) -> DirectoryConfig:
    path = Path(path or os.environ.get("DIRECTORY_CONFIG", ""))
    if not path.is_file():
        raise FileNotFoundError(f"directory config not found: {path!s} (set DIRECTORY_CONFIG)")
    raw = yaml.safe_load(path.read_text()) or {}
    missing = [k for k in ("port", "signing_key", "hospitals_file", "db_path") if k not in raw]
    if missing:
        raise ValueError(f"{path}: missing required keys {missing}")
    tls = raw.get("tls") or {}
    return DirectoryConfig(port=int(raw["port"]), signing_key=str(raw["signing_key"]),
                           hospitals_file=str(raw["hospitals_file"]), db_path=str(raw["db_path"]),
                           host=str(raw.get("host", "0.0.0.0")), margin_rounds=int(raw.get("margin_rounds", 3)),
                           tls_cert=tls.get("cert"), tls_key=tls.get("key"), ca=tls.get("ca"))


def credential_hash(credential: str) -> str:
    return hashlib.sha256(credential.encode()).hexdigest()


class RegisterRequest(BaseModel):
    node_id: str
    url: str
    public_key: str | None = None
    display_name: str | None = None
    patients: int | None = None
    tls_spki_sha256: str | None = None  # the pin of the TLS certificate this registration connects with


class LeaveRequest(BaseModel):
    node_id: str


def create_app(cfg: DirectoryConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()
    key = security.load_signing_key(cfg.signing_key)
    store = Store(cfg.db_path)
    lock = asyncio.Lock()
    state: dict = {}

    def hospitals() -> list[dict]:
        """Re-read on every request, so the operator can add a hospital's credential without a restart."""
        return list(yaml.safe_load(Path(cfg.hospitals_file).read_text()) or [])

    def authenticate(request: Request, node_id: str) -> int:
        """The caller must hold `node_id`'s own credential. Returns that hospital's placement rank."""
        auth = request.headers.get("Authorization", "")
        given = auth[7:] if auth.startswith("Bearer ") else ""
        for rank, h in enumerate(hospitals()):
            if str(h["id"]) == node_id:
                if given and hmac.compare_digest(credential_hash(given), str(h["credential_sha256"])):
                    return rank
                store.log("auth_failed", node_id, {"reason": "bad credential"})
                raise HTTPException(401, detail=f"bad credential for {node_id}")
        store.log("auth_failed", node_id, {"reason": "unknown hospital"})
        raise HTTPException(403, detail=f"{node_id} is not a hospital this directory knows")

    def require_any_hospital(request: Request) -> None:
        auth = request.headers.get("Authorization", "")
        given = auth[7:] if auth.startswith("Bearer ") else ""
        if not given or not any(hmac.compare_digest(credential_hash(given), str(h["credential_sha256"]))
                                for h in hospitals()):
            raise HTTPException(401, detail="a hospital credential is required")

    def expected_pins(host: str, port: int) -> list[pinning.Pin]:
        """Whose key may answer at host:port: a member of the current or the previous chain (a leaver)."""
        parties = [(m.id, m.display_name or m.id, m.url, m.tls_pin) for doc in store.recent(2) for m in doc.members.values()]
        return pinning.pins_at(host, port, parties)

    def require_own_key(request: Request, req: RegisterRequest) -> None:
        """A hospital registers a TLS key only by proving it holds it: the connection must present that certificate."""
        if not cfg.tls_cert:
            return
        if not req.tls_spki_sha256:
            raise HTTPException(400, detail="register with tls_spki_sha256, the pin of your TLS certificate's key")
        presented = pinning.peer_spki(request)
        if presented is None:
            store.log("auth_failed", req.node_id, {"reason": "no client certificate"})
            raise HTTPException(401, detail=f"{req.node_id} must connect with its kit's TLS certificate to register")
        if presented != req.tls_spki_sha256:
            store.log("auth_failed", req.node_id, {"reason": "registered key is not the connection's certificate"})
            raise HTTPException(403, detail=f"the TLS key registered for {req.node_id} is not the key of the "
                                            "certificate this registration connected with")

    async def statuses(chain) -> dict[str, dict]:
        """Members' public GET /status (run id, round, stop) - never anything about the model."""
        doc = store.latest()

        async def one(m):
            try:
                return m, (await state["client"].get(f"{doc.members[m].url}/status", timeout=3)).json()
            except (httpx.HTTPError, ValueError, KeyError):
                return m, None

        return {m: s for m, s in await asyncio.gather(*(one(m) for m in chain)) if s}

    async def schedule(chain_before) -> tuple[str | None, int | None]:
        """The run in progress and the round boundary a change should take effect at (None, None if idle)."""
        active = [s for s in (await statuses(chain_before)).values() if s.get("run_state") in ACTIVE]
        if not active:
            return None, None
        run_id = Counter(s["run_id"] for s in active).most_common(1)[0][0]
        at = max(int(s.get("current_round") or 0) for s in active if s["run_id"] == run_id) + cfg.margin_rounds
        latest = store.latest()
        if latest and latest.run_id == run_id and latest.effective_round:
            at = max(at, latest.effective_round + 1)  # one change per boundary, in epoch order
        return run_id, at

    async def issue(chain, members, reason: str, run_id, at) -> tuple[Membership, dict]:
        latest = store.latest()
        doc = Membership(epoch=(latest.epoch if latest else 0) + 1, chain=tuple(chain),
                         members={m: members[m] for m in chain}, effective_round=at, run_id=run_id,
                         reason=reason).signed_by(key)
        store.save(doc)
        # tell every member of the old and new chain (a leaver learns when to stop; a joiner when to start)
        told_ids = list(dict.fromkeys([*(latest.chain if latest else ()), *chain]))
        urls = {**(latest.members if latest else {}), **members}
        results = await asyncio.gather(*(push_membership(state["client"], urls[m].url, doc, "", "directory")
                                         for m in told_ids))
        told = dict(zip(told_ids, results))
        store.log("membership_issued", None, {"epoch": doc.epoch, "chain": list(chain), "reason": reason,
                                              "run_id": run_id, "effective_round": at, "told": told})
        log.info("epoch %d: %s (%s; from round %s of %s) told=%s", doc.epoch, " - ".join(chain), reason, at, run_id, told)
        return doc, told

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        timeout = httpx.Timeout(3.0, connect=1.5)
        state["client"] = (pinning.pinned_client(cfg.ca, cfg.tls_cert, cfg.tls_key, expected_pins, timeout) if cfg.ca
                           else httpx.AsyncClient(timeout=timeout))
        log.info("directory up on :%d (%s), %d hospitals known", cfg.port, "https" if cfg.tls_cert else "http",
                 len(hospitals()))
        try:
            yield
        finally:
            await state["client"].aclose()

    app = FastAPI(title="MedChain directory", lifespan=lifespan)
    app.state.store = store

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "directory"}

    @app.get("/public-key")
    async def public_key():
        return {"public_key": security.public_key_b64(key)}

    @app.get("/membership")
    async def membership(request: Request):
        """The current signed document. Needs any hospital's credential (it lists addresses and keys)."""
        require_any_hospital(request)
        doc = store.latest()
        return {"membership": doc.to_dict() if doc else None}

    @app.post("/register")
    async def register(req: RegisterRequest, request: Request):
        rank = authenticate(request, req.node_id)
        if req.public_key:
            try:
                security.parse_public_key(req.public_key)
            except ValueError as exc:
                raise HTTPException(400, detail=str(exc))
        try:
            pinning.check_pin(req.tls_spki_sha256)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        require_own_key(request, req)
        if not req.url.startswith(("http://", "https://")):
            raise HTTPException(400, detail="url must be http(s)://")
        me = Member(req.node_id, req.url.rstrip("/"), req.public_key, req.display_name, req.tls_spki_sha256)
        async with lock:
            latest = store.latest()
            chain = list(latest.chain) if latest else []
            members = dict(latest.members) if latest else {}
            if req.node_id in chain:
                if members[req.node_id] == me:  # a restart: same address, same key
                    store.log("registered", req.node_id, {"epoch": latest.epoch, "change": "none"})
                    return {"membership": latest.to_dict(), "joined": False}
                rekeyed = members[req.node_id].tls_pin != me.tls_pin
                members[req.node_id] = me  # only its address or key changed: no round boundary needed
                doc, told = await issue(chain, members, f"{req.node_id} re-registered at {me.url}"
                                        + (" with a new TLS key" if rekeyed else ""), None, None)
                return {"membership": doc.to_dict(), "joined": False, "told": told}
            run_id, at = await schedule(chain)
            members[req.node_id] = me
            if run_id is None:  # nothing in flight: keep the configured order
                order = {str(h["id"]): i for i, h in enumerate(hospitals())}
                chain = sorted(chain + [req.node_id], key=lambda m: order.get(m, len(order)))
                where = f"position {chain.index(req.node_id) + 1} (configured order; no run in progress)"
            else:  # mid-run: append only, so nobody else's head/tail role changes
                chain = chain + [req.node_id]
                where = f"appended at position {len(chain)} (run {run_id} in progress)"
            doc, told = await issue(chain, members, f"{req.node_id} joined: {where}", run_id, at)
            store.log("joined", req.node_id, {"epoch": doc.epoch, "rank": rank, "where": where, "effective_round": at})
            return {"membership": doc.to_dict(), "joined": True, "told": told}

    @app.post("/leave")
    async def leave(req: LeaveRequest, request: Request):
        authenticate(request, req.node_id)
        async with lock:
            latest = store.latest()
            if latest is None or req.node_id not in latest.chain:
                raise HTTPException(409, detail=f"{req.node_id} is not in the chain")
            chain = [m for m in latest.chain if m != req.node_id]
            if len(chain) < MIN_CHAIN:
                raise HTTPException(409, detail=f"a chain needs at least {MIN_CHAIN} hospitals")
            i = latest.chain.index(req.node_id)
            bridge = [latest.chain[j] for j in (i - 1, i + 1) if 0 <= j < len(latest.chain)]
            run_id, at = await schedule(latest.chain)
            members = {m: v for m, v in latest.members.items() if m != req.node_id}
            how = f"{' and '.join(bridge)} bridge to each other" if len(bridge) == 2 else f"{bridge[0]} becomes the chain end"
            doc, told = await issue(chain, members, f"{req.node_id} left: {how}", run_id, at)
            store.log("left", req.node_id, {"epoch": doc.epoch, "bridge": bridge, "effective_round": at})
            return {"membership": doc.to_dict(), "told": told}

    @app.get("/events")
    async def events(request: Request, limit: int = 100):
        require_any_hospital(request)
        return {"events": store.events(limit)}

    return app
