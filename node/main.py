"""MedChain hospital node (FastAPI).

Endpoints
    GET  /health          liveness
    GET  /status          identity, neighbours, current run state
    POST /round/start     start (or join) a training run; floods to neighbours
    POST /round/pause     pause at the next round boundary; floods
    POST /round/resume    resume a paused run; floods
    POST /update          receive a neighbour's weight array (raw float64 body)
    GET  /metrics         this node's per-round metrics for the current/last run

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
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import wire
from .config import NodeConfig, load_config
from .db import Database, NullDatabase
from .engine import NeighborUpdate
from .gossip import build_engine
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
    engine = build_engine(cfg.engine, trainer, bits=cfg.bits, rho=cfg.rho)
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
        runner = Runner(cfg, engine, db, client)
        state["runner"] = runner
        db.start()
        db.ensure_schema()
        db.register_node(node_id=cfg.node_id, display_name=cfg.display_name, url=cfg.advertise_url,
                         patients=trainer.n_patients, neighbours=[n.id for n in cfg.neighbours], engine=engine.label)
        db.audit(node_id=cfg.node_id, event="node_registered",
                 detail={"engine": engine.label, "patients": trainer.n_patients, "neighbours": [n.id for n in cfg.neighbours]})
        hb = asyncio.create_task(heartbeat_loop(runner))
        log.info("%s up on :%d with %d patients, neighbours=%s, engine=%s", cfg.node_id, cfg.port,
                 trainer.n_patients, [n.id for n in cfg.neighbours], engine.label)
        try:
            yield
        finally:
            hb.cancel()
            await runner.shutdown()
            await client.aclose()
            db.stop()

    app = FastAPI(title=f"MedChain node {cfg.node_id}", lifespan=lifespan)

    def runner() -> Runner:
        return state["runner"]

    async def flood(path: str, payload: dict, exclude: str | None) -> dict[str, str]:
        async def one(n):
            try:
                resp = await runner().client.post(f"{n.url}{path}", json=payload)
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
            "patients": trainer.n_patients,
            "n_train": len(trainer.y_train),
            "n_test": len(trainer.y_test),
            "bits": cfg.bits,
            "rho": cfg.rho,
            "neighbours": [{"id": n.id, "url": n.url, "last_send": r.neighbour_status.get(n.id)} for n in cfg.neighbours],
            "run_id": r.run_id,
            "run_state": r.state,
            "current_round": r.current_round,
            "total_rounds": r.total_rounds,
            "uptime_s": round(time.time() - started_at, 1),
        }

    @app.get("/metrics")
    async def metrics():
        r = runner()
        return {"node_id": cfg.node_id, "engine": engine.label, "run_id": r.run_id, "run_state": r.state,
                "total_rounds": r.total_rounds, "history": [asdict(m) for m in r.history]}

    @app.post("/round/start")
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

    @app.post("/round/pause")
    async def round_pause(req: ControlRequest | None = None):
        return await control("pause", req or ControlRequest())

    @app.post("/round/resume")
    async def round_resume(req: ControlRequest | None = None):
        return await control("resume", req or ControlRequest())

    @app.post("/update")
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
        if dtype not in wire.DTYPES:
            raise HTTPException(400, detail=f"unsupported dtype {dtype}")
        body = await request.body()
        claimed = h.get(wire.H_CHECKSUM, "")
        actual = wire.checksum(body)
        verified = claimed == actual
        db.audit(node_id=cfg.node_id, event="update_received", run_id=run_id, round_num=round_num, sender=sender,
                 receiver=cfg.node_id, payload_bytes=len(body), checksum=actual,
                 detail={"verified": verified, "dtype": dtype, "engine": engine.label})
        if not verified:
            raise HTTPException(400, detail="checksum mismatch")
        upd = NeighborUpdate(sender=sender, round=round_num, body=body, checksum=actual, dtype=dtype)
        participating = await runner().receive(run_id, upd)
        return {"accepted": True, "participating": participating, "node_id": cfg.node_id}

    return app
