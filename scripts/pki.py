"""Issue a MedChain network's keys: a CA, TLS certificates, update-signing keys, directory credentials.

    python scripts/pki.py --out .pki --directory directory:directory,localhost \\
        --service observer:observer,localhost \\
        --hospital hospital-a:hospital-a,localhost --hospital hospital-b:hospital-b,localhost ...

Layout (idempotent: anything that already exists is kept, so re-running only adds what
is missing - e.g. a new hospital - and never re-keys a running network; only a TLS
certificate that does not cover a party's requested names is re-issued):

    ca.crt, ca.key                 the network CA (keep ca.key with whoever adds hospitals)
    directory/tls.crt, tls.key     the directory's HTTPS certificate
    directory/signing.key, .pub    signs membership documents; every node gets signing.pub
    directory/hospitals.yaml       who may register, in placement order: id + SHA-256 of its credential
    <service>/tls.crt, tls.key     another service's HTTPS certificate (the observer); nodes pin its key
    <hospital>/tls.crt, tls.key    the node's HTTPS certificate (SAN = the names it is reached by; peers
                                   pin its key, which the node registers with the directory)
    <hospital>/signing.key, .pub   signs its /update packets; the public half travels in the
                                   directory's membership documents
    <hospital>/credential          its pre-shared directory credential (only the hash leaves this folder)

Self-signed for the demo; a real deployment would issue the TLS certificates from the
hospitals' own PKI and keep the CA key offline (see README).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import secrets
import sys
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from node.security import CERT_VALID_DAYS, issue_certificate, public_key_b64  # noqa: E402

VALID_DAYS = CERT_VALID_DAYS


def _pem_key(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _write(path: Path, data: bytes, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if private:
        path.chmod(0o600)


def ensure_ca(out: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    crt, key = out / "ca.crt", out / "ca.key"
    if crt.is_file() and key.is_file():
        return (x509.load_pem_x509_certificate(crt.read_bytes()),
                serialization.load_pem_private_key(key.read_bytes(), password=None))
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MedChain network CA")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=VALID_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
            .sign(k, hashes.SHA256()))
    _write(key, _pem_key(k), private=True)
    _write(crt, cert.public_bytes(serialization.Encoding.PEM))
    return cert, k


def _covers(crt: Path, names: list[str]) -> bool:
    san = x509.load_pem_x509_certificate(crt.read_bytes()).extensions.get_extension_for_class(x509.SubjectAlternativeName)
    have = {str(v) for v in san.value.get_values_for_type(x509.DNSName)}
    have |= {str(v) for v in san.value.get_values_for_type(x509.IPAddress)}
    return set(names) <= have


def ensure_identity(out: Path, name: str, hosts: list[str], *, credential: bool) -> Path:
    """TLS certificate (signed by the CA) + Ed25519 signing key (+ a directory credential) for one party."""
    ca_cert, ca_key = ensure_ca(out)
    d = out / name
    names = list(dict.fromkeys([*hosts, name]))
    if not (d / "tls.crt").is_file() or not _covers(d / "tls.crt", names):  # new, or reached at a new address
        # a new address keeps the party's TLS key: peers pin the key (node/pinning.py), not the address
        k = (serialization.load_pem_private_key((d / "tls.key").read_bytes(), password=None)
             if (d / "tls.key").is_file() else ec.generate_private_key(ec.SECP256R1()))
        cert = issue_certificate(ca_cert, ca_key, k.public_key(), name, names, VALID_DAYS)
        _write(d / "tls.key", _pem_key(k), private=True)
        _write(d / "tls.crt", cert.public_bytes(serialization.Encoding.PEM))
    if not (d / "signing.key").is_file():
        sk = Ed25519PrivateKey.generate()
        _write(d / "signing.key", _pem_key(sk), private=True)
        _write(d / "signing.pub", (public_key_b64(sk) + "\n").encode())
    if credential and not (d / "credential").is_file():
        _write(d / "credential", (secrets.token_urlsafe(24) + "\n").encode(), private=True)
    return d


def credential_sha256(out: Path, hospital: str) -> str:
    return hashlib.sha256((out / hospital / "credential").read_text().strip().encode()).hexdigest()


def write_hospitals(out: Path, directory: str, hospitals: list[str]) -> Path:
    """The directory's allow-list, in placement order; hospitals already listed keep their place."""
    path = out / directory / "hospitals.yaml"
    known = [h["id"] for h in (yaml.safe_load(path.read_text()) or [])] if path.is_file() else []
    order = list(dict.fromkeys([*known, *hospitals]))
    rows = [{"id": h, "credential_sha256": credential_sha256(out, h)} for h in order if (out / h / "credential").is_file()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Hospitals allowed to register, in placement order (scripts/pki.py). Only hashes:\n"
                    "# each hospital keeps its own credential.\n" + yaml.safe_dump(rows, sort_keys=False))
    return path


def parse_party(spec: str) -> tuple[str, list[str]]:
    """`id:host1,host2` -> (id, [hosts]); hosts default to the id itself."""
    name, _, hosts = spec.partition(":")
    hs = [h.strip() for h in hosts.split(",") if h.strip()] or [name]
    return name.strip(), hs


def issue(out: Path, directory: tuple[str, list[str]], hospitals: list[tuple[str, list[str]]],
          services: list[tuple[str, list[str]]] = ()) -> None:
    ensure_ca(out)
    ensure_identity(out, directory[0], directory[1], credential=False)
    for name, hosts in services:
        ensure_identity(out, name, hosts, credential=False)
    for name, hosts in hospitals:
        ensure_identity(out, name, hosts, credential=True)
    write_hospitals(out, directory[0], [h for h, _ in hospitals])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / ".pki")
    ap.add_argument("--directory", default="directory:directory,localhost,127.0.0.1",
                    help="id:host,host... the directory is reached by")
    ap.add_argument("--hospital", action="append", default=[], help="id:host,host... (repeatable, placement order)")
    ap.add_argument("--service", action="append", default=[],
                    help="id:host,host... another service's TLS identity, e.g. the observer (repeatable)")
    args = ap.parse_args(argv)
    issue(args.out, parse_party(args.directory), [parse_party(h) for h in args.hospital],
          [parse_party(s) for s in args.service])
    print(f"keys and certificates in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
