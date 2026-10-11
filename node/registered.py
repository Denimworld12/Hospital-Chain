"""Has this node got into the network? For the install scripts, run inside the node's container:

    docker compose ... exec -T node python -m node.registered

It asks the node itself (GET /status on this machine) how its registration with the directory went,
prints one plain line, and exits with:

    0  registered: the directory placed this hospital
    3  refused: the directory refuses this hospital's credential or keys (the organiser removed it); a
       new join code is needed
    1  not yet: still starting, or the directory is not answering yet (the node keeps trying)
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request

from .config import load_config

REFUSED = 3


def check() -> tuple[int, str]:
    cfg = load_config()
    ctx = ssl._create_unverified_context()  # this machine's own node, at 127.0.0.1: nothing to pin
    try:
        with urllib.request.urlopen(f"{cfg.scheme}://127.0.0.1:{cfg.port}/status", timeout=3, context=ctx) as r:
            status = json.load(r)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return 1, f"the node is not answering yet ({exc})"
    reg = status.get("registration") or {}
    if reg.get("state") in ("registered", "static"):
        return 0, f"registered as {status.get('node_id')}"
    if reg.get("state") == "refused":
        return REFUSED, f"the hub refuses this hospital: {reg.get('detail')}"
    return 1, f"not registered yet ({reg.get('detail') or reg.get('state') or 'starting'})"


def main() -> int:
    code, line = check()
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
