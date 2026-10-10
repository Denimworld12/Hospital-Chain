"""Rejoining the chain after a restart while the directory is down.

A node keeps the directory-signed membership documents it holds (current and scheduled)
in `<data_path>/<node_id>.membership.json`, rewritten whenever it takes a new one. Each
document carries the chain epoch, which the directory bumps on every join, leave
(bridge) and re-registration, so equal epochs mean an unchanged chain.

On restart, if the directory cannot be reached, the node does not simply trust its
saved chain. It asks each saved neighbour over pinned TLS (POST /membership/rejoin).
The neighbour accepts only if:
  (a) the epoch the node presents is the neighbour's own latest epoch, so the chain has
      not changed while it was gone; and
  (b) the node's TLS certificate key is the one the neighbour has on record for it.
Any refusal means the saved state is stale or not really this node, and the node goes
back to waiting for the directory, as it did before. Saved documents are verified
against the directory key again when they are loaded, so a tampered file is ignored.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import security, wire
from .config import NodeConfig
from .membership import Membership

log = logging.getLogger("medchain.rejoin")


def state_path(cfg: NodeConfig) -> Path:
    return Path(cfg.state_dir) / f"{cfg.node_id}.membership.json"


def save(path: Path, docs: list[Membership]) -> None:
    """Atomically replace the saved documents (only directory-signed ones are worth keeping)."""
    docs = [d for d in docs if d.signature]
    if not docs:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"epoch": max(d.epoch for d in docs), "documents": [d.to_dict() for d in docs]}))
    os.replace(tmp, path)


def load(path: Path, directory_key) -> list[Membership]:
    """The saved documents, oldest first, each re-verified against the directory key ([] if none or any is bad)."""
    if not path.is_file():
        return []
    try:
        docs = [Membership.from_dict(d) for d in json.loads(path.read_text())["documents"]]
        for d in docs:
            d.verify(directory_key)
    except (OSError, ValueError, KeyError, TypeError, security.SignatureError) as exc:
        log.warning("ignoring saved membership %s: %s", path, exc)
        return []
    return sorted(docs, key=lambda d: d.epoch)


@dataclass
class Attempt:
    outcome: str  # accepted | refused | unreachable | unusable
    epoch: int | None = None
    answers: dict[str, str] = field(default_factory=dict)
    reason: str = ""


def check_self(cfg: NodeConfig, docs: list[Membership], public_key: str | None) -> str | None:
    """Why my saved state cannot be offered at all (None if it can): I must still be the member it describes."""
    if not docs:
        return "no saved membership"
    newest = docs[-1]
    me = newest.members.get(cfg.node_id)
    if not newest.has(cfg.node_id) or me is None:
        return f"not in the saved chain (epoch {newest.epoch})"
    if me.url != cfg.advertise_url:
        return f"my address changed ({me.url} -> {cfg.advertise_url})"
    if me.public_key != public_key:
        return "my update-signing key changed"
    return None


async def ask_neighbours(client: httpx.AsyncClient, cfg: NodeConfig, docs: list[Membership]) -> Attempt:
    """POST /membership/rejoin to each saved neighbour, presenting my latest saved epoch."""
    newest = docs[-1]
    epoch = max(d.epoch for d in docs)
    answers: dict[str, str] = {}
    refused = accepted = False
    for m in newest.neighbours_of(cfg.node_id):
        try:
            resp = await client.post(f"{newest.members[m].url}/membership/rejoin", json={"node_id": cfg.node_id,
                                                                                          "epoch": epoch},
                                     headers={wire.H_SECRET: cfg.shared_secret, wire.H_SENDER: cfg.node_id})
        except httpx.HTTPError as exc:
            answers[m] = f"unreachable ({type(exc).__name__}: {exc})"
            continue
        if resp.status_code == 200:
            accepted = True
            answers[m] = "accepted"
        else:
            refused = True
            try:
                answers[m] = f"refused {resp.status_code}: {resp.json().get('detail', resp.text)}"
            except ValueError:
                answers[m] = f"refused {resp.status_code}: {resp.text}"
    outcome = "refused" if refused else "accepted" if accepted else "unreachable"
    return Attempt(outcome, epoch, answers)
