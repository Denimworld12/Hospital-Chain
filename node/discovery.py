"""A node's calls to the directory service (directory/), and membership pushes to peers.

The directory is only ever asked "where do I go in the chain / take me out of it";
it never receives model weights, and nothing in the round loop calls it.
"""

from __future__ import annotations

import httpx

from . import wire
from .config import NodeConfig
from .membership import Membership


class DirectoryError(Exception):
    @property
    def unreachable(self) -> bool:
        """True when the directory could not be reached (including a TLS pin refusal), not when it answered."""
        return isinstance(self.__cause__, httpx.HTTPError)


def _auth(cfg: NodeConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg.directory.credential}"}


async def _call(client: httpx.AsyncClient, method: str, url: str, **kw) -> dict:
    try:
        resp = await client.request(method, url, **kw)
    except httpx.HTTPError as exc:
        raise DirectoryError(f"{url} unreachable ({type(exc).__name__}: {exc})") from exc
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise DirectoryError(f"{url} answered {resp.status_code}: {detail}")
    return resp.json()


async def register(client: httpx.AsyncClient, cfg: NodeConfig, public_key: str | None,
                   patients: int, tls_pin: str | None = None) -> tuple[Membership, bool]:
    """Returns (the membership document that places me, whether this registration added me to the chain).

    `tls_pin` is my TLS certificate's key pin: the directory records it for my id (this call is authenticated
    with my kit credential, and made over a connection that presents that certificate) and publishes it in
    the signed documents, which is how the other hospitals know which key is really me."""
    out = await _call(client, "POST", f"{cfg.directory.url}/register", headers=_auth(cfg),
                      json={"node_id": cfg.node_id, "url": cfg.advertise_url, "public_key": public_key,
                            "display_name": cfg.display_name, "patients": patients, "tls_spki_sha256": tls_pin})
    return Membership.from_dict(out["membership"]), bool(out.get("joined"))


async def leave(client: httpx.AsyncClient, cfg: NodeConfig) -> Membership:
    out = await _call(client, "POST", f"{cfg.directory.url}/leave", headers=_auth(cfg), json={"node_id": cfg.node_id})
    return Membership.from_dict(out["membership"])


async def fetch_membership(client: httpx.AsyncClient, peer_url: str, secret: str, sender: str) -> list[Membership]:
    out = await _call(client, "GET", f"{peer_url}/membership", headers={wire.H_SECRET: secret, wire.H_SENDER: sender})
    return [Membership.from_dict(d) for d in out.get("documents") or []]


async def push_membership(client: httpx.AsyncClient, url: str, doc: Membership, secret: str, sender: str) -> str:
    """POST a signed document to a node; returns its answer ("applied", "scheduled", ...) or why it failed."""
    try:
        resp = await client.post(f"{url}/membership", json=doc.to_dict(),
                                 headers={wire.H_SECRET: secret, wire.H_SENDER: sender})
        return str(resp.json().get("status", resp.status_code)) if resp.status_code == 200 else f"rejected-{resp.status_code}"
    except (httpx.HTTPError, ValueError) as exc:
        return f"unreachable ({type(exc).__name__})"
