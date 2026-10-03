"""Chain membership documents: who is in the chain, in which order, from which round.

A `Membership` is one epoch of the chain: the ordered node ids (positions 1..N; odd =
HEAD group, even = TAIL group), each member's address and update-signing public key,
and the round boundary at which it takes effect. The directory service
(directory/) issues them, signed with its Ed25519 key, whenever a hospital joins or
leaves; a node accepts a document from anyone (the directory, a neighbour, the
leaving hospital) as long as the directory's signature verifies, so a membership
change can travel peer to peer and nobody can forge one.

A static chain from the node's own config (no directory) is epoch 0, unsigned.

Changes only ever take effect at a round boundary: `effective_round` R means "rounds
R and later of run `run_id` use this chain". A document for a run the node is not
in (or with no round at all) takes effect at once, because no round is in flight.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Mapping

from . import security


@dataclass(frozen=True)
class Member:
    id: str
    url: str
    public_key: str | None = None  # base64 Ed25519 key that verifies this member's /update signatures
    display_name: str | None = None


@dataclass(frozen=True)
class Membership:
    epoch: int
    chain: tuple[str, ...]
    members: Mapping[str, Member] = field(default_factory=dict)
    effective_round: int | None = None
    run_id: str | None = None
    reason: str = ""
    signature: str | None = None

    def __post_init__(self):
        if len(set(self.chain)) != len(self.chain):
            raise ValueError(f"chain has duplicate nodes: {list(self.chain)}")
        missing = [n for n in self.chain if n not in self.members]
        if missing:
            raise ValueError(f"chain members without an address: {missing}")

    # -- topology -----------------------------------------------------------
    def has(self, node_id: str) -> bool:
        return node_id in self.chain

    def neighbours_of(self, node_id: str) -> tuple[str, ...]:
        if node_id not in self.chain:
            return ()
        i = self.chain.index(node_id)
        return tuple(self.chain[j] for j in (i - 1, i + 1) if 0 <= j < len(self.chain))

    def links(self) -> set[frozenset[str]]:
        return {frozenset(p) for p in zip(self.chain, self.chain[1:])}

    def same_chain(self, other: "Membership") -> bool:
        return self.chain == other.chain

    # -- wire form ------------------------------------------------------------
    def to_dict(self, *, signed: bool = True) -> dict:
        out = {
            "epoch": self.epoch,
            "chain": list(self.chain),
            "members": {k: {"url": m.url, "public_key": m.public_key, "display_name": m.display_name}
                        for k, m in sorted(self.members.items())},
            "effective_round": self.effective_round,
            "run_id": self.run_id,
            "reason": self.reason,
        }
        if signed:
            out["signature"] = self.signature
        return out

    def canonical(self) -> bytes:
        """The bytes the directory signs: the document without its signature, canonical JSON."""
        return json.dumps(self.to_dict(signed=False), sort_keys=True, separators=(",", ":")).encode()

    @classmethod
    def from_dict(cls, raw: Mapping) -> "Membership":
        try:
            members = {str(k): Member(id=str(k), url=str(v["url"]).rstrip("/"), public_key=v.get("public_key"),
                                      display_name=v.get("display_name"))
                       for k, v in (raw.get("members") or {}).items()}
            eff = raw.get("effective_round")
            return cls(epoch=int(raw["epoch"]), chain=tuple(str(c) for c in raw["chain"]), members=members,
                       effective_round=None if eff is None else int(eff), run_id=raw.get("run_id"),
                       reason=str(raw.get("reason") or ""), signature=raw.get("signature"))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"malformed membership document: {exc}") from exc

    def signed_by(self, key) -> "Membership":
        return replace(self, signature=security.sign(key, self.canonical(), {}, "membership"))

    def verify(self, directory_key) -> None:
        """Raise security.SignatureError unless the directory signed exactly this document."""
        security.verify(directory_key, self.signature, self.canonical(), {}, "membership")


def link_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))
