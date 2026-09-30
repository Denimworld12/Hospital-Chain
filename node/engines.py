"""Engine registry: `engine:` in the node config picks the training engine and its round loop.

    gossip     naive gossip baseline (node/gossip.py); always float64, ignores bits/rho
    qgadmm     Q-GADMM (node/qgadmm/), `bits` in {2, 4, 8}
    gadmm_fp   full-precision GADMM: the same code path with quantization skipped
               (raw float32 deltas)
"""

from __future__ import annotations

import httpx

from .config import GADMM_FP, GOSSIP, QGADMM, NodeConfig
from .engine import Engine
from .gossip import GossipBaselineEngine
from .runner import Runner
from .trainer import LocalTrainer
from .writer import DbWriter

QGADMM_BITS = (2, 4, 8)


def build_engine(cfg: NodeConfig, trainer: LocalTrainer) -> Engine:
    if cfg.engine == GOSSIP:
        return GossipBaselineEngine(trainer)
    from .qgadmm import quantizer
    from .qgadmm.engine import QGADMMEngine
    from .qgadmm.state import ChainPosition

    if cfg.engine == QGADMM:
        if cfg.bits not in QGADMM_BITS:
            raise ValueError(f"engine qgadmm needs bits in {QGADMM_BITS}, got {cfg.bits}")
        bits = cfg.bits
    elif cfg.engine == GADMM_FP:
        bits = quantizer.FULL_PRECISION_BITS
    else:
        raise ValueError(f"unknown engine {cfg.engine!r}")
    pos = ChainPosition(cfg.chain, cfg.node_id)
    return QGADMMEngine(
        trainer.X_train, trainer.y_train, trainer.X_test, trainer.y_test,
        position=pos, rho=cfg.rho, alpha=cfg.alpha, bits=bits, local_epochs=cfg.local_epochs, lr=cfg.lr,
        seed=cfg.seed + pos.position,
    )


def build_runner(cfg: NodeConfig, engine: Engine, db: DbWriter, client: httpx.AsyncClient) -> Runner:
    if cfg.engine == GOSSIP:
        return Runner(cfg, engine, db, client)
    from .qgadmm.scheduler import ChainRoundScheduler
    from .qgadmm.state import checkpoint_path

    return ChainRoundScheduler(cfg, engine, db, client, checkpoint_path=checkpoint_path(cfg.checkpoint_dir, cfg.node_id))
