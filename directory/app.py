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
    POST /enroll     a new hospital joins the network with a one-time join code instead of a
                     kit: it gets its certificate, pins and node id (directory/enroll.py).
    /admin/join-codes  create, list and revoke join codes; for the dashboard's admin page, with
                     the hub's admin token (`admin_token_file`, made on first start).

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

Who may register: the kit hospitals in `hospitals_file` (scripts/provision.py) and the
hospitals that enrolled with a join code (its store). An enrolled hospital must register
with the keys it enrolled with. With DATABASE_URL set, every enrollment attempt and join-code
change also goes to the dashboard's audit log, and each hospital's keys on record go to
Postgres (`hospital_keys`), where the observer checks per-hospital tokens and signatures.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from node import pinning, security
from node.db import Database, NullDatabase
from node.discovery import push_membership
from node.membership import Member, Membership
from node.writer import DbWriter

from .enroll import Enroller, EnrollmentConfig, EnrollmentError
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
    admin_token_file: str | None = None  # the dashboard's admin API token (created on first start)
    enrollment: EnrollmentConfig | None = None  # join codes (directory/enroll.py); needs `tls:` and the CA key


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
                           tls_cert=tls.get("cert"), tls_key=tls.get("key"), ca=tls.get("ca"),
                           admin_token_file=raw.get("admin_token_file"),
                           enrollment=EnrollmentConfig.from_yaml(raw.get("enrollment")))


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


class NewCode(BaseModel):
    created_by: str | None = None
    ttl_minutes: int | None = None
    data_slice: str | None = None


class Revoke(BaseModel):
    revoked_by: str | None = None


MAX_ENROLL_BYTES = 64 * 1024


def admin_token(path: str) -> str:
    """The hub's admin token: made on first start, read by the dashboard from the same file."""
    p = Path(path)
    if not p.is_file():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_urlsafe(32) + "\n")
        p.chmod(0o600)
    return p.read_text().strip()


def create_app(cfg: DirectoryConfig | None = None, database: Database | None = None) -> FastAPI:
    cfg = cfg or load_config()
    key = security.load_signing_key(cfg.signing_key)
    store = Store(cfg.db_path)
    lock = asyncio.Lock()
    state: dict = {}
    dsn = os.environ.get("DATABASE_URL")
    db = DbWriter(database or (Database(dsn) if dsn else NullDatabase()))  # queued: never waited for
    admin = admin_token(cfg.admin_token_file) if cfg.admin_token_file else None

    def configured() -> list[dict]:
        """The kit hospitals, re-read on every request, so the operator can add one without a restart."""
        return list(yaml.safe_load(Path(cfg.hospitals_file).read_text()) or [])

    def hospitals() -> list[dict]:
        """Everyone who may register, in placement order: the kit hospitals, then the enrolled ones."""
        return configured() + [{"id": r["node_id"], "credential_sha256": r["credential_sha256"], "enrolled": r}
                               for r in store.enrolled()]

    def member_names() -> dict[str, str]:
        """{id: display name} of every hospital in the directory's documents (the current and earlier chains)."""
        return {m.id: m.display_name or m.id for doc in reversed(store.recent(1000)) for m in doc.members.values()}

    enroller = (Enroller(cfg.enrollment, store, signing_key=key, directory_cert=cfg.tls_cert,
                         configured_ids=lambda: [str(h["id"]) for h in configured()], member_names=member_names)
                if cfg.enrollment and cfg.tls_cert else None)

    def audit(event: str, node_id: str | None, detail: dict, sender: str | None = None) -> None:
        """The directory's own log, and the dashboard's audit log."""
        store.log(event, node_id, detail)
        db.audit(node_id="directory", event=event, sender=sender or node_id, detail=detail)

    def require_admin(request: Request) -> None:
        auth = request.headers.get("Authorization", "")
        given = auth[7:] if auth.startswith("Bearer ") else ""
        if admin is None:
            raise HTTPException(503, detail="this directory has no admin API (set admin_token_file)")
        if not given or not hmac.compare_digest(given.encode(), admin.encode()):
            raise HTTPException(401, detail="the hub's admin token is required")

    def require_enrollment() -> Enroller:
        if enroller is None:
            raise HTTPException(503, detail="this hub does not take join codes (its directory config has no "
                                            "`enrollment:` block, or no `tls:`)")
        return enroller

    def authenticate(request: Request, node_id: str) -> tuple[int, dict | None]:
        """The caller must hold `node_id`'s own credential. Returns its placement rank (and its enrollment)."""
        auth = request.headers.get("Authorization", "")
        given = auth[7:] if auth.startswith("Bearer ") else ""
        for rank, h in enumerate(hospitals()):
            if str(h["id"]) == node_id:
                if given and hmac.compare_digest(credential_hash(given), str(h["credential_sha256"])):
                    return rank, h.get("enrolled")
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
        db.start()
        db.ensure_schema()
        log.info("directory up on :%d (%s), %d hospitals known%s", cfg.port, "https" if cfg.tls_cert else "http",
                 len(hospitals()), ", taking join codes" if enroller else "")
        try:
            yield
        finally:
            await state["client"].aclose()
            db.stop()

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
        rank, enrolled = authenticate(request, req.node_id)
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
        if enrolled and (req.public_key, req.tls_spki_sha256) != (enrolled["signing_public_key"], enrolled["tls_pin"]):
            store.log("auth_failed", req.node_id, {"reason": "keys differ from the ones it enrolled with"})
            raise HTTPException(403, detail=f"{req.node_id} must register with the keys it enrolled with; to change "
                                            "them, enroll again with a new join code")
        # an enrolled hospital goes by the name it enrolled with, whatever its node says
        name = enrolled["display_name"] if enrolled else req.display_name
        me = Member(req.node_id, req.url.rstrip("/"), req.public_key, name, req.tls_spki_sha256)
        db.hospital_key(node_id=req.node_id, display_name=name or req.node_id,
                        signing_public_key=req.public_key, tls_pin=req.tls_spki_sha256)
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

    # -- join codes -----------------------------------------------------------------------------
    @app.post("/enroll")
    async def enroll(request: Request):
        """A new hospital, with a join code (directory/enroll.py). No credential: the code is the credential."""
        e = require_enrollment()
        raw = await request.body()
        req: dict = {}
        try:
            if len(raw) > MAX_ENROLL_BYTES:
                raise EnrollmentError(413, "enrollment request too large")
            try:
                req = json.loads(raw)
            except ValueError:
                req = None
            if not isinstance(req, dict):
                req = {}
                raise EnrollmentError(400, "the enrollment request is not a JSON object")
            answer, record = e.enroll(req)
        except EnrollmentError as exc:
            audit("enroll_rejected", None, {"code_id": str(req.get("code_id", ""))[:8],
                                            "display_name": " ".join(str(req.get("display_name", "")).split())[:80],
                                            "reason": exc.detail, "client": request.client.host if request.client else None})
            raise HTTPException(exc.status, detail=exc.detail)
        audit("enrolled", record["node_id"], {"code_id": record["code_id"], "display_name": record["display_name"],
                                              "data_slice": record["data_slice"], "tls_pin": record["tls_pin"],
                                              "client": request.client.host if request.client else None})
        db.hospital_key(node_id=record["node_id"], display_name=record["display_name"],
                        signing_public_key=record["signing_public_key"], tls_pin=record["tls_pin"],
                        observer_token_sha256=record["observer_token_sha256"], enrolled=True)
        log.info("enrolled %s as %s (code %s)", record["display_name"], record["node_id"], record["code_id"])
        return answer

    @app.post("/admin/join-codes")
    async def create_code(req: NewCode, request: Request):
        require_admin(request)
        e = require_enrollment()
        try:
            out = e.create_code(req.created_by, req.ttl_minutes, req.data_slice)
        except EnrollmentError as exc:
            raise HTTPException(exc.status, detail=exc.detail)
        audit("join_code_created", None, {"code_id": out["code_id"], "expires_at": out["expires_at"],
                                          "data_slice": out["data_slice"], "by": req.created_by}, sender=req.created_by)
        return out

    @app.get("/admin/join-codes")
    async def list_codes(request: Request):
        require_admin(request)
        e = require_enrollment()
        return {"codes": [{**c, "state": e.state(c)} for c in store.codes()], "slices": e.slices(),
                "ttl_minutes": e.cfg.code_ttl_minutes,
                "hospitals": [{k: r[k] for k in ("node_id", "display_name", "data_slice", "code_id", "enrolled_at")}
                              for r in store.enrolled()]}

    @app.post("/admin/join-codes/{code_id}/revoke")
    async def revoke_code(code_id: str, req: Revoke, request: Request):
        require_admin(request)
        require_enrollment()
        if not store.revoke_code(code_id, req.revoked_by):
            raise HTTPException(409, detail=f"join code {code_id} is not active (unknown, used or already revoked)")
        audit("join_code_revoked", None, {"code_id": code_id, "by": req.revoked_by}, sender=req.revoked_by)
        return {"code_id": code_id, "state": "revoked"}

    return app
