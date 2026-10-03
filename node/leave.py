"""`python -m node.leave`: this hospital leaves the chain at the next round boundary (planned departure).

Run inside a node's container (`docker compose exec hospital-b python -m node.leave`) or next
to its config. It calls the node's own POST /membership/leave, which asks the directory to
take this hospital out and tells both neighbours to bridge to each other.
"""

import json
import ssl
import sys
import urllib.request

from . import wire
from .config import load_config


def main() -> int:
    cfg = load_config()
    req = urllib.request.Request(f"{cfg.scheme}://127.0.0.1:{cfg.port}/membership/leave", method="POST", data=b"",
                                 headers={wire.H_SECRET: cfg.shared_secret})
    ctx = ssl._create_unverified_context()  # our own node on loopback
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            print(json.dumps(json.load(resp), indent=2))
            return 0
    except urllib.error.HTTPError as exc:
        print(f"leave refused ({exc.code}): {exc.read().decode()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
