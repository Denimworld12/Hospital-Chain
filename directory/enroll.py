"""The hub's side of join-code enrollment (the node's side and the protocol: node/enroll.py).

The organiser creates a join code from the dashboard's admin page (the directory's admin
API, directory/app.py): single use, expiring (30 minutes by default), stored only as its
SHA-256. A hospital POSTs /enroll with the code's id, a proof that it holds the code, its
display name, a certificate signing request for its own TLS key, the public half of its
update-signing key and the hash of a directory credential it made itself. Its private keys
never leave it.

If the code is valid, the directory marks it used (atomically: one winner), issues the
node a certificate from the network CA, generates its node id and an observer ingest token
for it alone, and answers with everything a kit would hold except private keys. Every
attempt, accepted or not, is written to the audit log.

Names: the display name is free text, chosen by the person enrolling, and it is what the
dashboard, the logs and the audit trail show. No two hospitals may share one (compared
case- and space-insensitively). The node id is separate and stable: a slug of the name
plus random hex (`st-marys-leeds-3f9a`), generated here and never chosen by the node. TLS
pins, signatures and chain placement are keyed on the id and its keys, never the name.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from node import enroll as protocol
from node import pinning, security
from node.config import engine_settings

from .store import Store

DEFAULT_TTL_MINUTES = 30
MAX_TTL_MINUTES = 24 * 60
NAME = re.compile(r"^[^\x00-\x1f\x7f]{2,60}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class EnrollmentConfig:
    """The `enrollment:` block of the directory's config. Without one, /enroll answers 503."""

    ca_cert: str
    ca_key: str
    observer_cert: str  # the observer's TLS certificate: enrolled nodes pin its key
    slices_dir: str  # the data slices a code may hand out (*.csv)
    node_template: str  # a node config: its engine settings are the ones every hospital of this network shares
    observer_port: int = 8200
    code_ttl_minutes: int = DEFAULT_TTL_MINUTES
    shared_secret_file: str | None = None  # the network secret; default $MEDCHAIN_SHARED_SECRET

    @classmethod
    def from_yaml(cls, raw: dict | None) -> "EnrollmentConfig | None":
        if not raw:
            return None
        missing = [k for k in ("ca_cert", "ca_key", "observer_cert", "slices_dir", "node_template") if not raw.get(k)]
        if missing:
            raise ValueError(f"directory enrollment block: missing {missing}")
        return cls(**{k: raw[k] for k in cls.__dataclass_fields__ if k in raw})

    @property
    def shared_secret(self) -> str:
        secret = (Path(self.shared_secret_file).read_text().strip() if self.shared_secret_file
                  else os.environ.get("MEDCHAIN_SHARED_SECRET", ""))
        if not secret:
            raise EnrollmentError(503, "the hub has no network secret to hand out (set MEDCHAIN_SHARED_SECRET for "
                                       "the directory)")
        return secret


def name_key(name: str) -> str:
    """What two display names are compared by: "St  Mary's" and "st mary's" are the same name."""
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def slug(name: str, size: int = 20) -> str:
    """The readable part of a node id: lowercase ASCII letters, digits and single dashes."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:size].strip("-") or "hospital"


class EnrollmentError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class Enroller:
    def __init__(self, cfg: EnrollmentConfig, store: Store, *, signing_key, directory_cert: str,
                 configured_ids: Callable[[], list[str]], member_names: Callable[[], dict[str, str]]):
        self.cfg, self.store, self.signing_key = cfg, store, signing_key
        self.directory_cert = directory_cert
        self.configured_ids = configured_ids  # the kit hospitals (hospitals.yaml), whose ids are taken
        self.member_names = member_names  # {node id: display name} of the hospitals the directory has registered

    # -- join codes -----------------------------------------------------------------------------
    def slices(self) -> list[str]:
        return sorted(p.name for p in Path(self.cfg.slices_dir).glob("*.csv"))

    def create_code(self, created_by: str | None, ttl_minutes: int | None = None,
                    data_slice: str | None = None) -> dict:
        """A new single-use code. The full code is only ever in this answer: the store keeps its hash."""
        ttl = int(ttl_minutes or self.cfg.code_ttl_minutes)
        if not 1 <= ttl <= MAX_TTL_MINUTES:
            raise EnrollmentError(400, f"a join code lasts 1 to {MAX_TTL_MINUTES} minutes")
        if data_slice and data_slice not in self.slices():
            raise EnrollmentError(400, f"no data slice {data_slice!r} on the hub (have {self.slices()})")
        expires_at = time.time() + 60 * ttl
        for _ in range(20):  # the id is the first group: retry on the rare collision
            code = protocol.new_code()
            if self.store.add_code(protocol.code_id(code), protocol.code_key(code), created_by, expires_at,
                                   data_slice or None):
                return {"code": code, "code_id": protocol.code_id(code), "expires_at": expires_at,
                        "data_slice": data_slice or None}
        raise EnrollmentError(503, "could not find a free join code id")

    @staticmethod
    def state(row: dict, now: float | None = None) -> str:
        now = time.time() if now is None else now
        if row["used_at"]:
            return "used"
        if row["revoked_at"]:
            return "revoked"
        return "expired" if row["expires_at"] <= now else "active"

    # -- enrollment -------------------------------------------------------------------------------
    def new_node_id(self, name: str) -> str:
        taken = {*self.configured_ids(), *(r["node_id"] for r in self.store.enrolled()), "directory", "observer"}
        while True:
            node_id = f"{slug(name)}-{secrets.token_hex(2)}"
            if node_id not in taken:
                return node_id

    def name_taken_by(self, name: str) -> str | None:
        """The node id of the hospital that already goes by this display name (None if it is free)."""
        names = {**self.member_names(), **{r["node_id"]: r["display_name"] for r in self.store.enrolled()}}
        return next((i for i, n in names.items() if name_key(n or i) == name_key(name)), None)

    def pick_slice(self, wanted: str | None) -> str:
        """The code's slice, else the one the fewest enrolled hospitals have (kit hospitals' own slices last)."""
        names = self.slices()
        if wanted:
            if wanted not in names:
                raise EnrollmentError(503, f"the join code's data slice {wanted} is not on the hub")
            return wanted
        if not names:
            raise EnrollmentError(503, "the hub has no data slices to hand out")
        used = [r["data_slice"] for r in self.store.enrolled()]
        configured = {f"{i}.csv" for i in self.configured_ids()}
        return min(names, key=lambda n: (used.count(n), n in configured, n))

    def check(self, req: dict) -> tuple[dict, str, x509.CertificateSigningRequest, list[str]]:
        """Validate the request's shape and the join code. Returns (the code's row and key, the CSR, the addresses)."""
        found = self.store.code(str(req.get("code_id", "")))
        if found is None:
            raise EnrollmentError(403, "unknown join code: check it with the organiser")
        row, key = found
        state = self.state(row)
        if state != "active":
            raise EnrollmentError(403, f"this join code has {'been ' if state != 'expired' else ''}{state}: "
                                       "ask the organiser for a new one")
        proof = req.get("proof")
        if not protocol.check_mac(key, "request", {k: v for k, v in req.items() if k != "proof"}, proof):
            revoked = self.store.code_failure(row["code_id"])
            raise EnrollmentError(403, "the join code is wrong (check every character)"
                                       + ("; it was revoked after too many wrong attempts" if revoked else ""))
        try:
            csr = x509.load_pem_x509_csr(str(req.get("csr", "")).encode())
            if not csr.is_signature_valid:
                raise ValueError("the certificate request's signature does not verify")
            if not isinstance(csr.public_key(), ec.EllipticCurvePublicKey):
                raise ValueError("the TLS key must be an EC key")
            security.parse_public_key(str(req.get("signing_public_key", "")))
            if not HEX64.match(str(req.get("credential_sha256", ""))):
                raise ValueError("credential_sha256 must be 64 lowercase hex digits")
            addresses = [protocol.check_address(a) for a in req.get("addresses") or []]
            if not 1 <= len(addresses) <= 8:
                raise ValueError("give 1 to 8 addresses this node is reached at")
            if not isinstance(req.get("nonce"), str) or not 16 <= len(req["nonce"]) <= 128:
                raise ValueError("nonce must be 16 to 128 characters")
        except (ValueError, TypeError) as exc:
            raise EnrollmentError(400, f"malformed enrollment: {exc}") from exc
        name = " ".join(str(req.get("display_name", "")).split())
        if not NAME.match(name):
            raise EnrollmentError(400, "the display name must be 2 to 60 characters")
        taken = self.name_taken_by(name)
        if taken:
            raise EnrollmentError(409, f"another hospital ({taken}) is already called {name!r}: choose another name "
                                       "(your join code is still valid)")
        return row, key, csr, addresses

    def enroll(self, req: dict) -> tuple[dict, dict]:
        """(the answer for the node, the hospital's enrolled record). Raises EnrollmentError."""
        code_row, key, csr, addresses = self.check(req)
        name = " ".join(str(req["display_name"]).split())
        data_slice = self.pick_slice(code_row["data_slice"])
        secret = self.cfg.shared_secret
        ca_cert = x509.load_pem_x509_certificate(Path(self.cfg.ca_cert).read_bytes())
        ca_key = serialization.load_pem_private_key(Path(self.cfg.ca_key).read_bytes(), password=None)
        node_id = self.new_node_id(name)
        if not self.store.consume_code(code_row["code_id"], node_id):  # someone else used it a moment ago
            raise EnrollmentError(403, "this join code has just been used: ask the organiser for a new one")
        cert = security.issue_certificate(ca_cert, ca_key, csr.public_key(), node_id,
                                          [*addresses, node_id, "localhost", "127.0.0.1"])
        token = secrets.token_urlsafe(32)
        record = {"node_id": node_id, "display_name": name, "name_key": name_key(name),
                  "credential_sha256": req["credential_sha256"],
                  "signing_public_key": req["signing_public_key"], "tls_pin": pinning.spki_sha256(cert),
                  "observer_token_sha256": hashlib.sha256(token.encode()).hexdigest(), "data_slice": data_slice,
                  "code_id": code_row["code_id"], "enrolled_at": time.time()}
        self.store.add_enrolled(record)
        engine = engine_settings(yaml.safe_load(Path(self.cfg.node_template).read_text()) or {})
        engine.pop("display_name", None)
        answer = {
            "node_id": node_id,
            "display_name": name,
            "certificate": _pem(cert),
            "ca_certificate": _pem(ca_cert),
            "directory": {"signing_public_key": security.public_key_b64(self.signing_key),
                          "tls_certificate": Path(self.directory_cert).read_text()},
            "observer": {"port": self.cfg.observer_port, "tls_certificate": Path(self.cfg.observer_cert).read_text(),
                         "token": token},
            "shared_secret": secret,
            "engine": engine,
            "data_slice": {"name": data_slice, "csv": (Path(self.cfg.slices_dir) / data_slice).read_text()},
            "nonce": req.get("nonce"),
        }
        answer["mac"] = protocol.mac(key, "response", answer)
        return answer, record
