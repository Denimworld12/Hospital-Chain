"""Node settings: environment > YAML ($NODE_CONFIG, optional) > defaults, producing the same frozen NodeConfig.

The YAML loader (node/config.py) stays the source for the single-machine stack. This
layer overlays the environment on top of it, so a node on another machine can be
configured from an `.env` file alone, or from its kit's YAML plus a few overrides.

    NODE_CONFIG                  optional YAML file (config/hospital-*.yaml, a kit's config)
    NODE_ID                      node_id
    BIND_HOST                    host to bind (default 0.0.0.0)
    NODE_PORT                    port to bind; also the port of ADVERTISE_ADDR when it names none (default 8000)
    ADVERTISE_ADDR               where peers reach this node: host, host:port or a URL; required with a
                                 directory (alias MEDCHAIN_ADVERTISE_URL)
    DIRECTORY_URL                the directory (alias MEDCHAIN_DIRECTORY_URL)
    DIRECTORY_CREDENTIAL_FILE    this hospital's directory credential (from its kit)
    DIRECTORY_PUBLIC_KEY         the directory's document-signing key: base64 or file:<path>
    DIRECTORY_TLS_PIN            the directory's TLS key pin: sha256:<hex> or file:<its tls.crt>
    OBSERVER_URL                 the observer (observer/) that takes this node's telemetry
    OBSERVER_TOKEN / OBSERVER_TOKEN_FILE   its ingest token
    OBSERVER_TLS_PIN             the observer's TLS key pin (with TLS)
    DATA_PATH                    node state: the data slice, checkpoints, saved membership, audit.db
    DATA_SLICE                   this hospital's CSV (default DATA_PATH/<NODE_ID>.csv)
    CHECKPOINT_DIR               default DATA_PATH/checkpoints
    ALGORITHM                    engine: qgadmm | gadmm_fp | gossip
    BITS, EPSILON                Q-GADMM bits per coordinate; consensus threshold
    ROUND_TIMEOUT_S, ROUND_PAUSE_S
    HTTP_CONNECT_TIMEOUT_S, HTTP_READ_TIMEOUT_S
    TLS_CERT, TLS_KEY, TLS_CA    this node's certificate and key, and the network CA
    SIGNING_KEY                  this node's Ed25519 update-signing key
    SHARED_SECRET                the network's X-MedChain-Secret
    IMAGE_VERSION                the image's version, reported by /health
    MEDCHAIN_PEER_<ID>_URL       a static neighbour's address (node/config.py)

Blank variables count as unset.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .config import NodeConfig, _env_url, build_config, peer_url_env


def _env(*names: str):
    return Field(default=None, validation_alias=AliasChoices(*names))


class NodeSettings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, env_ignore_empty=True, extra="ignore")

    node_config: str | None = _env("NODE_CONFIG")
    node_id: str | None = _env("NODE_ID")
    bind_host: str | None = _env("BIND_HOST")
    node_port: int | None = _env("NODE_PORT")
    advertise_addr: str | None = _env("ADVERTISE_ADDR", "MEDCHAIN_ADVERTISE_URL")
    directory_url: str | None = _env("DIRECTORY_URL", "MEDCHAIN_DIRECTORY_URL")
    directory_credential_file: str | None = _env("DIRECTORY_CREDENTIAL_FILE")
    directory_public_key: str | None = _env("DIRECTORY_PUBLIC_KEY")
    directory_tls_pin: str | None = _env("DIRECTORY_TLS_PIN")
    observer_url: str | None = _env("OBSERVER_URL")
    observer_token: str | None = _env("OBSERVER_TOKEN")
    observer_token_file: str | None = _env("OBSERVER_TOKEN_FILE")
    observer_tls_pin: str | None = _env("OBSERVER_TLS_PIN")
    data_path: str | None = _env("DATA_PATH")
    data_slice: str | None = _env("DATA_SLICE")
    checkpoint_dir: str | None = _env("CHECKPOINT_DIR")
    algorithm: str | None = _env("ALGORITHM")
    bits: int | None = _env("BITS")
    epsilon: float | None = _env("EPSILON")
    round_timeout_s: float | None = _env("ROUND_TIMEOUT_S")
    round_pause_s: float | None = _env("ROUND_PAUSE_S")
    http_connect_timeout_s: float | None = _env("HTTP_CONNECT_TIMEOUT_S")
    http_read_timeout_s: float | None = _env("HTTP_READ_TIMEOUT_S")
    tls_cert: str | None = _env("TLS_CERT")
    tls_key: str | None = _env("TLS_KEY")
    tls_ca: str | None = _env("TLS_CA")
    signing_key: str | None = _env("SIGNING_KEY")
    shared_secret: str | None = _env("SHARED_SECRET")
    image_version: str | None = _env("IMAGE_VERSION")


def _block(raw: dict, key: str, values: dict) -> None:
    """Overlay the set `values` onto the YAML block `key` (creating it if any value is set)."""
    values = {k: v for k, v in values.items() if v is not None}
    if values:
        raw[key] = {**(raw.get(key) or {}), **values}


def apply_env(raw: dict, s: NodeSettings, source: str) -> dict:
    """The YAML mapping with every set environment variable laid over it."""
    raw = copy.deepcopy(raw)
    top = {"node_id": s.node_id, "host": s.bind_host, "port": s.node_port, "advertise_url": s.advertise_addr,
           "data_path": s.data_path, "data_slice": s.data_slice, "checkpoint_dir": s.checkpoint_dir,
           "engine": s.algorithm, "bits": s.bits, "epsilon": s.epsilon, "signing_key": s.signing_key,
           "shared_secret": s.shared_secret, "image_version": s.image_version}
    raw.update({k: v for k, v in top.items() if v is not None})
    _block(raw, "timing", {"round_timeout_s": s.round_timeout_s, "round_pause_s": s.round_pause_s})
    _block(raw, "http", {"connect_timeout_s": s.http_connect_timeout_s, "read_timeout_s": s.http_read_timeout_s})
    _block(raw, "tls", {"cert": s.tls_cert, "key": s.tls_key, "ca": s.tls_ca})
    if s.directory_credential_file:  # the kit's file replaces an inline credential
        (raw.get("directory") or {}).pop("credential", None)
    _block(raw, "directory", {"url": s.directory_url, "credential_file": s.directory_credential_file,
                              "public_key": s.directory_public_key, "tls_pin": s.directory_tls_pin})
    if s.observer_token or s.observer_token_file:
        for k in ("token", "token_file"):
            (raw.get("observer") or {}).pop(k, None)
    _block(raw, "observer", {"url": s.observer_url, "token": s.observer_token, "token_file": s.observer_token_file,
                             "tls_pin": s.observer_tls_pin})
    for n in raw.get("neighbours") or []:
        n["url"] = _env_url(source, peer_url_env(str(n["id"])), str(n["url"]))
    return raw


def load_config(path: str | os.PathLike | None = None) -> NodeConfig:
    s = NodeSettings()
    path = path or s.node_config
    raw, source = {}, "environment"
    if path:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"node config not found: {p!s} (NODE_CONFIG)")
        raw, source = yaml.safe_load(p.read_text()) or {}, str(p)
    return build_config(apply_env(raw, s, source), source)
