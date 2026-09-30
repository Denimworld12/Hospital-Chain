"""Wire format for /update: the raw little-endian weight array, nothing else.

Metadata travels in headers; the body is only model parameters, so its length
is exactly the payload the node puts on the wire (HTTP headers not counted).
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


def checksum(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def encode(weights: np.ndarray, dtype: str = "float64") -> bytes:
    return np.ascontiguousarray(weights, dtype=DTYPES[dtype]).tobytes()


def decode(body: bytes, dtype: str = "float64") -> np.ndarray:
    if dtype not in DTYPES:
        raise ValueError(f"unsupported dtype {dtype!r}")
    return np.frombuffer(body, dtype=DTYPES[dtype]).astype(np.float64)
