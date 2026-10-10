"""Node configuration: the frozen `NodeConfig` every part of the node receives.

`load_config` (node/settings.py) builds it from, in order of precedence, the
environment, the optional YAML file named by $NODE_CONFIG, and the defaults here;
`build_config` validates the merged result. Nothing in node/ hardcodes a hospital
identity: node_id, neighbours, port, data slice, shared secret and engine
parameters all come from configuration.

Engines (`engine:`, exactly one of these): `gossip` (the naive baseline),
`qgadmm` (Q-GADMM, `bits` in {2, 4, 8}) and `gadmm_fp` (full-precision
GADMM). The ADMM engines also read `chain` (node order 1..N),
`rho`, `alpha`, `local_epochs`, `lr`, `epsilon`, `checkpoint_every`,
`checkpoint_dir` and `seed` (the node's RNG uses seed + its chain position).
The optional `assess` block overrides where the risk-assessment models are read from.

Membership comes from one of two places:
    static      `chain:` plus `neighbours:` (each with an optional `public_key`), as before
    directory   a `directory:` block (url, this hospital's credential, the directory's
                public key): the node registers at start and the directory's signed
                membership documents set its chain and neighbours (node/membership.py).
                `chain`/`neighbours` are then left out.

Security (both optional for local development; the shipped compose stack and the
provisioning kits always set them): `tls:` (cert, key, ca) serves HTTPS, verifies
peers' certificates against the network CA and pins each peer's key
(node/pinning.py): the directory block then needs `tls_pin` (the directory's key,
from the kit) and each static neighbour a `tls_pin`; with a directory, the other
hospitals' pins come from its signed membership documents. `signing_key:` is this
node's Ed25519 key. A node
with a signing key signs every /update and /snapshot it sends and rejects any update
whose sender it has no public key for, or whose signature does not verify.

Addresses are deployment, not identity, so they come from the environment without
editing the YAML (node/settings.py lists every variable): $ADVERTISE_ADDR (alias
$MEDCHAIN_ADVERTISE_URL) replaces `advertise_url` and is required with a directory,
$DIRECTORY_URL (alias $MEDCHAIN_DIRECTORY_URL) replaces `directory.url`, and
$MEDCHAIN_PEER_<ID>_URL replaces the `url` of static neighbour <ID> (upper-cased,
every run of other characters as `_`: hospital-b -> MEDCHAIN_PEER_HOSPITAL_B_URL).
That is how the same config points at Docker service names on one machine and at
LAN IPs or Tailscale names across machines.

Telemetry goes to the observer service (observer/) named by an `observer:` block
(url, token, and with TLS its `tls_pin`); see node/writer.py.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class Neighbour:
    id: str
    url: str
    public_key: str | None = None  # base64 Ed25519 key that verifies this neighbour's signatures
    tls_pin: str | None = None  # SHA-256 of its TLS certificate key (node/pinning.py); required with TLS


@dataclass(frozen=True)
class TlsConfig:
    cert: str
    key: str
    ca: str  # the network CA that issued every node's (and the directory's) certificate


@dataclass(frozen=True)
class DirectoryConfig:
    url: str
    credential_ref: str  # this hospital's pre-shared registration credential, inline or `file:<path>`
    public_key_ref: str  # base64 Ed25519 key that verifies the directory's documents, inline or `file:<path>`
    tls_pin_ref: str | None = None  # the directory's TLS key pin, `sha256:<hex>` or `file:<its tls.crt>` (from the kit)

    # read when used, not when the config loads, so a config can be inspected away from its key files
    @property
    def credential(self) -> str:
        ref = self.credential_ref
        return Path(ref[5:]).read_text().strip() if ref.startswith("file:") else ref

    @property
    def public_key(self) -> str:
        return _read_key(None, self.public_key_ref)

    @property
    def tls_pin(self) -> str | None:
        from .pinning import parse_pin

        return parse_pin(self.tls_pin_ref) if self.tls_pin_ref else None


@dataclass(frozen=True)
class ObserverConfig:
    """Where this node pushes its telemetry (observer/): never waited for by training."""

    url: str
    token_ref: str  # the observer's ingest token, inline or `file:<path>`
    tls_pin_ref: str | None = None  # the observer's TLS key pin (sha256:<hex> or file:<its tls.crt>), with TLS

    @property
    def token(self) -> str:
        ref = self.token_ref
        return Path(ref[5:]).read_text().strip() if ref.startswith("file:") else ref

    @property
    def tls_pin(self) -> str | None:
        from .pinning import parse_pin

        return parse_pin(self.tls_pin_ref) if self.tls_pin_ref else None


@dataclass(frozen=True)
class HttpConfig:
    connect_timeout_s: float = 1.5
    read_timeout_s: float = 3.0


@dataclass(frozen=True)
class TrainingConfig:
    rounds: int = 20
    local_epochs: int = 2
    learning_rate: float = 0.005
    test_fraction: float = 0.25
    seed: int = 7


@dataclass(frozen=True)
class TimingConfig:
    heartbeat_interval_s: float = 5.0
    round_timeout_s: float = 6.0
    round_pause_s: float = 1.0


GOSSIP = "gossip"
QGADMM = "qgadmm"
GADMM_FP = "gadmm_fp"
ENGINE_ALIASES = {"gossip": GOSSIP, "qgadmm": QGADMM, "gadmm_fp": GADMM_FP}
ADMM_ENGINES = (QGADMM, GADMM_FP)

ADVERTISE_URL_ENV = "MEDCHAIN_ADVERTISE_URL"
DIRECTORY_URL_ENV = "MEDCHAIN_DIRECTORY_URL"
DEFAULT_PORT = 8000


def peer_url_env(node_id: str) -> str:
    """The environment variable that overrides neighbour `node_id`'s URL."""
    return f"MEDCHAIN_PEER_{re.sub(r'[^A-Za-z0-9]+', '_', node_id).upper()}_URL"


@dataclass(frozen=True)
class AssessConfig:
    """Model files for the per-patient risk assessment endpoints (inference only)."""

    tabular_model: str = "/app/models/heart-risk.joblib"
    ecg_model: str = "/app/models/ecg-resnet18.onnx"


@dataclass(frozen=True)
class NodeConfig:
    node_id: str
    display_name: str
    port: int
    advertise_url: str
    data_slice: str
    shared_secret: str
    neighbours: tuple[Neighbour, ...] = ()
    engine: str = GOSSIP
    rho: float = 1.0
    bits: int = 64
    host: str = "0.0.0.0"
    # ADMM engines (qgadmm / gadmm_fp); the gossip baseline ignores these
    chain: tuple[str, ...] = ()
    alpha: float = 1.0
    local_epochs: int = 50
    lr: float = 0.01
    epsilon: float = 1e-3
    checkpoint_every: int = 5
    checkpoint_dir: str = "/checkpoints"
    seed: int = 42
    data_path: str | None = None  # node state that must survive a restart (saved membership); default checkpoint_dir
    training: TrainingConfig = field(default_factory=TrainingConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)
    assess: AssessConfig = field(default_factory=AssessConfig)
    tls: TlsConfig | None = None
    signing_key: str | None = None
    directory: DirectoryConfig | None = None
    observer: ObserverConfig | None = None
    http: HttpConfig = field(default_factory=HttpConfig)
    image_version: str = "dev"

    @property
    def scheme(self) -> str:
        return "https" if self.tls else "http"

    def neighbour(self, node_id: str) -> Neighbour | None:
        return next((n for n in self.neighbours if n.id == node_id), None)

    @property
    def is_admm(self) -> bool:
        return self.engine in ADMM_ENGINES

    @property
    def state_dir(self) -> str:
        return self.data_path or self.checkpoint_dir


def load_config(path: str | os.PathLike | None = None) -> NodeConfig:
    """Environment > YAML ($NODE_CONFIG or `path`, optional) > defaults; see node/settings.py."""
    from .settings import load_config as load

    return load(path)


def build_config(raw: dict, path: str = "config") -> NodeConfig:
    """Validate one merged configuration (`path` names its source in error messages)."""
    raw = dict(raw)
    if raw.get("data_path"):  # DATA_PATH holds the slice and the checkpoints unless they are named
        raw.setdefault("checkpoint_dir", str(Path(raw["data_path"]) / "checkpoints"))
        if raw.get("node_id"):
            raw.setdefault("data_slice", str(Path(raw["data_path"]) / f"{raw['node_id']}.csv"))
    missing = [k for k in ("node_id", "data_slice", "shared_secret") if k not in raw]
    if missing:
        raise ValueError(f"{path}: missing required keys {missing}")
    neighbours = tuple(
        Neighbour(id=str(n["id"]), url=str(n["url"]).rstrip("/"),
                  public_key=_read_key(path, n.get("public_key")),
                  tls_pin=_read_pin(path, n.get("tls_pin")))
        for n in raw.get("neighbours") or []
    )
    if any(n.id == raw["node_id"] for n in neighbours):
        raise ValueError(f"{path}: a node cannot be its own neighbour")
    if not str(raw["shared_secret"]):
        raise ValueError(f"{path}: shared_secret must not be empty")
    port = int(raw.get("port", DEFAULT_PORT))
    engine = str(raw.get("engine", GOSSIP))
    if engine not in ENGINE_ALIASES:
        raise ValueError(f"{path}: unknown engine {engine!r}; use one of {sorted(ENGINE_ALIASES)}")
    engine = ENGINE_ALIASES[engine]
    chain = tuple(str(c) for c in raw.get("chain") or ())
    directory = _directory(path, raw.get("directory"))
    if directory and (chain or neighbours):
        raise ValueError(f"{path}: with a `directory:` the directory sets the chain; drop `chain`/`neighbours`")
    if engine in ADMM_ENGINES and not directory:
        _check_chain(path, str(raw["node_id"]), chain, neighbours)
    tls = TlsConfig(**{k: str(v) for k, v in raw["tls"].items()}) if raw.get("tls") else None
    if tls:  # peers are identified by their pinned TLS key, not by the address they are dialled at
        unpinned = [n.id for n in neighbours if not n.tls_pin]
        if unpinned:
            raise ValueError(f"{path}: with `tls:` every neighbour needs a `tls_pin` (sha256:<hex> or "
                             f"file:<its tls.crt>); missing for {unpinned}")
        if directory and not directory.tls_pin_ref:
            raise ValueError(f"{path}: with `tls:` the directory block needs `tls_pin` (the directory's key pin "
                             "from this hospital's kit, e.g. file:/pki/directory/tls.crt)")
    scheme = "https" if tls else "http"
    # one port for binding and for the advertised address; no fallback to the node id as a hostname
    advertise_url = _address(raw.get("advertise_url"), scheme, port)
    if directory and not advertise_url:
        raise ValueError(f"{path}: a node with a directory must say where its peers reach it: set ADVERTISE_ADDR "
                         "(host:port, host, or a URL) or `advertise_url`")
    if directory:
        directory = replace(directory, url=_address(directory.url, scheme, None))
    observer = _observer(path, raw.get("observer"), scheme)
    if tls and observer and not observer.tls_pin_ref:
        raise ValueError(f"{path}: with `tls:` the observer block needs `tls_pin` (the observer's key pin, "
                         "e.g. file:/pki/observer/tls.crt)")
    for url in [advertise_url, *(n.url for n in neighbours), *([directory.url] if directory else []),
                *([observer.url] if observer else [])]:
        if not url:
            continue
        if not url.startswith(f"{scheme}://"):
            raise ValueError(f"{path}: {url} must be {scheme}:// ({'TLS is on' if tls else 'no `tls:` configured'})")
    return NodeConfig(
        node_id=str(raw["node_id"]),
        display_name=str(raw.get("display_name", raw["node_id"])),
        port=port,
        advertise_url=advertise_url,
        data_slice=str(raw["data_slice"]),
        shared_secret=str(raw["shared_secret"]),
        neighbours=neighbours,
        engine=engine,
        rho=float(raw.get("rho", 1.0)),
        bits=int(raw.get("bits", 64)),
        host=str(raw.get("host", "0.0.0.0")),
        chain=chain,
        alpha=float(raw.get("alpha", 1.0)),
        local_epochs=int(raw.get("local_epochs", 50)),
        lr=float(raw.get("lr", 0.01)),
        epsilon=float(raw.get("epsilon", 1e-3)),
        checkpoint_every=int(raw.get("checkpoint_every", 5)),
        checkpoint_dir=str(raw.get("checkpoint_dir", "/checkpoints")),
        seed=int(raw.get("seed", 42)),
        data_path=str(raw["data_path"]) if raw.get("data_path") else None,
        training=TrainingConfig(**(raw.get("training") or {})),
        timing=TimingConfig(**(raw.get("timing") or {})),
        assess=AssessConfig(**(raw.get("assess") or {})),
        tls=tls,
        signing_key=str(raw["signing_key"]) if raw.get("signing_key") else None,
        directory=directory,
        observer=observer,
        http=HttpConfig(**{k: float(v) for k, v in (raw.get("http") or {}).items()}),
        image_version=str(raw.get("image_version") or "dev"),
    )


def _read_key(path, value) -> str | None:
    """A public key given inline (base64) or as `file:<path>`."""
    if not value:
        return None
    value = str(value)
    if value.startswith("file:"):
        from .security import load_public_key, public_key_b64

        return public_key_b64(load_public_key(value[5:]))
    return value


def _read_pin(path, value) -> str | None:
    if not value:
        return None
    from .pinning import parse_pin

    try:
        return parse_pin(str(value))
    except (ValueError, OSError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _directory(path, raw) -> DirectoryConfig | None:
    if not raw:
        return None
    missing = [k for k in ("url", "public_key") if not raw.get(k)]
    if not (raw.get("credential") or raw.get("credential_file")):
        missing.append("credential or credential_file")
    if missing:
        raise ValueError(f"{path}: directory needs {missing}")
    credential = str(raw["credential"]) if raw.get("credential") else f"file:{raw['credential_file']}"
    return DirectoryConfig(url=str(raw["url"]).rstrip("/"), credential_ref=credential,
                           public_key_ref=str(raw["public_key"]),
                           tls_pin_ref=str(raw["tls_pin"]) if raw.get("tls_pin") else None)


def _address(value, scheme: str, port: int | None) -> str:
    """A URL as given, or `host[:port]` completed with the scheme (and this node's port when it has none)."""
    value = str(value or "").strip().rstrip("/")
    if not value or "://" in value:
        return value
    _, sep, last = value.rpartition(":")
    has_port = bool(sep) and last.isdigit()  # "[::1]" ends in "1]", not a port
    return f"{scheme}://{value}" if has_port or port is None else f"{scheme}://{value}:{port}"


def _observer(path, raw, scheme: str) -> ObserverConfig | None:
    if not raw:
        return None
    if not raw.get("url"):
        raise ValueError(f"{path}: observer needs `url`")
    if not (raw.get("token") or raw.get("token_file")):
        raise ValueError(f"{path}: observer needs `token` or `token_file` (its ingest token)")
    token = str(raw["token"]) if raw.get("token") else f"file:{raw['token_file']}"
    return ObserverConfig(url=_address(raw["url"], scheme, None), token_ref=token,
                          tls_pin_ref=str(raw["tls_pin"]) if raw.get("tls_pin") else None)


def _env_url(path, var: str, configured: str) -> str:
    """The URL from $var when it is set (it must be http(s)://...), else the configured one."""
    override = os.environ.get(var, "").strip()
    if not override:
        return configured.rstrip("/")
    if not re.match(r"https?://[^/]", override):
        raise ValueError(f"{path}: ${var}={override!r} must be an http:// or https:// URL")
    return override.rstrip("/")


def _check_chain(path, node_id: str, chain: tuple[str, ...], neighbours: tuple[Neighbour, ...]) -> None:
    """ADMM engines need the chain order, and the configured neighbours must be exactly my chain neighbours."""
    if not chain:
        raise ValueError(f"{path}: engine needs `chain:` (node ids in chain order 1..N)")
    if len(set(chain)) != len(chain) or node_id not in chain:
        raise ValueError(f"{path}: chain must list distinct node ids including {node_id}")
    i = chain.index(node_id)
    adjacent = {chain[j] for j in (i - 1, i + 1) if 0 <= j < len(chain)}
    if adjacent != {n.id for n in neighbours}:
        raise ValueError(f"{path}: neighbours {sorted(n.id for n in neighbours)} must be the chain neighbours {sorted(adjacent)}")
