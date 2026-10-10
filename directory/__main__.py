"""`python -m directory`: run the directory service on the port its config names (HTTPS with `tls:`)."""

import logging

import uvicorn

from node.pinning import uvicorn_config

from .app import create_app, load_config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    tls = {"cert": cfg.tls_cert, "key": cfg.tls_key, "ca": cfg.ca} if cfg.tls_cert else {}
    uvicorn.Server(uvicorn_config(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning", **tls)).run()


if __name__ == "__main__":
    main()
