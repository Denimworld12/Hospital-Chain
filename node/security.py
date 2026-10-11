"""Node identity: Ed25519 signatures on every /update (and /snapshot), and TLS between nodes.

A checksum only catches accidental corruption: whoever controls the wire can rewrite
the bytes and recompute it. So every node holds an Ed25519 signing key, and each
/update carries a signature over the body's SHA-256 and every X-MedChain-* header
that steers training (run, round, sender, receiver, sequence, fingerprints, residual
view, stop proposal, membership epoch). The receiver verifies it with the sender's
public key, which it learned from the directory's signed membership document (or,
for a static chain, from its own config). An unknown sender, or a signature that
does not verify, is rejected before the packet reaches the engine.

TLS encrypts the links and authenticates both ends: every node and the directory
present a certificate issued by the network's CA (scripts/pki.py), and the peer's
certificate key is pinned to the one on record for that party (node/pinning.py).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
from pathlib import Path
from typing import Iterable, Mapping

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .wire import H_SIGNATURE

_PREFIX = "x-medchain-"
_UNSIGNED = {H_SIGNATURE.lower(), "x-medchain-secret"}  # the signature itself; the secret never goes in a log


CERT_VALID_DAYS = 3650
# certificates start a day early, so a peer whose clock runs slow (a laptop's Docker VM after sleep) still accepts
# one that was issued a moment ago
BACKDATE = dt.timedelta(days=1)


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



def san(host: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def issue_certificate(ca_cert: x509.Certificate, ca_key, public_key, name: str, hosts: Iterable[str],
                      days: int = CERT_VALID_DAYS) -> x509.Certificate:
    """A TLS certificate for `public_key`, signed by the network CA: CN `name`, SAN `hosts` (scripts/pki.py and
    the directory's join-code enrollment, directory/enroll.py, both issue them)."""
    now = dt.datetime.now(dt.timezone.utc)
    return (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_cert.subject).public_key(public_key).serial_number(x509.random_serial_number())
            .not_valid_before(now - BACKDATE).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([san(h) for h in dict.fromkeys(hosts)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
