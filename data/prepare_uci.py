#!/usr/bin/env python3
"""Download the UCI Heart Disease data and write three non-IID hospital slices.

The UCI collection is itself four hospital datasets, so the non-IID split is the
real one: each MedChain hospital gets one source institution's patients, with
that institution's own case mix, disease prevalence, and missing-value pattern.

    hospital-a  <- Cleveland Clinic Foundation           (303 patients)
    hospital-b  <- Hungarian Institute of Cardiology      (294 patients)
    hospital-c  <- V.A. Medical Center, Long Beach        (200 patients)

Columns kept are the ten that all three sources record reasonably well; `slope`,
`ca` and `thal` are mostly missing outside Cleveland and are dropped. Remaining
missing values (`?`, and physiologically impossible zeros in `chol`/`trestbps`)
are imputed with the median *of that hospital's own slice*, so no statistic ever
crosses hospital boundaries. The target is `num > 0` (any angiographic disease).

Stdlib only.

    python3 data/prepare_uci.py            # writes data/slices/*.csv + manifest.json

Source: Janosi, Steinbrunn, Pfisterer, Detrano (1989). Heart Disease. UCI Machine
Learning Repository. https://doi.org/10.24432/C52P4X (CC BY 4.0).
"""

from __future__ import annotations

import csv
import io
import json
import statistics
import sys
import urllib.request
from pathlib import Path

BASE_URL = "https://archive.ics.uci.edu/ml/machine-learning-databases/heart-disease/"
SOURCES = {
    "hospital-a": ("processed.cleveland.data", "Cleveland Clinic Foundation"),
    "hospital-b": ("processed.hungarian.data", "Hungarian Institute of Cardiology, Budapest"),
    "hospital-c": ("processed.va.data", "V.A. Medical Center, Long Beach"),
}
RAW_COLUMNS = ["age", "sex", "cp", "trestbps", "chol", "fbs", "restecg", "thalach", "exang", "oldpeak", "slope", "ca", "thal", "num"]
KEEP = ["age", "sex", "cp", "trestbps", "chol", "fbs", "restecg", "thalach", "exang", "oldpeak"]
ZERO_IS_MISSING = {"chol", "trestbps"}
INTEGER = {"age", "sex", "cp", "fbs", "restecg", "exang"}

OUT_DIR = Path(__file__).resolve().parent / "slices"


def fetch(name: str) -> str:
    with urllib.request.urlopen(BASE_URL + name, timeout=60) as resp:
        return resp.read().decode("utf-8")


def parse(text: str) -> list[dict]:
    rows = []
    for rec in csv.reader(io.StringIO(text)):
        if len(rec) != len(RAW_COLUMNS):
            continue
        rows.append(dict(zip(RAW_COLUMNS, (v.strip() for v in rec))))
    return rows


def clean(rows: list[dict]) -> list[dict]:
    def value(row, col):
        v = row[col]
        if v in ("?", ""):
            return None
        f = float(v)
        if col in ZERO_IS_MISSING and f == 0:
            return None
        return f

    medians = {}
    for col in KEEP:
        present = [x for x in (value(r, col) for r in rows) if x is not None]
        medians[col] = statistics.median(present)

    out = []
    for r in rows:
        rec = {}
        for col in KEEP:
            v = value(r, col)
            v = medians[col] if v is None else v
            rec[col] = int(round(v)) if col in INTEGER else round(v, 2)
        rec["target"] = 1 if float(r["num"]) > 0 else 0
        out.append(rec)
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {"source": "UCI Heart Disease (CC BY 4.0), https://doi.org/10.24432/C52P4X", "features": KEEP, "target": "target", "slices": {}}
    for node_id, (fname, institution) in SOURCES.items():
        rows = clean(parse(fetch(fname)))
        path = OUT_DIR / f"{node_id}.csv"
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=KEEP + ["target"])
            writer.writeheader()
            writer.writerows(rows)
        positives = sum(r["target"] for r in rows)
        manifest["slices"][node_id] = {
            "file": path.name,
            "institution": institution,
            "patients": len(rows),
            "positive_rate": round(positives / len(rows), 3),
        }
        print(f"{node_id}: {len(rows)} patients from {institution} ({positives / len(rows):.0%} positive) -> {path}")
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
