"""Preflight: can this machine's node reach each of its neighbours, and is that path direct or relayed?

    python scripts/preflight.py --node https://localhost:8001 --ca pki/ca.crt

Run it on each hospital's machine. For every neighbour the local node reports (GET
/status), it prints how this machine reaches that neighbour's address:

    direct      a direct path: a Tailscale peer reached peer-to-peer, or an address
                outside Tailscale (LAN, phone hotspot)
    relayed     a Tailscale peer reached only through a DERP relay. This is allowed as a
                fallback: still end-to-end WireGuard between the two machines, but slower,
                so give rounds a longer timeout
    unreachable the neighbour's node does not answer /health

Tailscale's view comes from `tailscale ping` (which also establishes the path) and
`tailscale status --json`, both read on the host. Containers cannot see it. The
dialled address must be the neighbour's Tailscale IP or MagicDNS name for the path to
be reported.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import shutil
import ssl
import subprocess
import sys
from urllib.parse import urlsplit

import httpx

PING_VIA = re.compile(r"\bvia (DERP\((?P<derp>[^)]*)\)|(?P<addr>\S+))")


def classify_ping(output: str) -> tuple[str, str] | None:
    """`tailscale ping` output -> ("direct", endpoint) or ("relayed", DERP region); None if it never ponged."""
    pongs = [m for m in PING_VIA.finditer(output)]
    if not pongs:
        return None
    last = pongs[-1]  # the first pong may still go via DERP while the direct path is being set up
    if last.group("derp") is not None:
        return "relayed", f"DERP {last.group('derp')}"
    return "direct", last.group("addr")


def find_peer(status: dict, host: str) -> dict | None:
    """The Tailscale peer whose IP, MagicDNS name or host name is `host`."""
    host = host.lower().rstrip(".")
    for peer in (status.get("Peer") or {}).values():
        names = {str(ip) for ip in peer.get("TailscaleIPs") or []}
        dns = str(peer.get("DNSName") or "").lower().rstrip(".")
        names |= {dns, dns.split(".")[0], str(peer.get("HostName") or "").lower()}
        if host in names - {""}:
            return peer
    return None


def classify_status(peer: dict) -> tuple[str, str]:
    if peer.get("CurAddr"):
        return "direct", peer["CurAddr"]
    if peer.get("Relay"):
        return "relayed", f"DERP {peer['Relay']}"
    return "unknown", "no traffic yet (run with --ping)"


def is_tailscale_ip(host: str) -> bool:
    try:
        return ipaddress.ip_address(host) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return host.endswith(".ts.net")


def tailscale(*args: str) -> str | None:
    exe = shutil.which("tailscale")
    if not exe:
        return None
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


def path_to(host: str, *, ping: bool) -> tuple[str, str]:
    raw = tailscale("status", "--json")
    status = json.loads(raw) if raw else {}
    peer = find_peer(status, host) if status else None
    if peer is None:
        if is_tailscale_ip(host):
            return "unknown", "a Tailscale address, but not a peer this machine's Tailscale knows"
        return "direct", "not a Tailscale address (LAN or hotspot)"
    if ping:
        got = classify_ping(tailscale("ping", "--c", "3", "--timeout", "5s", host) or "")
        if got:
            return got
    return classify_status(peer)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--node", required=True, help="this machine's node, e.g. https://localhost:8001")
    ap.add_argument("--ca", help="the network CA (pki/ca.crt) to check the node's certificate")
    ap.add_argument("--no-ping", action="store_true", help="read `tailscale status` only, without pinging")
    args = ap.parse_args(argv)
    verify: ssl.SSLContext | bool = True
    if args.ca:  # the network CA, without a hostname match: nodes are identified by their pinned key
        verify = ssl.create_default_context(cafile=args.ca)
        verify.check_hostname = False
    try:
        st = httpx.get(f"{args.node.rstrip('/')}/status", timeout=5, verify=verify).json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"preflight: cannot read {args.node}/status: {exc}", file=sys.stderr)
        return 2
    print(f"{st['node_id']}: chain {' - '.join(st.get('chain') or []) or '(not placed yet)'}")
    worst = 0
    for n in st.get("neighbours") or []:
        host = urlsplit(n["url"]).hostname or ""
        kind, how = path_to(host, ping=not args.no_ping)
        try:
            alive = httpx.get(f"{n['url']}/health", timeout=5, verify=verify).status_code == 200
        except httpx.HTTPError:
            alive = False
        if not alive:
            kind, worst = "unreachable", 1
        print(f"  {st['node_id']} <-> {n['id']:<14} {kind:<11} {how}  ({n['url']}, last send: {n.get('last_send')})")
    if not st.get("neighbours"):
        print("  no neighbours yet")
    return worst


if __name__ == "__main__":
    sys.exit(main())
