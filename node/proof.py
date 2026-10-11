"""Proof of origin: a signed status message, every few seconds, about the machine this node really runs on.

The dashboard's Start/Pause buttons prove nothing about where a hospital is. So each node
periodically POSTs the observer (POST /proof) a small JSON document, signed with the same
Ed25519 key that signs its training updates (node/security.py, context "proof"):

    host        hostname, OS and version, uptime, Tailscale device name. In Docker these
                come from the install script, which reads them on the host and puts them in
                the kit's .env (MEDCHAIN_HOST_*); uptime is counted from the host's boot time.
                Without them (make up, a kit), the container's own values, labelled as such.
    container   the kernel and uptime the node itself sees
    neighbours  the round-trip time of a GET /health to each chain neighbour, measured now
                over the same pinned TLS connections training uses
    data_rows   how many rows its local data slice holds (the count, never the data)

plus its node id, the time, a per-start boot id and a sequence number (so an old message
cannot be replayed). The observer checks the signature against the key the directory has
on record for that hospital and audits any message that fails (observer/app.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import time

import httpx

from . import security, wire

HOST_ENV = {"hostname": "MEDCHAIN_HOST_NAME", "os": "MEDCHAIN_HOST_OS", "booted_at": "MEDCHAIN_HOST_BOOTED_AT",
            "tailscale_device": "MEDCHAIN_TAILSCALE_DEVICE"}


def _uptime() -> float | None:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def host_facts(now: float | None = None) -> dict:
    """The machine's facts: from the install script when it set them, else what this process sees."""
    now = time.time() if now is None else now
    env = {k: os.environ.get(v, "").strip() for k, v in HOST_ENV.items()}
    try:
        booted = float(env["booted_at"]) if env["booted_at"] else None
    except ValueError:
        booted = None
    if booted is not None and not 0 <= now - booted < 10 * 365 * 86400:  # not a boot time: report no uptime
        booted = None
    if env["hostname"]:
        return {"source": "host, read by the install script", "hostname": env["hostname"], "os": env["os"] or None,
                "uptime_s": round(now - booted) if booted else None, "tailscale_device": env["tailscale_device"] or None}
    return {"source": "this process (no install script facts)", "hostname": platform.node(),
            "os": f"{platform.system()} {platform.release()}", "uptime_s": _uptime(),
            "tailscale_device": env["tailscale_device"] or None}


async def neighbour_rtts(client: httpx.AsyncClient, neighbours) -> dict[str, float | None]:
    """Milliseconds for a GET /health to each neighbour (None when it does not answer)."""

    async def one(n):
        t0 = time.perf_counter()
        try:
            r = await client.get(f"{n.url}/health", timeout=3)
            return n.id, round((time.perf_counter() - t0) * 1000, 1) if r.status_code == 200 else None
        except httpx.HTTPError:
            return n.id, None

    return dict(await asyncio.gather(*(one(n) for n in neighbours)))


class Prover:
    """Builds and signs this node's proof messages (one boot id per start, a rising sequence number)."""

    def __init__(self, node_id: str, signing_key, data_rows: int, image_version: str):
        self.node_id, self.key, self.data_rows, self.image_version = node_id, signing_key, data_rows, image_version
        self.boot_id = secrets.token_hex(8)
        self.started = time.time()
        self.seq = 0

    def message(self, rtts: dict[str, float | None]) -> tuple[bytes, dict[str, str]]:
        self.seq += 1
        now = time.time()
        body = json.dumps({
            "node_id": self.node_id, "boot_id": self.boot_id, "seq": self.seq, "sent_at": now,
            "host": host_facts(now),
            "container": {"kernel": f"{platform.system()} {platform.release()}", "uptime_s": round(now - self.started)},
            "neighbours_rtt_ms": rtts, "data_rows": self.data_rows, "image_version": self.image_version,
        }, sort_keys=True).encode()
        headers = {wire.H_SENDER: self.node_id, "Content-Type": "application/json"}
        headers[wire.H_SIGNATURE] = security.sign(self.key, body, headers, "proof")
        return body, headers


def verify(public_key_b64: str, body: bytes, headers) -> dict:
    """The verified message (raises security.SignatureError or ValueError)."""
    security.verify(security.parse_public_key(public_key_b64), headers.get(wire.H_SIGNATURE), body, headers, "proof")
    msg = json.loads(body)
    if not isinstance(msg, dict) or msg.get("node_id") != headers.get(wire.H_SENDER):
        raise ValueError("the message is not about the hospital that signed it")
    return msg
