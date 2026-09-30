"""MedChain hospital node (FastAPI).

Endpoints
    GET  /health          liveness
    GET  /status          identity, neighbours, current run state
    POST /round/start     start (or join) a training run; floods to neighbours
    POST /round/pause     pause at the next round boundary; floods
    POST /round/resume    resume a paused run; floods
    POST /update          receive a neighbour's update (the engine's packet bytes)
    GET  /snapshot        ADMM engines: my full theta_hat for a restarted/drifted neighbour
    GET  /metrics         this node's per-round metrics for the current/last run
    POST /assess, /assess/pdf, /assess/ecg, /assess/ecg/pdf
                          per-patient risk assessment (node/assess/routes.py)

Identity comes entirely from the YAML config named by $NODE_CONFIG.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import wire
from .assess.routes import Assessor, build_router as build_assess_router
from .config import NodeConfig, load_config
from .db import Database, NullDatabase
from .engine import NeighborUpdate
from .engines import build_engine, build_runner
from .runner import Runner, new_run_id
from .trainer import LocalTrainer
from .writer import DbWriter

log = logging.getLogger("medchain.node")


class StartRequest(BaseModel):
    run_id: str | None = None
    rounds: int | None = Field(default=None, ge=1, le=500)
    origin: str | None = None
    initiated_by: str | None = None


class ControlRequest(BaseModel):
    op_id: str | None = None
    origin: str | None = None


def create_app(cfg: NodeConfig | None = None, database: Database | None = None) -> FastAPI:
    cfg = cfg or load_config()
    if database is None:
        dsn = os.environ.get("DATABASE_URL")
        database = Database(dsn) if dsn else NullDatabase()
    trainer = LocalTrainer(
        cfg.data_slice,
        test_fraction=cfg.training.test_fraction,
        seed=cfg.training.seed,
        learning_rate=cfg.training.learning_rate,
        local_epochs=cfg.training.local_epochs,
    )
    engine = build_engine(cfg, trainer)
    assessor = Assessor(cfg.assess.tabular_model, cfg.assess.ecg_model)
    db = DbWriter(database)
    started_at = time.time()
    state: dict = {}

    async def heartbeat_loop(runner: Runner) -> None:
        while True:
            db.heartbeat(cfg.node_id, runner.run_id, runner.state, runner.current_round)
            await asyncio.sleep(cfg.timing.heartbeat_interval_s)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.5))
        runner = build_runner(cfg, engine, db, client)
        state["runner"] = app.state.runner = runner
        db.start()
        db.ensure_schema()
        db.register_node(node_id=cfg.node_id, display_name=cfg.display_name, url=cfg.advertise_url,
                         patients=trainer.n_patients, neighbours=[n.id for n in cfg.neighbours], engine=engine.label)
        db.audit(node_id=cfg.node_id, event="node_registered",
                 detail={"engine": engine.label, "patients": trainer.n_patients, "neighbours": [n.id for n in cfg.neighbours]})
        hb = asyncio.create_task(heartbeat_loop(runner))
        if cfg.is_admm:  # a checkpoint on the volume means we were killed mid-run: rejoin it
            state["rejoin"] = asyncio.create_task(runner.rejoin())
        log.info("%s up on :%d with %d patients, neighbours=%s, engine=%s", cfg.node_id, cfg.port,
                 trainer.n_patients, [n.id for n in cfg.neighbours], engine.label)
        try:
            yield
        finally:
            hb.cancel()
            if "rejoin" in state:
                state["rejoin"].cancel()
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

        results = await asyncio.gather(*(one(n) for n in cfg.neighbours if n.id != exclude))
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
            **({"chain": list(cfg.chain), "position": engine.pos.position, "group": engine.pos.group,
                "d": engine.d, "stop_at": r.stop_at, "converged_round": r.converged_round} if cfg.is_admm else {}),
            "neighbours": [{"id": n.id, "url": n.url, "last_send": r.neighbour_status.get(n.id)} for n in cfg.neighbours],
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
        if cfg.neighbour(sender) is None:
            db.audit(node_id=cfg.node_id, event="update_rejected", run_id=run_id, round_num=round_num, sender=sender,
                     receiver=cfg.node_id, detail={"reason": "sender is not a configured neighbour"})
            raise HTTPException(403, detail=f"{sender} is not a neighbour of {cfg.node_id}")
        dtype = h.get(wire.H_DTYPE, "float64")
        if dtype != engine.dtype:
            raise HTTPException(400, detail=f"unsupported dtype {dtype}: {cfg.node_id} runs {engine.label}")
        try:
            seq = int(h[wire.H_SEQ]) if cfg.is_admm else None
        except (KeyError, ValueError):
            raise HTTPException(400, detail="missing or invalid sequence header")
        body = await request.body()
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
        answer = await runner().receive(run_id, upd, {k: h[k] for k in (wire.H_VIEW, wire.H_STOP) if h.get(k)})
        db.audit(node_id=cfg.node_id, event="update_received", run_id=run_id, round_num=round_num, sender=sender,
                 receiver=cfg.node_id, payload_bytes=len(body), checksum=actual,
                 detail={"verified": True, "dtype": dtype, "engine": engine.label,
                         **({"seq": seq, "on_time": answer["on_time"]} if cfg.is_admm else {})})
        return {"accepted": True, **answer, "node_id": cfg.node_id}

    @app.get("/snapshot", dependencies=peer_only)
    async def snapshot(requester: str, rejoin_round: int | None = None):
        """A neighbour's one-off full resync: my theta_hat (plus its mirror and our edge's lambda). Never data."""
        if not cfg.is_admm:
            raise HTTPException(404, detail=f"{engine.label} keeps no theta_hat snapshot")
        if cfg.neighbour(requester) is None:
            raise HTTPException(403, detail=f"{requester} is not a neighbour of {cfg.node_id}")
        from .qgadmm.engine import snapshot_body

        snap = await asyncio.to_thread(engine.snapshot_for, requester)
        body = snapshot_body(snap)
        r = runner()
        if rejoin_round is not None:
            r.note_rejoin(requester, rejoin_round)
        db.audit(node_id=cfg.node_id, event="snapshot_served", run_id=r.run_id, round_num=r.current_round,
                 sender=cfg.node_id, receiver=requester, payload_bytes=len(body), checksum=wire.checksum(body),
                 detail={"seq": snap["seq"], "rejoin_round": rejoin_round, "engine": engine.label,
                         "contents": "theta_hat, my mirror of the requester, our edge's lambda (model state only)"})
        return Response(content=body, media_type="application/octet-stream",
                        headers={wire.H_SEQ: str(snap["seq"]), wire.H_MIRROR_SEQ: str(snap["mirror_seq"]),
                                 wire.H_CHECKSUM: wire.checksum(body)})

    return app
