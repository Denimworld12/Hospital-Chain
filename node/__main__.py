"""`python -m node`: run the node on the port its config names (HTTPS when the config has `tls:`)."""

import logging

import uvicorn

from .config import load_config
from .main import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    tls = {"ssl_certfile": cfg.tls.cert, "ssl_keyfile": cfg.tls.key} if cfg.tls else {}
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning", **tls)


if __name__ == "__main__":
    main()
