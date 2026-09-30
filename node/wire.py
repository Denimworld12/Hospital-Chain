"""Wire format for /update: the engine's packet bytes, nothing else.

The gossip baseline's body is the raw little-endian float64 weight array; the
ADMM engines' body is a quantized packet (node/qgadmm/quantizer.py). Metadata
travels in headers; the body is only model parameters, so its length is exactly
the payload the node puts on the wire (HTTP headers not counted).
"""

from __future__ import annotations

import hashlib

import numpy as np

DTYPES = {"float64": "<f8", "float32": "<f4"}

H_RUN = "X-MedChain-Run"
H_ROUND = "X-MedChain-Round"
H_SENDER = "X-MedChain-Sender"
H_CHECKSUM = "X-MedChain-Checksum"
H_DTYPE = "X-MedChain-Dtype"
H_SECRET = "X-MedChain-Secret"
# ADMM engines only
H_SEQ = "X-MedChain-Seq"  # packet sequence number of the sender's theta_hat
H_BASE = "X-MedChain-Base"  # fingerprint of the snapshot the packet was built on
H_PRODUCES = "X-MedChain-Produces"  # fingerprint of the snapshot it produces
H_VIEW = "X-MedChain-Residuals"  # JSON: the sender's recent per-edge residuals (convergence detection)
H_STOP = "X-MedChain-Stop"  # agreed last round once consensus is detected
H_MIRROR_SEQ = "X-MedChain-Mirror-Seq"  # /snapshot: seq of the server's mirror of the requester


def checksum(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def encode(weights: np.ndarray, dtype: str = "float64") -> bytes:
    return np.ascontiguousarray(weights, dtype=DTYPES[dtype]).tobytes()


def decode(body: bytes, dtype: str = "float64") -> np.ndarray:
    if dtype not in DTYPES:
        raise ValueError(f"unsupported dtype {dtype!r}")
    return np.frombuffer(body, dtype=DTYPES[dtype]).astype(np.float64)
