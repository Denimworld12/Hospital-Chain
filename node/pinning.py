"""TLS by identity: a peer's certificate is accepted only if its key is the one on record for that party.

Hostnames identify addresses, not hospitals, and a laptop's address changes (a phone
hotspot hands out a new IP, a mesh name is not resolvable inside a container). So
the TLS check is about *who*, not *where*:

- Certificate verification stays on. Every certificate must still chain to the
  network CA (`CERT_REQUIRED`), so an arbitrary self-signed certificate is refused
  in the handshake. Only the hostname/SAN match is dropped.
- On top of that, the certificate's public key is pinned. A pin is the SHA-256 of
  the certificate's DER SubjectPublicKeyInfo, as lowercase hex.
- Which key is expected comes from the directory's Ed25519-signed membership
  document: each hospital registers its pin with the directory using its own kit
  credential, proving that it holds the key by connecting with that certificate. The
  directory's own pin comes from the hospital's kit (`directory.tls_pin`), never
  from the directory. A static chain pins each neighbour in the node's config.

Both ends check the pin:
- The dialler (`PinnedTransport`) checks it after the handshake and before any
  request bytes leave, looking the dialled address up in the signed documents.
- The receiver (`uvicorn_config` + `peer_spki`) asks for a client certificate and
  checks it against the pin of the hospital the caller claims to be.

A mismatch is never downgraded to an address-based check.
"""

from __future__ import annotations

import hashlib
import re
import ssl
from pathlib import Path
from typing import Callable, Iterable, NamedTuple
from urllib.parse import urlsplit

import httpcore
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization

PEER_SPKI = "medchain.peer_spki"  # ASGI scope key: the caller's certificate pin (None without a client certificate)
_HEX = re.compile(r"^[0-9a-f]{64}$")


class Pin(NamedTuple):
    party: str  # node id, or "directory"
    label: str  # how to name it to a person ("Hospital B", "the directory")
    pin: str | None  # None: the party is known but no key is on record for it


def spki_sha256(cert: bytes | x509.Certificate) -> str:
    """The pin of a certificate (DER bytes or a parsed certificate)."""
    if isinstance(cert, (bytes, bytearray)):
        cert = x509.load_der_x509_certificate(bytes(cert))
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).hexdigest()


def cert_file_pin(path: str | Path) -> str:
    return spki_sha256(x509.load_pem_x509_certificate(Path(path).read_bytes()))


def parse_pin(value: str) -> str:
    """A pin as `sha256:<hex>` / `<hex>`, or `file:<certificate.pem>` (pinned to that certificate's key)."""
    value = str(value).strip()
    if value.startswith("file:"):
        return cert_file_pin(value[5:])
    pin = value.removeprefix("sha256:").lower()
    if not _HEX.match(pin):
        raise ValueError(f"not a TLS key pin (sha256:<64 hex digits> or file:<cert.pem>): {value!r}")
    return pin


def check_pin(pin: str | None) -> str | None:
    """Validate a pin from the wire (a registration, a membership document)."""
    if pin is None:
        return None
    if not _HEX.match(str(pin)):
        raise ValueError(f"tls_spki_sha256 must be 64 lowercase hex digits, got {pin!r}")
    return str(pin)


# -- messages: say what happened and what to do -------------------------------------------


def key_changed(p: Pin, where: str) -> str:
    if p.party == "directory":
        return (f"The directory's TLS key changed: {where} does not match the directory pin in this hospital's "
                "kit (directory.tls_pin). If the directory was re-keyed, get a refreshed kit from whoever "
                "provisioned the network; otherwise something else is answering at the directory's address.")
    return (f"{p.label}'s TLS key changed: {where} does not match the key the directory has on record for "
            f"{p.party}. If {p.party} was re-keyed, re-register it with the directory (restart its node with its "
            "kit credential so it registers the new key); otherwise something else is using its identity.")


def no_key_on_record(p: Pin) -> str:
    return (f"No TLS key is on record for {p.label} ({p.party}), so its certificate cannot be checked. Re-register "
            f"{p.party} with the directory (restart its node with its kit credential).")


def caller_problem(presented: str | None, p: Pin) -> tuple[int, str] | None:
    """Receiver side: why the caller claiming to be `p.party` is refused (HTTP code, message), or None if it may pass."""
    if presented is None:
        return 401, (f"{p.party} must connect with its TLS certificate (the one in its kit); no client certificate "
                     "was presented")
    if p.pin is None:
        return 403, no_key_on_record(p)
    if presented != p.pin:
        return 403, key_changed(p, f"the certificate {p.party} connected with")
    return None


# -- the dialler ------------------------------------------------------------------------------


def origin(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    return (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


def pins_at(host: str, port: int, parties: Iterable[tuple[str, str, str, str | None]]) -> list[Pin]:
    """The pins of every (party, label, url, pin) whose url is host:port."""
    want = (host.lower().strip("[]"), port)
    return [Pin(party, label, pin) for party, label, url, pin in parties if url and origin(url) == want]


class PinError(httpcore.ConnectError):
    """The peer's certificate chains to the CA but its key is not the one on record (httpx: ConnectError)."""


def is_pin_error(exc: BaseException) -> bool:
    while exc is not None:
        if isinstance(exc, PinError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def client_ssl_context(ca_file: str | Path, cert: str | Path | None = None,
                       key: str | Path | None = None) -> ssl.SSLContext:
    """Verify the server's certificate chain against the network CA (no hostname match: the key is pinned
    instead, by PinnedTransport) and present our own certificate so the server can pin us too."""
    ctx = ssl.create_default_context(cafile=str(ca_file))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    if cert:
        ctx.load_cert_chain(str(cert), str(key) if key else None)
    return ctx


class _PinnedStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream: httpcore.AsyncNetworkStream, host: str, port: int, expected):
        self._stream, self._host, self._port, self._expected = stream, host, port, expected

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._stream.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        await self._stream.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._stream.aclose()

    def get_extra_info(self, info: str):
        return self._stream.get_extra_info(info)

    async def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None,
                        timeout: float | None = None) -> httpcore.AsyncNetworkStream:
        tls = await self._stream.start_tls(ssl_context, server_hostname, timeout)
        problem = _server_problem(tls, self._host, self._port, self._expected)
        if problem:
            await tls.aclose()
            raise PinError(problem)
        return tls


def _server_problem(tls, host: str, port: int, expected) -> str | None:
    """Why the certificate this TLS stream's server presented is refused (None if its key is on record there)."""
    der = tls.get_extra_info("ssl_object").getpeercert(True)
    presented = spki_sha256(der) if der else None
    where = f"{host}:{port}"
    candidates = expected(host, port)
    if not candidates:
        return (f"No TLS key is on record for {where}: it is not an address in this node's signed membership "
                "or kit, so the connection was refused.")
    if any(p.pin and p.pin == presented for p in candidates):
        return None
    p = candidates[0]
    return no_key_on_record(p) if p.pin is None else key_changed(p, f"the certificate served at {where}")


class _PinningBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, expected):
        self._inner, self._expected = httpcore.AnyIOBackend(), expected

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options=None) -> httpcore.AsyncNetworkStream:
        stream = await self._inner.connect_tcp(host, port, timeout=timeout, local_address=local_address,
                                               socket_options=socket_options)
        return _PinnedStream(stream, host, port, self._expected)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return await self._inner.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """An httpx transport whose every TLS connection is pinned: `expected(host, port)` returns the pins on
    record for that address (from the signed membership / the kit); the handshake's certificate must match one."""

    def __init__(self, ssl_context: ssl.SSLContext, expected: Callable[[str, int], list[Pin]],
                 limits: httpx.Limits = httpx.Limits()):
        super().__init__(verify=ssl_context, limits=limits)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context, max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections, keepalive_expiry=limits.keepalive_expiry,
            network_backend=_PinningBackend(expected))


def pinned_client(ca: str, cert: str | None, key: str | None, expected: Callable[[str, int], list[Pin]],
                  timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=PinnedTransport(client_ssl_context(ca, cert, key), expected))


# the same, for the blocking client a background thread uses (node/writer.py's observer sink)
class _PinnedSyncStream(httpcore.NetworkStream):
    def __init__(self, stream: httpcore.NetworkStream, host: str, port: int, expected):
        self._stream, self._host, self._port, self._expected = stream, host, port, expected

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(max_bytes, timeout)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, timeout)

    def close(self) -> None:
        self._stream.close()

    def get_extra_info(self, info: str):
        return self._stream.get_extra_info(info)

    def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None,
                  timeout: float | None = None) -> httpcore.NetworkStream:
        tls = self._stream.start_tls(ssl_context, server_hostname, timeout)
        problem = _server_problem(tls, self._host, self._port, self._expected)
        if problem:
            tls.close()
            raise PinError(problem)
        return tls


class _PinningSyncBackend(httpcore.NetworkBackend):
    def __init__(self, expected):
        self._inner, self._expected = httpcore.SyncBackend(), expected

    def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                    socket_options=None) -> httpcore.NetworkStream:
        stream = self._inner.connect_tcp(host, port, timeout=timeout, local_address=local_address,
                                         socket_options=socket_options)
        return _PinnedSyncStream(stream, host, port, self._expected)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return self._inner.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class PinnedSyncTransport(httpx.HTTPTransport):
    def __init__(self, ssl_context: ssl.SSLContext, expected: Callable[[str, int], list[Pin]],
                 limits: httpx.Limits = httpx.Limits()):
        super().__init__(verify=ssl_context, limits=limits)
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context, max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections, keepalive_expiry=limits.keepalive_expiry,
            network_backend=_PinningSyncBackend(expected))


def pinned_sync_client(ca: str, cert: str | None, key: str | None, expected: Callable[[str, int], list[Pin]],
                       timeout: httpx.Timeout) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=PinnedSyncTransport(client_ssl_context(ca, cert, key), expected))


# -- the receiver -----------------------------------------------------------------------------


def peer_spki(request) -> str | None:
    """The pin of the client certificate this request's connection presented (None if it presented none)."""
    return request.scope.get(PEER_SPKI)


def _peer_aware_protocol(registry: dict):
    """uvicorn's HTTP protocol, remembering each TLS connection's client-certificate pin by peer address."""
    from uvicorn.protocols.http.auto import AutoHTTPProtocol

    class PeerAwareProtocol(AutoHTTPProtocol):
        def connection_made(self, transport):
            super().connection_made(transport)
            ssl_object = transport.get_extra_info("ssl_object")
            der = ssl_object.getpeercert(True) if ssl_object is not None else None
            if self.client:
                registry[tuple(self.client)] = (id(self), spki_sha256(der) if der else None)

        def connection_lost(self, exc):
            if self.client and registry.get(tuple(self.client), (None,))[0] == id(self):
                registry.pop(tuple(self.client), None)
            super().connection_lost(exc)

    return PeerAwareProtocol


class _PeerCertApp:
    def __init__(self, app, registry: dict):
        self.app, self.registry = app, registry

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            entry = self.registry.get(tuple(scope.get("client") or ()))
            scope = {**scope, PEER_SPKI: entry[1] if entry else None}
        await self.app(scope, receive, send)


def uvicorn_config(app, *, host: str, port: int, cert: str | None = None, key: str | None = None,
                   ca: str | None = None, **kw):
    """A uvicorn.Config serving `app` over HTTPS that asks for (but does not require) a CA-issued client
    certificate and exposes its pin to the app as `peer_spki(request)`. Callers without one (the dashboard,
    health checks) can still reach the endpoints that do not need a hospital's identity."""
    import uvicorn

    if not cert:
        return uvicorn.Config(app, host=host, port=port, **kw)
    registry: dict = {}
    tls = {"ssl_certfile": cert, "ssl_keyfile": key}
    if ca:
        tls |= {"ssl_ca_certs": ca, "ssl_cert_reqs": ssl.CERT_OPTIONAL}
    return uvicorn.Config(_PeerCertApp(app, registry), host=host, port=port, http=_peer_aware_protocol(registry),
                          proxy_headers=False, **tls, **kw)
