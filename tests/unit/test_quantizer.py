"""Q-GADMM stochastic quantizer (node/qgadmm/quantizer.py)."""

import math

import numpy as np
import pytest

from node.qgadmm import quantizer as qz

TRIALS = 10_000


def fixed_delta(d: int, seed: int = 3) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 0.7, d)


@pytest.mark.parametrize("b", [2, 4, 8])
def test_quantizer_is_unbiased(b):
    """Average 10,000 quantize -> reconstruct trials of one fixed delta; the mean error must vanish.

    The error of one coordinate is Delta*(Bernoulli(p) - p), so its 10,000-trial mean has standard
    deviation sigma_i = Delta*sqrt(p_i(1-p_i))/100. At b=8 that is below R/25,500 and the
    per-coordinate bound mean error < 1e-3*R holds for every coordinate. At b=2 (Delta = 2R/3) sigma_i
    reaches R/300, so a per-coordinate 1e-3*R bound would fail by chance for an unbiased quantizer; there
    the per-coordinate check is 5 sigma_i, and the 1e-3*R bound is applied to the error averaged
    over all coordinates (and still per coordinate at b=8, see above).
    """
    d = 1000
    delta = fixed_delta(d)
    base = np.zeros(d)
    rng = np.random.default_rng(11)
    total = np.zeros(d)
    R = None
    for _ in range(TRIALS):
        pkt = qz.quantize(delta, base, b, rng)
        R = pkt.R
        total += qz.reconstruct(base, pkt)
    err = total / TRIALS - delta
    step = qz.step(b, R)
    c = (delta + R) / step
    p = c - np.floor(c)
    sigma = step * np.sqrt(p * (1 - p)) / math.sqrt(TRIALS)
    assert np.all(np.abs(err) <= 5 * sigma + 1e-12), "a coordinate's mean error is beyond 5 sigma: biased"
    assert abs(err.mean()) < 1e-3 * R
    if b == 8:
        assert np.max(np.abs(err)) < 1e-3 * R


@pytest.mark.parametrize("b", [2, 4, 8])
def test_rungs_and_range(b):
    delta = fixed_delta(64)
    pkt = qz.quantize(delta, np.zeros(64), b, np.random.default_rng(0))
    assert pkt.R == np.float32(pkt.R) and pkt.R >= np.max(np.abs(delta))  # float32 on the wire, never clips
    assert pkt.q.dtype == np.uint8 and pkt.q.min() >= 0 and pkt.q.max() <= 2**b - 1
    rebuilt = qz.reconstruct(np.zeros(64), pkt)
    # every coordinate lands on one of the two rungs around it: -R + q*Delta
    assert np.all(np.abs(rebuilt - delta) <= qz.step(b, pkt.R) + 1e-12)


@pytest.mark.parametrize("b,d", [(2, 16), (2, 1000), (4, 16), (8, 16), (4, 7), (2, 5)])
def test_packbits_wire_size_and_roundtrip(b, d):
    theta = fixed_delta(d, seed=d)
    pkt = qz.quantize(theta, np.zeros(d), b, np.random.default_rng(1))
    body = qz.encode(pkt)
    assert len(body) == math.ceil(b * d / 8) + 8 == qz.payload_bytes(b, d)
    back = qz.decode(body, d)
    assert back.b == b and back.R == pkt.R
    np.testing.assert_array_equal(back.q, pkt.q)


def test_d16_b2_packet_is_12_bytes_vs_64_at_32_bit():
    """The honest per-packet ratio for this project's model (d = 15 features + bias)."""
    assert qz.payload_bytes(2, 16) == 12
    assert 4 * 16 / qz.payload_bytes(2, 16) == pytest.approx(5.33, abs=0.01)
    assert 4 * 16 / (2 * 16 / 8) == 16  # coordinates only, header excluded
    assert 4 * 100_000 / qz.payload_bytes(2, 100_000) == pytest.approx(16, rel=1e-3)  # the asymptote


def test_no_change_flag():
    theta = np.arange(5.0)
    pkt = qz.quantize(theta, theta.copy(), 2, np.random.default_rng(0))
    assert pkt.no_change and pkt.q is None
    body = qz.encode(pkt)
    assert len(body) == 8 == qz.payload_bytes(2, 5, no_change=True)
    np.testing.assert_array_equal(qz.reconstruct(theta, qz.decode(body, 5)), theta)


def test_full_precision_path_sends_raw_float32_delta():
    d = 16
    theta = fixed_delta(d)
    pkt = qz.quantize(theta, np.zeros(d), qz.FULL_PRECISION_BITS, np.random.default_rng(0))
    body = qz.encode(pkt)
    assert len(body) == 4 * d + 8
    np.testing.assert_array_equal(qz.reconstruct(np.zeros(d), qz.decode(body, d)), theta.astype(np.float32))


def test_seeded_generator_makes_quantization_reproducible():
    delta = fixed_delta(32)
    a = qz.quantize(delta, np.zeros(32), 2, np.random.default_rng(42))
    b = qz.quantize(delta, np.zeros(32), 2, np.random.default_rng(42))
    np.testing.assert_array_equal(a.q, b.q)


def test_decode_rejects_malformed_packets():
    with pytest.raises(ValueError):
        qz.decode(b"\x00" * 3, 16)
    good = qz.encode(qz.quantize(np.ones(16), np.zeros(16), 2, np.random.default_rng(0)))
    with pytest.raises(ValueError):
        qz.decode(good[:-1], 16)
    with pytest.raises(ValueError):
        qz.decode(qz.HEADER.pack(9, 1.0) + good[8:], 16)
    with pytest.raises(ValueError):
        qz.decode(qz.HEADER.pack(2, float("nan")) + good[8:], 16)
