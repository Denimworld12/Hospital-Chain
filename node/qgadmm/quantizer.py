"""Stochastic quantizer and reconstruction for Q-GADMM (Elgabli et al., 2021).

A node never sends its weights. It sends the difference between its new weights
and the quantized snapshot its neighbours already hold, quantized to b bits per
coordinate:

    delta = theta_new - theta_hat_old
    R     = max_i |delta_i|                    (sent as float32, rounded up)
    Delta = 2R / (2^b - 1)                     (grid step; rungs -R + q*Delta)
    c_i   = (delta_i + R) / Delta
    q_i   = floor(c_i) + 1 with probability c_i - floor(c_i), else floor(c_i)

and both ends rebuild the same snapshot (Eq. 13):

    theta_hat_new = theta_hat_old + Delta*q - R*1

`reconstruct` is the only place that formula lives; the sender calls it on its
own theta_hat and the receiver on its mirror, with the same float64 inputs in the
same order, so the two results are bit-identical.

Wire body: 8-byte header (uint32 b, float32 R) + q packed at b bits/coordinate
(numpy packbits), i.e. ceil(b*d/8) + 8 bytes. R = 0 is the "no change" flag and
the body is the header alone. b = 32 is the full-precision GADMM path: no
quantization, the body is the header plus the raw float32 delta.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

import numpy as np

HEADER = struct.Struct("<If")  # b, R
HEADER_BYTES = HEADER.size  # 8
QUANTIZED_BITS = (1, 2, 3, 4, 5, 6, 7, 8)
FULL_PRECISION_BITS = 32  # the GADMM (b = infinity) code path: raw float32 delta


@dataclass(frozen=True)
class Packet:
    b: int
    R: float  # exactly representable as float32
    q: np.ndarray | None  # uint8 rungs (b <= 8), float32 delta (b = 32), None when R == 0

    @property
    def no_change(self) -> bool:
        return self.R == 0.0


def payload_bytes(b: int, d: int, *, no_change: bool = False) -> int:
    """Bytes on the wire for one packet: ceil(b*d/8) + 8 (header only when nothing changed)."""
    return HEADER_BYTES if no_change else math.ceil(b * d / 8) + HEADER_BYTES


def _range_f32(delta: np.ndarray) -> float:
    """R = ||delta||_inf as a float32 that is >= the true max, so every coordinate is on the grid."""
    r = float(np.max(np.abs(delta))) if delta.size else 0.0
    r32 = np.float32(r)
    if float(r32) < r:
        r32 = np.nextafter(r32, np.float32(np.inf))
    return float(r32)


def step(b: int, R: float) -> float:
    return 2.0 * R / (2**b - 1)


def quantize(theta_new: np.ndarray, theta_hat_old: np.ndarray, b: int, rng: np.random.Generator) -> Packet:
    delta = np.asarray(theta_new, dtype=np.float64) - np.asarray(theta_hat_old, dtype=np.float64)
    if b == FULL_PRECISION_BITS:
        d32 = delta.astype(np.float32)
        return Packet(b=b, R=_range_f32(delta), q=d32) if np.any(d32) else Packet(b=b, R=0.0, q=None)
    if b not in QUANTIZED_BITS:
        raise ValueError(f"bits must be one of {QUANTIZED_BITS} (or {FULL_PRECISION_BITS} for full precision), got {b}")
    R = _range_f32(delta)
    if R == 0.0:
        return Packet(b=b, R=0.0, q=None)
    levels = 2**b - 1
    c = (delta + R) / step(b, R)
    lower = np.floor(c)
    up = rng.random(delta.shape) < (c - lower)
    q = np.clip(lower + up, 0, levels).astype(np.uint8)
    return Packet(b=b, R=R, q=q)


def reconstruct(theta_hat_old: np.ndarray, pkt: Packet) -> np.ndarray:
    """Eq. 13: theta_hat_old + Delta*q - R (or + raw delta on the full-precision path)."""
    old = np.asarray(theta_hat_old, dtype=np.float64)
    if pkt.no_change:
        return old.copy()
    if pkt.b == FULL_PRECISION_BITS:
        return old + pkt.q.astype(np.float64)
    return old + step(pkt.b, pkt.R) * pkt.q.astype(np.float64) - pkt.R


def encode(pkt: Packet) -> bytes:
    head = HEADER.pack(pkt.b, pkt.R)
    if pkt.no_change:
        return head
    if pkt.b == FULL_PRECISION_BITS:
        return head + np.ascontiguousarray(pkt.q, dtype="<f4").tobytes()
    bits = ((pkt.q[:, None] >> np.arange(pkt.b, dtype=np.uint8)) & 1).astype(np.uint8)
    return head + np.packbits(bits.ravel(), bitorder="little").tobytes()


def decode(body: bytes, d: int) -> Packet:
    if len(body) < HEADER_BYTES:
        raise ValueError(f"packet too short ({len(body)} bytes)")
    b, R = HEADER.unpack_from(body)
    rest = body[HEADER_BYTES:]
    if b != FULL_PRECISION_BITS and b not in QUANTIZED_BITS:
        raise ValueError(f"packet has unsupported bit width {b}")
    if R == 0.0:
        if rest:
            raise ValueError("no-change packet must carry no payload")
        return Packet(b=b, R=0.0, q=None)
    if not (math.isfinite(R) and R > 0):
        raise ValueError(f"packet has invalid range R={R}")
    expected = payload_bytes(b, d) - HEADER_BYTES
    if len(rest) != expected:
        raise ValueError(f"packet body is {len(rest)} bytes, expected {expected} for b={b}, d={d}")
    if b == FULL_PRECISION_BITS:
        return Packet(b=b, R=R, q=np.frombuffer(rest, dtype="<f4").copy())
    bits = np.unpackbits(np.frombuffer(rest, dtype=np.uint8), count=b * d, bitorder="little").reshape(d, b)
    q = (bits.astype(np.uint8) << np.arange(b, dtype=np.uint8)).sum(axis=1).astype(np.uint8)
    return Packet(b=b, R=R, q=q)
