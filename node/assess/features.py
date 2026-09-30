"""Cleaning and feature engineering for the tabular heart-risk ensemble.

One implementation shared by training (`train_tabular.py`) and serving
(`tabular.py`), so the two can never drift apart. The feature set follows the
HEALIGRAM approach: raw exam fields, age in years with age-group buckets, BMI
with BMI-category buckets, pulse pressure, hypertension stage, and two
interaction terms.

Inputs use the Kaggle Cardiovascular Disease dataset's coding (see
`data/README.md`): gender 1 = female, 2 = male; cholesterol and gluc 1 = normal,
2 = above normal, 3 = well above normal; smoke/alco/active 0 or 1.
"""

from __future__ import annotations

import numpy as np

RAW = ("gender", "height", "weight", "ap_hi", "ap_lo", "cholesterol", "gluc", "smoke", "alco", "active")

# Right-inclusive buckets like pandas.cut; the first bucket is the dropped baseline.
AGE_EDGES = (0, 40, 50, 60, 70, 120)
AGE_LABELS = ("<40", "40-50", "50-60", "60-70", "70+")
BMI_EDGES = (0, 18.5, 25, 30, 100)
BMI_LABELS = ("underweight", "normal", "overweight", "obese")

FEATURES = (
    list(RAW)
    + ["age"]
    + [f"age_group_{v}" for v in AGE_LABELS[1:]]
    + ["bmi"]
    + [f"bmi_category_{v}" for v in BMI_LABELS[1:]]
    + ["pulse_pressure", "hypertension_stage", "age_bmi_interaction", "bp_pulse_interaction"]
)

# Training range for blood pressure; inputs are clipped into it before scoring.
AP_HI_RANGE = (90, 200)
AP_LO_RANGE = (60, 120)
HEIGHT_RANGE = (120, 220)
WEIGHT_RANGE = (30, 200)


def bmi(height_cm, weight_kg):
    return np.asarray(weight_kg, dtype=float) / (np.asarray(height_cm, dtype=float) / 100.0) ** 2


def hypertension_stage(ap_hi, ap_lo):
    """0 = below stage 1, 1 = >= 140/90, 2 = >= 160/100 (either reading)."""
    ap_hi, ap_lo = np.asarray(ap_hi, dtype=float), np.asarray(ap_lo, dtype=float)
    return np.where((ap_hi >= 160) | (ap_lo >= 100), 2, np.where((ap_hi >= 140) | (ap_lo >= 90), 1, 0))


def _one_hot(values, edges, n_labels):
    """Dummies for buckets 1..n-1 (bucket 0 dropped); values outside every bucket get all zeros."""
    idx = np.searchsorted(np.asarray(edges, dtype=float), values, side="left") - 1
    return np.stack([(idx == k) for k in range(1, n_labels)], axis=1).astype(float)


def clean_mask(cols: dict[str, np.ndarray]) -> np.ndarray:
    """Rows kept for training: diastolic not above systolic, plausible height and weight."""
    return (
        (cols["ap_hi"] >= cols["ap_lo"])
        & (cols["height"] >= HEIGHT_RANGE[0]) & (cols["height"] <= HEIGHT_RANGE[1])
        & (cols["weight"] >= WEIGHT_RANGE[0]) & (cols["weight"] <= WEIGHT_RANGE[1])
    )


def featurize(cols: dict[str, np.ndarray]) -> np.ndarray:
    """Feature matrix in FEATURES order from raw columns (`age` in years)."""
    c = {k: np.asarray(cols[k], dtype=float) for k in (*RAW, "age")}
    ap_hi = np.clip(c["ap_hi"], *AP_HI_RANGE)
    ap_lo = np.clip(c["ap_lo"], *AP_LO_RANGE)
    age = c["age"]
    b = bmi(c["height"], c["weight"])
    pulse = ap_hi - ap_lo
    raw = [ap_hi if k == "ap_hi" else ap_lo if k == "ap_lo" else c[k] for k in RAW]
    return np.column_stack([
        *raw,
        age,
        _one_hot(age, AGE_EDGES, len(AGE_LABELS)),
        b,
        _one_hot(b, BMI_EDGES, len(BMI_LABELS)),
        pulse,
        hypertension_stage(ap_hi, ap_lo),
        age * b,
        pulse * ap_hi,
    ])
