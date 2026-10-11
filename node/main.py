"""MedChain hospital node (FastAPI).

Endpoints
    GET  /health          liveness
    GET  /status          identity, neighbours, current run state
    POST /round/start     start (or join) a training run; floods to neighbours
    POST /round/pause     pause at the next round boundary; floods
    POST /round/resume    resume a paused run; floods
    POST /update          receive a neighbour's update (the engine's packet bytes)
    GET  /snapshot        ADMM engines: my full theta_hat for a restarted/drifted/new neighbour
    GET  /metrics         this node's per-round metrics for the current/last run
    GET  /membership      my current (and scheduled) membership documents, as the directory signed them
    POST /membership      take a directory-signed membership document (from the directory or a peer)
    POST /membership/rejoin
                          a restarted neighbour asks to rejoin from its saved chain while the directory
                          is down; accepted only for an unchanged chain epoch and its pinned TLS key
    POST /membership/leave
                          planned departure: ask the directory to take me out of the chain at the next
                          round boundary, and tell my neighbours so they bridge to each other
    POST /assess, /assess/pdf, /assess/ecg, /assess/ecg/pdf
                          per-patient risk assessment (node/assess/routes.py)

Identity comes from the environment and the optional YAML named by $NODE_CONFIG
(node/settings.py). With a `directory:` block the node registers at start (POST
<directory>/register with its own credential) and its chain comes from the directory's
signed documents; training never waits for the directory, so a directory outage only
blocks joins and leaves. Telemetry is pushed to the observer service (node/writer.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import discovery, pinning, proof, rejoin, security, wire
from .assess.routes import Assessor, build_router as build_assess_router
from .config import NodeConfig, load_config
from .db import Database, NullDatabase
from .engine import NeighborUpdate
from .engines import build_engine, build_runner
from .membership import Membership
from .runner import Runner, new_run_id
from .trainer import LocalTrainer
from .writer import DbWriter, ObserverSink

log = logging.getLogger("medchain.node")

PROOF_INTERVAL_S = float(os.environ.get("PROOF_INTERVAL_S", "10"))


class StartRequest(BaseModel):
    run_id: str | None = None
    rounds: int | None = Field(default=None, ge=1, le=500)
    origin: str | None = None
    initiated_by: str | None = None


class RejoinRequest(BaseModel):
    node_id: str
    epoch: int


class ControlRequest(BaseModel):
    op_id: str | None = None
    origin: str | None = None


def observer_sink(cfg: NodeConfig) -> ObserverSink:
    timeout = httpx.Timeout(cfg.http.read_timeout_s, connect=cfg.http.connect_timeout_s)
    if cfg.tls:  # the observer is pinned like the directory: its key comes from the kit
        pins = [("observer", "the observer", cfg.observer.url, cfg.observer.tls_pin)]
        client = pinning.pinned_sync_client(cfg.tls.ca, cfg.tls.cert, cfg.tls.key,
                                            lambda host, port: pinning.pins_at(host, port, pins), timeout)
    else:
        client = httpx.Client(timeout=timeout)
    return ObserverSink(cfg.observer.url, cfg.observer.token, cfg.node_id, client)


def create_app(cfg: NodeConfig | None = None, database: Database | None = None) -> FastAPI:
    cfg = cfg or load_config()
    if database is None:  # telemetry: the observer service; Postgres directly only without one (node/writer.py)
        dsn = os.environ.get("DATABASE_URL")
        database = observer_sink(cfg) if cfg.observer else Database(dsn) if dsn else NullDatabase()
    trainer = LocalTrainer(
        cfg.data_slice,
        test_fraction=cfg.training.test_fraction,
        seed=cfg.training.seed,
        learning_rate=cfg.training.learning_rate,
        local_epochs=cfg.training.local_epochs,
    )
    engine = build_engine(cfg, trainer)
    assessor = Assessor(cfg.assess.tabular_model, cfg.assess.ecg_model)
    db = DbWriter(database, audit_db=Path(cfg.state_dir) / "audit.db")
    started_at = time.time()
    state: dict = {}
    signing_key = security.load_signing_key(cfg.signing_key) if cfg.signing_key else None
    directory_key = security.parse_public_key(cfg.directory.public_key) if cfg.directory else None
    if signing_key is None:
        log.warning("%s has no signing_key: /update packets are neither signed nor verified (development only)",
                    cfg.node_id)

    directory_pin = cfg.directory.tls_pin if cfg.directory and cfg.tls else None

    def expected_pins(host: str, port: int) -> list[pinning.Pin]:
        """Whose key may answer at host:port: the directory (pinned by my kit) or a member of my signed chain."""
        parties = [("directory", "the directory", cfg.directory.url, directory_pin)] if cfg.directory else []
        if cfg.observer and cfg.tls:  # the proof-of-origin messages go to the observer on this client
            parties.append(("observer", "the observer", cfg.observer.url, cfg.observer.tls_pin))
        if "runner" in state:
            parties += state["runner"].known_parties()
        return pinning.pins_at(host, port, parties)

    def require_caller(request: Request, claimed: str, member, run_id: str | None = None,
                       round_num: int | None = None) -> None:
        """Mutual pinning: the caller's client certificate must carry the key on record for the hospital it claims."""
        if not cfg.tls:
            return
        pin = pinning.Pin(claimed, (member.display_name or claimed) if member else claimed,
                          member.tls_pin if member else None)
        problem = pinning.caller_problem(pinning.peer_spki(request), pin)
        if problem:
            code, reason = problem
            db.audit(node_id=cfg.node_id, event="tls_pin_mismatch", run_id=run_id, round_num=round_num, sender=claimed,
                     receiver=cfg.node_id, detail={"path": request.url.path, "reason": reason})
            raise HTTPException(code, detail=reason)

    async def heartbeat_loop(runner: Runner) -> None:
        while True:
            db.heartbeat(cfg.node_id, runner.run_id, runner.state, runner.current_round)
            await asyncio.sleep(cfg.timing.heartbeat_interval_s)

    async def proof_loop(runner: Runner, prover: proof.Prover) -> None:
        """Signed facts about this machine, for the dashboard (node/proof.py). Never waited for by training."""
        while True:
            await asyncio.sleep(PROOF_INTERVAL_S)  # the first one after registration has had a moment
            try:
                body, headers = prover.message(await proof.neighbour_rtts(runner.client, runner.neighbours))
                resp = await runner.client.post(f"{cfg.observer.url}/proof", content=body,
                                                headers={**headers, "Authorization": f"Bearer {cfg.observer.token}"})
                if resp.status_code != 200:
                    log.warning("observer refused my proof-of-origin message: %s %s", resp.status_code, resp.text[:200])
            except httpx.HTTPError as exc:
                log.debug("proof-of-origin message not delivered: %s", exc)

    def take_membership(doc: Membership, via: str) -> str:
        """Verify the directory's signature, then apply or schedule the document."""
        if directory_key is None:
            raise HTTPException(409, detail=f"{cfg.node_id} has a static chain (no directory configured)")
        try:
            doc.verify(directory_key)
        except security.SignatureError as exc:
            db.audit(node_id=cfg.node_id, event="membership_rejected", detail={"via": via, "reason": str(exc)})
            raise HTTPException(403, detail=f"membership document not signed by the directory: {exc}")
        outcome = runner().offer_membership(doc)
        if outcome != "stale":
            log.info("membership epoch %d via %s: %s", doc.epoch, via, outcome)
        return outcome

    async def rejoin_from_saved(r: Runner) -> str:
        """The directory is down: offer my saved chain to my saved neighbours (node/rejoin.py)."""
        reason = rejoin.check_self(cfg, r.saved, r.public_key)
        if reason:
            attempt = rejoin.Attempt("unusable", reason=reason)
        else:
            attempt = await rejoin.ask_neighbours(r.client, cfg, r.saved)
        if attempt.outcome != "unreachable":
            db.audit(node_id=cfg.node_id, event=f"rejoin_{attempt.outcome}",
                     detail={"epoch": attempt.epoch, "neighbours": attempt.answers, "reason": attempt.reason})
        if attempt.outcome == "accepted":
            log.warning("directory unreachable; my neighbours accepted my saved chain (epoch %d): rejoining",
                        attempt.epoch)
            docs, r.saved = r.saved, []
            for doc in docs:
                take_membership(doc, "saved membership, accepted by my neighbours")
        elif attempt.outcome in ("refused", "unusable"):
            log.warning("cannot rejoin from my saved chain (%s); waiting for the directory",
                        attempt.reason or attempt.answers)
            r.saved = []
        return attempt.outcome

    async def register_with_directory(r: Runner) -> None:
        """Register (retrying while the directory is unreachable), then join or rejoin the chain's run.

        While the directory is unreachable, a restarted node may rejoin from its saved chain if its
        neighbours vouch that the chain is unchanged; it keeps registering in the background either way.
        A directory that answers but refuses the registration is never bypassed this way."""
        joined, doc, rejoined = False, None, False
        r.saved = rejoin.load(r.state_file, directory_key) if r.state_file and directory_key else []
        delay = 1.0
        while doc is None:
            try:
                doc, joined = await discovery.register(r.client, cfg, r.public_key, trainer.n_patients, r.tls_pin)
                break
            except discovery.DirectoryError as exc:
                if pinning.is_pin_error(exc):
                    db.audit(node_id=cfg.node_id, event="tls_pin_mismatch", receiver="directory",
                             detail={"reason": str(exc.__cause__ or exc)})
                log.warning("directory registration failed (%s); retrying in %.0fs", exc, delay)
                unreachable = exc.unreachable
            if unreachable and r.saved and await rejoin_from_saved(r) == "accepted":
                rejoined = True
                if cfg.is_admm and r.in_chain:
                    await r.rejoin()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)
        r.saved = []
        take_membership(doc, "directory registration")
        if rejoined:
            return
        if joined and doc.run_id and doc.effective_round and r.in_chain:
            outcome = await r.join_run(doc)
            log.info("join of run %s: %s", doc.run_id, outcome)
        elif cfg.is_admm and r.in_chain:
            await r.rejoin()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        timeout = httpx.Timeout(cfg.http.read_timeout_s, connect=cfg.http.connect_timeout_s)
        if cfg.tls:  # every TLS connection is pinned to the key on record for whoever we dial (node/pinning.py)
            client = pinning.pinned_client(cfg.tls.ca, cfg.tls.cert, cfg.tls.key, expected_pins, timeout)
        else:
            client = httpx.AsyncClient(timeout=timeout)
        runner = build_runner(cfg, engine, db, client, signing_key=signing_key)
        state["runner"] = app.state.runner = runner
        db.start()
        db.ensure_schema()
        nbs = [n.id for n in runner.neighbours]
        db.register_node(node_id=cfg.node_id, display_name=cfg.display_name, url=cfg.advertise_url,
                         patients=trainer.n_patients, neighbours=nbs, engine=engine.label)
        m = runner.membership
        db.membership(node_id=cfg.node_id, epoch=m.epoch, chain=list(m.chain), neighbours=nbs, in_chain=runner.in_chain,
                      membership_round=None)
        db.audit(node_id=cfg.node_id, event="node_registered",
                 detail={"engine": engine.label, "patients": trainer.n_patients, "neighbours": nbs,
                         "directory": cfg.directory.url if cfg.directory else None, "tls": bool(cfg.tls),
                         "signed_updates": signing_key is not None})
        hb = asyncio.create_task(heartbeat_loop(runner))
        if cfg.observer and signing_key is not None:
            state["proof"] = asyncio.create_task(proof_loop(runner, proof.Prover(
                cfg.node_id, signing_key, trainer.n_patients, cfg.image_version)))
        if cfg.directory:
            state["rejoin"] = asyncio.create_task(register_with_directory(runner))
        elif cfg.is_admm:  # a checkpoint on the volume means we were killed mid-run: rejoin it
            state["rejoin"] = asyncio.create_task(runner.rejoin())
        log.info("%s (%s) up on :%d (%s) with %d patients, neighbours=%s, engine=%s", cfg.display_name, cfg.node_id,
                 cfg.port, cfg.scheme, trainer.n_patients, nbs, engine.label)
        try:
            yield
        finally:
            hb.cancel()
            for t in (state.get("rejoin"), state.get("fetch"), state.get("proof")):
                if t:
                    t.cancel()
            await runner.shutdown()
            await client.aclose()
            db.stop()

    app = FastAPI(title=f"MedChain node {cfg.node_id}", lifespan=lifespan)

    def runner() -> Runner:
        return state["runner"]

    def require_secret(request: Request) -> None:
        given = request.headers.get(wire.H_SECRET, "")
        if not secrets.compare_digest(given.encode(), cfg.shared_secret.encode()):
            raise HTTPException(401, detail="missing or invalid shared secret")

    peer_only = [Depends(require_secret)]
    app.include_router(build_assess_router(cfg.node_id, cfg.display_name, assessor, peer_only))

    async def flood(path: str, payload: dict, exclude: str | None) -> dict[str, str]:
        async def one(n):
            try:
                resp = await runner().client.post(f"{n.url}{path}", json=payload,
                                                  headers={wire.H_SECRET: cfg.shared_secret})
                return n.id, str(resp.json().get("status", resp.status_code))
            except (httpx.HTTPError, ValueError) as exc:
                return n.id, f"unreachable ({type(exc).__name__})"

        results = await asyncio.gather(*(one(n) for n in runner().neighbours if n.id != exclude))
        return dict(results)

    @app.get("/health")
    async def health():
        return {"status": "ok", "node_id": cfg.node_id}

    @app.get("/status")
    async def status():
        r = runner()
        return {
            "node_id": cfg.node_id,
            "display_name": cfg.display_name,
            "engine": engine.label,
            "engine_name": cfg.engine,
            "patients": trainer.n_patients,
            "n_train": len(trainer.y_train),
            "n_test": len(trainer.y_test),
            "bits": getattr(engine, "bits", 64),
            "rho": cfg.rho,
            "chain": list(r.membership.chain),
            "in_chain": r.in_chain,
            "epoch": r.epoch,
            "epoch_start_round": r.epoch_start_round,
            "pending_epochs": [{"epoch": d.epoch, "effective_round": d.effective_round, "chain": list(d.chain)}
                               for d in r._pending],
            "position": r.membership.chain.index(cfg.node_id) + 1 if r.in_chain else None,
            "tls": bool(cfg.tls),
            "signed_updates": signing_key is not None,
            **({"group": engine.pos.group if r.in_chain else None, "d": engine.d, "stop_at": r.stop_at,
                "converged_round": r.converged_round} if cfg.is_admm else {}),
            "neighbours": [{"id": n.id, "url": n.url, "last_send": r.neighbour_status.get(n.id)} for n in r.neighbours],
            "run_id": r.run_id,
            "run_state": r.state,
            "current_round": r.current_round,
            "total_rounds": r.total_rounds,
            "uptime_s": round(time.time() - started_at, 1),
            "assess": assessor.status(),
        }

    @app.get("/metrics")
    async def metrics():
        r = runner()
        return {"node_id": cfg.node_id, "engine": engine.label, "run_id": r.run_id, "run_state": r.state,
                "total_rounds": r.total_rounds, "history": [asdict(m) for m in r.history]}

    @app.post("/round/start", dependencies=peer_only)
    async def round_start(req: StartRequest):
        r = runner()
        run_id = req.run_id or new_run_id()
        rounds = req.rounds or cfg.training.rounds
        outcome = await r.start(run_id, rounds, req.initiated_by or req.origin or cfg.node_id)
        if outcome == "busy":
            raise HTTPException(409, detail=f"{cfg.node_id} is already in run {r.run_id} ({r.state})")
        if outcome == "not-in-chain":
            raise HTTPException(409, detail=f"{cfg.node_id} is not in the chain (epoch {r.epoch})")
        forwarded = {}
        if outcome == "started":
            # tell neighbours first so they are listening before our first /update arrives
            forwarded = await flood("/round/start", {"run_id": run_id, "rounds": rounds, "origin": cfg.node_id,
                                                     "initiated_by": req.initiated_by or cfg.node_id}, req.origin)
            r.launch()
        return {"status": outcome, "run_id": run_id, "rounds": rounds, "engine": engine.label,
                "node_id": cfg.node_id, "forwarded": forwarded}

    async def control(action: str, req: ControlRequest):
        r = runner()
        op_id = req.op_id or f"{action}-{secrets.token_hex(6)}"
        if r.seen_op(op_id):
            return {"status": "seen", "node_id": cfg.node_id}
        changed = r.pause() if action == "pause" else r.resume()
        forwarded = await flood(f"/round/{action}", {"op_id": op_id, "origin": cfg.node_id}, req.origin)
        return {"status": ("paused" if action == "pause" else "running") if changed else r.state,
                "node_id": cfg.node_id, "run_id": r.run_id, "forwarded": forwarded}

    @app.post("/round/pause", dependencies=peer_only)
    async def round_pause(req: ControlRequest | None = None):
        return await control("pause", req or ControlRequest())

    @app.post("/round/resume", dependencies=peer_only)
    async def round_resume(req: ControlRequest | None = None):
        return await control("resume", req or ControlRequest())

    @app.post("/update", dependencies=peer_only)
    async def update(request: Request):
        h = request.headers
        sender = h.get(wire.H_SENDER)
        run_id = h.get(wire.H_RUN)
        try:
            round_num = int(h.get(wire.H_ROUND, ""))
        except ValueError:
            raise HTTPException(400, detail="missing or invalid round header")
        if not sender or not run_id:
            raise HTTPException(400, detail="missing sender or run header")
        r = runner()
        peer = r.peer(sender)
        if peer is None:
            db.audit(node_id=cfg.node_id, event="update_rejected", run_id=run_id, round_num=round_num, sender=sender,
                     receiver=cfg.node_id, detail={"reason": "sender is not a current or scheduled neighbour"})
            raise HTTPException(403, detail=f"{sender} is not a neighbour of {cfg.node_id}")
        require_caller(request, sender, peer, run_id, round_num)
        body = await request.body()
        if signing_key is not None:  # who really sent this? (a checksum alone proves nothing)
            reason = None
            if not peer.public_key:
                reason, code = "unrecognized sender: no public key on record", 403
            elif h.get(wire.H_RECEIVER) != cfg.node_id:
                reason, code = "packet is addressed to another node", 403
            else:
                try:
                    security.verify(security.parse_public_key(peer.public_key), h.get(wire.H_SIGNATURE), body, h, "update")
                except (security.SignatureError, ValueError) as exc:
                    reason, code = f"signature: {exc}", 401
            if reason:
                db.audit(node_id=cfg.node_id, event="update_rejected", run_id=run_id, round_num=round_num, sender=sender,
                         receiver=cfg.node_id, payload_bytes=len(body), detail={"reason": reason})
                raise HTTPException(code, detail=f"update from {sender} rejected: {reason}")
        epoch = h.get(wire.H_EPOCH, "")
        if directory_key is not None and epoch.lstrip("-").isdigit() and int(epoch) > r.known_epoch:
            fetch_newer(peer.url, sender)
        dtype = h.get(wire.H_DTYPE, "float64")
        if dtype != engine.dtype:
            raise HTTPException(400, detail=f"unsupported dtype {dtype}: {cfg.node_id} runs {engine.label}")
        try:
            seq = int(h[wire.H_SEQ]) if cfg.is_admm else None
        except (KeyError, ValueError):
            raise HTTPException(400, detail="missing or invalid sequence header")
        claimed = h.get(wire.H_CHECKSUM, "")
        actual = wire.checksum(body)
        verified = claimed == actual
        if not verified:
            db.audit(node_id=cfg.node_id, event="update_received", run_id=run_id, round_num=round_num, sender=sender,
                     receiver=cfg.node_id, payload_bytes=len(body), checksum=actual,
                     detail={"verified": False, "dtype": dtype, "engine": engine.label})
            raise HTTPException(400, detail="checksum mismatch")
        upd = NeighborUpdate(sender=sender, round=round_num, body=body, checksum=actual, dtype=dtype, seq=seq,
                             base=h.get(wire.H_BASE) or None, produces=h.get(wire.H_PRODUCES) or None)
        answer = await r.receive(run_id, upd, {k: h[k] for k in (wire.H_VIEW, wire.H_STOP) if h.get(k)})
        db.audit(node_id=cfg.node_id, event="update_received", run_id=run_id, round_num=round_num, sender=sender,
                 receiver=cfg.node_id, payload_bytes=len(body), checksum=actual,
                 detail={"verified": True, "signed": signing_key is not None, "dtype": dtype, "engine": engine.label,
                         **({"seq": seq, "on_time": answer["on_time"]} if cfg.is_admm else {})})
        return {"accepted": True, **answer, "node_id": cfg.node_id}

    @app.get("/snapshot", dependencies=peer_only)
    async def snapshot(requester: str, request: Request, rejoin_round: int | None = None):
        """A neighbour's one-off full resync: my theta_hat (plus its mirror and our edge's lambda). Never data."""
        if not cfg.is_admm:
            raise HTTPException(404, detail=f"{engine.label} keeps no theta_hat snapshot")
        r = runner()
        if r.peer(requester) is None:
            raise HTTPException(403, detail=f"{requester} is not a neighbour of {cfg.node_id}")
        require_caller(request, requester, r.peer(requester), r.run_id, r.current_round)
        from .qgadmm.engine import snapshot_body

        snap = await asyncio.to_thread(engine.snapshot_for, requester)
        body = snapshot_body(snap)
        if rejoin_round is not None:
            r.note_rejoin(requester, rejoin_round)
        db.audit(node_id=cfg.node_id, event="snapshot_served", run_id=r.run_id, round_num=r.current_round,
                 sender=cfg.node_id, receiver=requester, payload_bytes=len(body), checksum=wire.checksum(body),
                 detail={"seq": snap["seq"], "rejoin_round": rejoin_round, "engine": engine.label,
                         "contents": "theta_hat, my mirror of the requester, our edge's lambda (model state only)"})
        headers = r.signed_headers(body, {wire.H_SENDER: cfg.node_id, wire.H_RECEIVER: requester,
                                          wire.H_SEQ: str(snap["seq"]), wire.H_MIRROR_SEQ: str(snap["mirror_seq"]),
                                          wire.H_CHECKSUM: wire.checksum(body)}, "snapshot")
        return Response(content=body, media_type="application/octet-stream", headers=headers)

    # -- membership ---------------------------------------------------------------
    def fetch_newer(url: str, sender: str) -> None:
        """A neighbour is on a newer epoch than I know of: fetch its signed documents (once at a time)."""
        if state.get("fetch") and not state["fetch"].done():
            return

        async def go():
            try:
                docs = await discovery.fetch_membership(runner().client, url, cfg.shared_secret, cfg.node_id)
            except discovery.DirectoryError as exc:
                log.warning("could not fetch membership from %s: %s", sender, exc)
                return
            for doc in docs:
                try:
                    take_membership(doc, f"peer {sender}")
                except HTTPException:
                    pass

        state["fetch"] = asyncio.create_task(go())

    @app.get("/membership", dependencies=peer_only)
    async def membership_get(request: Request):
        r = runner()
        if cfg.tls:  # a hospital asking for my documents must be one of my chain's members, with its own key
            sender = request.headers.get(wire.H_SENDER, "")
            if not r.member(sender):
                raise HTTPException(403, detail=f"{sender or 'the caller'} is not a member of {cfg.node_id}'s chain")
            require_caller(request, sender, r.member(sender))
        docs = [d for d in [r.membership, *r._pending] if d is not None and d.signature]
        return {"node_id": cfg.node_id, "epoch": r.epoch, "documents": [d.to_dict() for d in docs]}

    @app.post("/membership")  # no shared secret needed: only a document the directory signed is accepted
    async def membership_post(request: Request):
        try:
            doc = Membership.from_dict(await request.json())
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        outcome = take_membership(doc, request.headers.get(wire.H_SENDER) or "push")
        return {"status": outcome, "node_id": cfg.node_id, "epoch": runner().known_epoch}

    @app.post("/membership/rejoin", dependencies=peer_only)
    async def membership_rejoin(req: RejoinRequest, request: Request):
        """A restarted neighbour asks to rejoin from its saved chain while the directory is down (node/rejoin.py).

        Accepted only if (a) it presents my latest epoch (the chain has not changed since it went down) and
        (b) it connects with the TLS key the directory has on record for it."""
        r = runner()
        if cfg.directory is None:
            raise HTTPException(409, detail=f"{cfg.node_id} has a static chain (no directory configured)")
        member = r.peer(req.node_id)
        if member is None or request.headers.get(wire.H_SENDER) != req.node_id:
            raise HTTPException(403, detail=f"{req.node_id} is not a neighbour of {cfg.node_id} in its chain")
        require_caller(request, req.node_id, member, r.run_id, r.current_round)
        if req.epoch != r.known_epoch:
            reason = (f"the chain changed while {req.node_id} was away: it presents epoch {req.epoch}, {cfg.node_id} "
                      f"is at epoch {r.known_epoch}; wait for the directory")
            db.audit(node_id=cfg.node_id, event="rejoin_refused", run_id=r.run_id, round_num=r.current_round,
                     sender=req.node_id, receiver=cfg.node_id, detail={"reason": reason, "presented": req.epoch,
                                                                        "epoch": r.known_epoch})
            raise HTTPException(409, detail=reason)
        db.audit(node_id=cfg.node_id, event="rejoin_accepted", run_id=r.run_id, round_num=r.current_round,
                 sender=req.node_id, receiver=cfg.node_id, detail={"epoch": req.epoch})
        return {"status": "accepted", "node_id": cfg.node_id, "epoch": r.known_epoch}

    @app.post("/membership/leave", dependencies=peer_only)
    async def membership_leave():
        """Planned departure: the directory schedules the chain without me; my neighbours bridge to each other."""
        if cfg.directory is None:
            raise HTTPException(409, detail=f"{cfg.node_id} has a static chain (no directory configured)")
        r = runner()
        try:
            doc = await discovery.leave(r.client, cfg)
        except discovery.DirectoryError as exc:
            raise HTTPException(502, detail=f"directory refused or unreachable: {exc}")
        neighbours = list(r.neighbours)
        outcome = take_membership(doc, "directory (leave)")
        told = {}
        for n in neighbours:  # tell my neighbours directly too, so they bridge even if the directory's push is lost
            told[n.id] = await discovery.push_membership(r.client, n.url, doc, cfg.shared_secret, cfg.node_id)
        db.audit(node_id=cfg.node_id, event="leave_requested", run_id=r.run_id, round_num=r.current_round,
                 detail={"epoch": doc.epoch, "effective_round": doc.effective_round, "chain": list(doc.chain),
                         "told_neighbours": told})
        return {"status": outcome, "node_id": cfg.node_id, "epoch": doc.epoch, "effective_round": doc.effective_round,
                "chain": list(doc.chain), "told_neighbours": told}

    return app
