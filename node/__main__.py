"""`python -m node`: run the node on the port its config names (HTTPS when the config has `tls:`, asking callers
for their certificate so a hospital's identity can be pinned on both ends; see node/pinning.py)."""

import logging

import uvicorn

from .config import load_config
from .main import create_app
from .pinning import uvicorn_config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    tls = {"cert": cfg.tls.cert, "key": cfg.tls.key, "ca": cfg.tls.ca} if cfg.tls else {}
    uvicorn.Server(uvicorn_config(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning", **tls)).run()


if __name__ == "__main__":
    main()
