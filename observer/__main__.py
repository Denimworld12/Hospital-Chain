"""`python -m observer`: run the observer service (HTTPS when TLS_CERT is set)."""

import logging

import uvicorn

from node.pinning import uvicorn_config

from .app import ObserverSettings, create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = ObserverSettings()
    tls = {"cert": s.tls_cert, "key": s.tls_key, "ca": s.tls_ca} if s.tls_cert else {}
    uvicorn.Server(uvicorn_config(create_app(s), host=s.bind_host, port=s.observer_port, log_level="warning",
                                  **tls)).run()


if __name__ == "__main__":
    main()
