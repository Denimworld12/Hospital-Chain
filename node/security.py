"""Node identity: Ed25519 signatures on every /update (and /snapshot), and TLS between nodes.

A checksum only catches accidental corruption: whoever controls the wire can rewrite
the bytes and recompute it. So every node holds an Ed25519 signing key, and each
/update carries a signature over the body's SHA-256 and every X-MedChain-* header
that steers training (run, round, sender, receiver, sequence, fingerprints, residual
view, stop proposal, membership epoch). The receiver verifies it with the sender's
public key, which it learned from the directory's signed membership document (or,
for a static chain, from its own config). An unknown sender, or a signature that
does not verify, is rejected before the packet reaches the engine.

TLS encrypts the links and authenticates the server end: every node and the
directory present a certificate issued by the network's CA (scripts/pki.py), and
clients verify it, hostname included.
"""

from __future__ import annotations

import base64
import hashlib
import ssl
from pathlib import Path
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .wire import H_SIGNATURE

_PREFIX = "x-medchain-"
_UNSIGNED = {H_SIGNATURE.lower(), "x-medchain-secret"}  # the signature itself; the secret never goes in a log


class SignatureError(Exception):
    pass


def load_signing_key(path: str | Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 private key")
    return key


def public_key_b64(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    """Raw 32-byte Ed25519 public key, base64 (how keys travel in configs and membership documents)."""
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return base64.b64encode(pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def parse_public_key(b64: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(b64, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"not a base64 Ed25519 public key: {b64!r}") from exc


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    """A public key file: either PEM or the raw base64 form."""
    text = Path(path).read_text().strip()
    if text.startswith("-----BEGIN"):
        key = serialization.load_pem_public_key(text.encode())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError(f"{path} is not an Ed25519 public key")
        return key
    return parse_public_key(text)


def message(body: bytes, headers: Mapping[str, str], context: str) -> bytes:
    """The exact bytes that get signed: a context tag, every X-MedChain-* header (sorted), the body digest."""
    signed = sorted((k.lower(), str(v)) for k, v in headers.items()
                    if k.lower().startswith(_PREFIX) and k.lower() not in _UNSIGNED)
    lines = [f"medchain/{context}/v1"] + [f"{k}:{v}" for k, v in signed] + [hashlib.sha256(body).hexdigest()]
    return "\n".join(lines).encode()


def sign(key: Ed25519PrivateKey, body: bytes, headers: Mapping[str, str], context: str) -> str:
    return base64.b64encode(key.sign(message(body, headers, context))).decode()


def verify(pub: Ed25519PublicKey, signature: str | None, body: bytes, headers: Mapping[str, str], context: str) -> None:
    if not signature:
        raise SignatureError("missing signature")
    try:
        pub.verify(base64.b64decode(signature, validate=True), message(body, headers, context))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise SignatureError("signature does not verify") from exc


def client_ssl_context(ca_file: str | Path) -> ssl.SSLContext:
    """Verify the server's certificate against the network CA, hostname included."""
    return ssl.create_default_context(cafile=str(ca_file))
