"""Join a MedChain network with a one-time join code, instead of a kit made and sent by the organiser.

    python -m node.enroll --hub 100.101.1.99 --name "St Mary's" --advertise 100.101.1.20 --out /kit
                          (the code comes from --code or $MEDCHAIN_JOIN_CODE)

The organiser creates a join code on the dashboard's admin page: single use, expires after
30 minutes. This node then:

1. makes its own keys, here, and never sends a private one anywhere: a TLS key (sent only
   as a certificate signing request), an Ed25519 update-signing key (its public half) and
   a directory credential (its SHA-256);
2. POSTs them to the hub's directory (`POST /enroll`) with its chosen display name and a
   proof that it holds the join code (an HMAC keyed by a hash of the code; the code itself
   never goes over the wire, so a machine answering in the hub's place learns nothing);
3. checks the answer before trusting anything in it: it must carry an HMAC under the same
   key (only the real hub, which holds that hash, can make one), echo this request's nonce,
   and name the TLS key that answered this very connection as the directory's;
4. writes a kit folder laid out exactly like a scripts/provision.py kit (`.env`, `pki/`,
   `data/`, `config/node.yaml`, `deploy/node.yml`), so `docker compose --project-directory
   <out> -f deploy/node.yml up -d` runs it like any other hospital.

The display name is free text and is what the dashboard, the logs and the audit trail
show; the hub refuses one another hospital already has. The hub answers with: a
certificate for this node's TLS key, signed by the network CA; the
CA certificate; the directory's document key and TLS certificate (pinned from then on); the
observer's TLS certificate and port and an ingest token for this hospital alone; the
network secret (written to `pki/network/shared_secret`, never printed); the engine settings
every hospital shares; the data slice the organiser assigned; and a generated node id. The
node id, not the display name, is the hospital's identity on the wire.

Nothing secret is printed. Running it again on an enrolled kit does nothing (`--force`
re-enrolls with a new join code and new keys).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import ssl
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from .pinning import spki_sha256
from .security import public_key_b64

# join codes: 5 groups of 4 from an alphabet without look-alikes (no 0/O, 1/I/L, U). The first group names
# the code (the hub looks it up by it); the other 16 characters (~78 bits) are the secret.
ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
CODE_LEN, ID_LEN = 20, 4
DIRECTORY_PORT = 8100
NODE_PORT = 8000
IMAGE_VERSION = "v1.1.0"  # the published node image that has this module (.github/workflows/image.yml)
NODE_COMPOSE = Path(__file__).resolve().parents[1] / "deploy" / "node.yml"
SECRET_FILE = "/pki/network/shared_secret"  # inside the node's container (deploy/node.yml mounts ./pki at /pki)
HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


class EnrollError(Exception):
    """Why enrollment failed, in words for the person running it."""


# -- the protocol (shared with the hub, directory/enroll.py) -------------------------------------


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    return "-".join(raw[i:i + 4] for i in range(0, CODE_LEN, 4))


def normalize_code(code: str) -> str:
    raw = re.sub(r"[^A-Za-z0-9]", "", str(code)).upper()
    if len(raw) != CODE_LEN or any(c not in ALPHABET for c in raw):
        raise ValueError("a join code is 20 letters and digits in groups of four, like ABCD-EFGH-JKMN-PQRS-TVWX")
    return raw


def code_id(code: str) -> str:
    """The public part of a code: how the hub finds it (the rest is the secret)."""
    return normalize_code(code)[:ID_LEN]


def code_key(code: str) -> str:
    """The SHA-256 of the code: all the hub stores, and the HMAC key both sides prove themselves with."""
    return hashlib.sha256(b"medchain-join/v1\n" + normalize_code(code).encode()).hexdigest()


def canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def mac(key: str, context: str, obj: dict) -> str:
    """HMAC-SHA256 of `obj` (without its mac/proof field) under the code key, for `context` request|response."""
    return hmac.new(bytes.fromhex(key), f"medchain-enroll-{context}/v1\n".encode() + canonical(obj),
                    hashlib.sha256).hexdigest()


def check_mac(key: str, context: str, obj: dict, given: str | None) -> bool:
    return bool(given) and hmac.compare_digest(mac(key, context, obj), str(given))


def check_address(host: str) -> str:
    """An address peers reach this node at: an IP or a DNS name, nothing else."""
    host = str(host).strip().strip("[]")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        if not HOSTNAME.match(host):
            raise ValueError(f"not an IP address or a host name: {host!r}") from None
        return host.lower()


# -- this node's side --------------------------------------------------------------------------


@dataclass
class Keys:
    tls: ec.EllipticCurvePrivateKey
    signing: Ed25519PrivateKey
    credential: str

    @classmethod
    def generate(cls) -> "Keys":
        return cls(ec.generate_private_key(ec.SECP256R1()), Ed25519PrivateKey.generate(), secrets.token_urlsafe(24))

    def csr(self, addresses: list[str]) -> str:
        from .security import san

        req = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "medchain-enrollment")]))
               .add_extension(x509.SubjectAlternativeName([san(a) for a in addresses]), critical=False)
               .sign(self.tls, hashes.SHA256()))
        return req.public_bytes(serialization.Encoding.PEM).decode()


def split_hub(hub: str) -> tuple[str, int]:
    hub = hub.strip().removeprefix("https://").rstrip("/")
    host, sep, port = hub.rpartition(":")
    if sep and port.isdigit() and "]" not in port:
        return check_address(host), int(port)
    return check_address(hub), DIRECTORY_PORT


def post_enroll(host: str, port: int, body: dict, timeout: float) -> tuple[int, dict, str | None]:
    """POST /enroll. The hub's certificate cannot be checked yet (this node has no CA certificate): the answer's
    HMAC authenticates it instead, and the TLS key that answered is returned so the caller can pin it."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
    try:
        conn.request("POST", "/enroll", body=canonical(body), headers={"Content-Type": "application/json"})
        der = conn.sock.getpeercert(True) if conn.sock else None
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, http.client.HTTPException) as exc:
        raise EnrollError(f"cannot reach the hub at {host}:{port} ({type(exc).__name__}: {exc}). Check that the "
                          "organiser's hub is running and that this computer is on their Tailscale network.") from exc
    finally:
        conn.close()
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {"detail": raw[:200].decode(errors="replace")}
    return resp.status, data, spki_sha256(der) if der else None


def request_enrollment(hub: str, display_name: str, code: str, addresses: list[str], keys: Keys,
                       timeout: float = 30) -> dict:
    """Enroll with the hub; returns its verified answer (raises EnrollError with a plain reason)."""
    try:
        key = code_key(code)
        addresses = list(dict.fromkeys(check_address(a) for a in addresses))
    except ValueError as exc:
        raise EnrollError(str(exc)) from exc
    host, port = split_hub(hub)
    body = {"code_id": code_id(code), "display_name": display_name, "csr": keys.csr(addresses),
            "signing_public_key": public_key_b64(keys.signing),
            "credential_sha256": hashlib.sha256(keys.credential.encode()).hexdigest(),
            "addresses": addresses, "nonce": secrets.token_hex(16)}
    body["proof"] = mac(key, "request", body)
    status, answer, server_pin = post_enroll(host, port, body, timeout)
    if status != 200:
        raise EnrollError(f"the hub refused: {answer.get('detail', f'HTTP {status}')}")
    given = answer.pop("mac", None)
    if not check_mac(key, "response", answer, given) or answer.get("nonce") != body["nonce"]:
        raise EnrollError("the answer is not from the hub that issued your join code (it does not carry the code's "
                          "signature). Check the hub address with the organiser.")
    try:
        directory_cert = x509.load_pem_x509_certificate(answer["directory"]["tls_certificate"].encode())
        ca = x509.load_pem_x509_certificate(answer["ca_certificate"].encode())
        cert = x509.load_pem_x509_certificate(answer["certificate"].encode())
        cert.verify_directly_issued_by(ca)
    except (KeyError, TypeError, ValueError) as exc:
        raise EnrollError(f"the hub's answer is incomplete or malformed ({exc})") from exc
    if server_pin != spki_sha256(directory_cert):
        raise EnrollError("the TLS key that answered is not the directory's own key: something else is in the way")
    spki = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if cert.public_key().public_bytes(*spki) != keys.tls.public_key().public_bytes(*spki):
        raise EnrollError("the hub issued a certificate for a different key than this node's")
    return answer


def _pem_key(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _write(path: Path, data: str | bytes, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    if private:
        try:
            path.chmod(0o600)
        except OSError:  # a Windows folder shared into the container
            pass


def kit_env(answer: dict, hub_host: str, hub_port: int, advertise: str, port: int, image_version: str,
            image: str | None) -> str:
    host = f"[{hub_host}]" if ":" in hub_host else hub_host
    lines = [
        f"# MedChain hospital node settings for {answer['display_name']} ({answer['node_id']}), written by",
        "# `python -m node.enroll` (join-code enrollment). Read by deploy/node.yml. The keys are in pki/;",
        "# the network's shared secret is in pki/network/shared_secret. Nothing here needs editing.",
        "",
        f"NODE_ID={answer['node_id']}",
        f"NODE_PORT={port}",
        f"ADVERTISE_ADDR={advertise}:{port}",
        f"DIRECTORY_URL=https://{host}:{hub_port}",
        f"OBSERVER_URL=https://{host}:{answer['observer']['port']}",
        f"SHARED_SECRET_FILE={SECRET_FILE}",
        f"IMAGE_VERSION={image_version}",
        *([f"MEDCHAIN_IMAGE={image}"] if image else []),
        "# Rounds over a real network take longer than on one machine: give each one time.",
        "ROUND_TIMEOUT_S=15",
        "HTTP_CONNECT_TIMEOUT_S=5",
        "HTTP_READ_TIMEOUT_S=10",
    ]
    return "\n".join(lines) + "\n"


def write_kit(out: Path, answer: dict, keys: Keys, env: str) -> None:
    """The same layout as a scripts/provision.py kit, so deploy/node.yml runs it unchanged."""
    node_id = answer["node_id"]
    pki = out / "pki"
    _write(pki / node_id / "tls.key", _pem_key(keys.tls), private=True)
    _write(pki / node_id / "tls.crt", answer["certificate"])
    _write(pki / node_id / "signing.key", _pem_key(keys.signing), private=True)
    _write(pki / node_id / "signing.pub", public_key_b64(keys.signing) + "\n")
    _write(pki / node_id / "credential", keys.credential + "\n", private=True)
    _write(pki / "ca.crt", answer["ca_certificate"])
    _write(pki / "directory" / "signing.pub", answer["directory"]["signing_public_key"] + "\n")
    _write(pki / "directory" / "tls.crt", answer["directory"]["tls_certificate"])
    _write(pki / "observer" / "tls.crt", answer["observer"]["tls_certificate"])
    _write(pki / "observer" / "token", answer["observer"]["token"] + "\n", private=True)
    _write(pki / "network" / "shared_secret", answer["shared_secret"] + "\n", private=True)
    _write(out / "data" / f"{node_id}.csv", answer["data_slice"]["csv"])
    engine = {"display_name": answer["display_name"], **answer["engine"]}
    _write(out / "config" / "node.yaml", f"# {answer['display_name']} ({node_id}): the engine settings every hospital "
                                         "of this network shares, from the hub.\n" + yaml.safe_dump(engine, sort_keys=False))
    (out / "deploy").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(NODE_COMPOSE, out / "deploy" / "node.yml")
    _write(out / "README.md", textwrap.dedent(f"""\
        # MedChain hospital: {answer['display_name']} (`{node_id}`)

        Enrolled with a join code. This folder holds this hospital's private keys: don't share it.
        The node trains on `data/{node_id}.csv`, which never leaves this computer.

        ```sh
        docker compose --project-directory . -f deploy/node.yml up -d     # start (or restart)
        docker compose --project-directory . -f deploy/node.yml logs -f   # watch
        docker compose --project-directory . -f deploy/node.yml exec node python -m node.leave
        docker compose --project-directory . -f deploy/node.yml down      # stop
        ```
        """))
    _write(out / ".env", env)


def enrolled_id(out: Path) -> str | None:
    """The node id of the kit already in `out` (None if there is none)."""
    env = out / ".env"
    if not env.is_file():
        return None
    for line in env.read_text().splitlines():
        if line.startswith("NODE_ID="):
            node_id = line.partition("=")[2].strip()
            return node_id if (out / "pki" / node_id / "tls.crt").is_file() else None
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hub", required=True, help=f"the hub's directory: host[:port] (default port {DIRECTORY_PORT})")
    ap.add_argument("--name", required=True, help="this hospital's display name, shown on the dashboard")
    ap.add_argument("--code", help="the join code (default: $MEDCHAIN_JOIN_CODE)")
    ap.add_argument("--advertise", required=True, help="the address the other hospitals reach this computer at "
                                                       "(its Tailscale IP)")
    ap.add_argument("--also", action="append", default=[], help="another name for this computer for its certificate "
                                                                "(e.g. its MagicDNS name; repeatable)")
    ap.add_argument("--port", type=int, default=NODE_PORT, help=f"the port this node listens on (default {NODE_PORT})")
    ap.add_argument("--out", type=Path, default=Path("/kit"), help="where to write the kit (default /kit)")
    ap.add_argument("--image-version", default=os.environ.get("IMAGE_VERSION") or IMAGE_VERSION)
    ap.add_argument("--image", default=os.environ.get("MEDCHAIN_IMAGE") or None, help="the node image repository")
    ap.add_argument("--force", action="store_true", help="enroll again even if --out already holds an enrolled kit")
    args = ap.parse_args(argv)

    existing = enrolled_id(args.out)
    if existing and not args.force:
        print(f"already enrolled as {existing} in {args.out}; nothing to do (--force enrolls again)")
        return 0
    code = args.code or os.environ.get("MEDCHAIN_JOIN_CODE", "")
    name = " ".join(args.name.split())
    try:
        if not code:
            raise EnrollError("no join code: pass --code or set MEDCHAIN_JOIN_CODE")
        if not 2 <= len(name) <= 60:
            raise EnrollError("the hospital's name must be 2 to 60 characters")
        advertise = check_address(args.advertise)
        hub_host, hub_port = split_hub(args.hub)
        keys = Keys.generate()
        answer = request_enrollment(f"{hub_host}:{hub_port}", name, code, [advertise, *args.also], keys)
    except (EnrollError, ValueError) as exc:
        print(f"enroll: {exc}", file=sys.stderr)
        return 2
    write_kit(args.out, answer, keys, kit_env(answer, hub_host, hub_port, advertise, args.port, args.image_version,
                                              args.image))
    print(f"enrolled {answer['display_name']} as {answer['node_id']} (data: {answer['data_slice']['name']}); "
          f"kit written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
