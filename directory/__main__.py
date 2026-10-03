"""`python -m directory`: run the directory service on the port its config names (HTTPS with `tls:`)."""

import logging

import uvicorn

from .app import create_app, load_config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    tls = {"ssl_certfile": cfg.tls_cert, "ssl_keyfile": cfg.tls_key} if cfg.tls_cert else {}
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning", **tls)


if __name__ == "__main__":
    main()
