"""Train the tabular heart-risk ensemble on the Kaggle Cardiovascular Disease dataset.

Architecture after HEALIGRAM (github.com/avdhoot-hande/HEALIGRAM), trained fresh
here: a class-balanced RandomForest and an isotonic-calibrated XGBoost, blended
0.4 * RF + 0.6 * XGB, positive at a blended probability >= 0.30.

    python -m node.assess.train_tabular                 # -> models/heart-risk.joblib
    python -m node.assess.train_tabular --out /app/models/heart-risk.joblib

The node image runs this at build time (node/Dockerfile), so the model always
matches the image's scikit-learn/xgboost versions and no pickle is committed.
This is a centrally trained inference model, not part of federated training.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

from .features import FEATURES, RAW, clean_mask, featurize

ROOT = Path(__file__).resolve().parents[2]
# Kaggle "Cardiovascular Disease dataset" (Svetlana Ulianova), 70,000 patients,
# kaggle.com/datasets/sulianova/cardiovascular-disease-dataset; Kaggle lists its
# license as "Unknown". Not Indian-sourced. Provenance in data/README.md.
DEFAULT_CSV = ROOT / "data/cardio/cardio_train.csv"
DEFAULT_OUT = ROOT / "models/heart-risk.joblib"
SOURCE = "Kaggle Cardiovascular Disease dataset (sulianova), 70k patients"

RF_WEIGHT, XGB_WEIGHT = 0.4, 0.6
THRESHOLD = 0.30


def load_csv(path: str | Path) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Raw columns (age converted from days to years) and the `cardio` target."""
    d = np.genfromtxt(path, delimiter=";", names=True)
    cols = {k: d[k].astype(float) for k in RAW}
    cols["age"] = d["age"] / 365.25
    return cols, d["cardio"].astype(int)


def blend(rf_prob, xgb_prob):
    return RF_WEIGHT * np.asarray(rf_prob) + XGB_WEIGHT * np.asarray(xgb_prob)


def train(csv_path: str | Path = DEFAULT_CSV, *, rf_estimators: int = 300, xgb_estimators: int = 600,
          max_rows: int | None = None, seed: int = 42) -> dict:
    cols, y = load_csv(csv_path)
    keep = clean_mask(cols)
    cols, y = {k: v[keep] for k, v in cols.items()}, y[keep]
    if max_rows and len(y) > max_rows:
        idx = np.random.default_rng(seed).choice(len(y), max_rows, replace=False)
        cols, y = {k: v[idx] for k, v in cols.items()}, y[idx]
    X = featurize(cols)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=seed, stratify=y)

    imputer = SimpleImputer(strategy="median").fit(X_train)
    X_train, X_test = imputer.transform(X_train), imputer.transform(X_test)
    neg, pos = np.bincount(y_train)

    rf = RandomForestClassifier(n_estimators=rf_estimators, max_depth=12, min_samples_leaf=10,
                                class_weight="balanced", random_state=seed, n_jobs=-1).fit(X_train, y_train)
    xgb = XGBClassifier(objective="binary:logistic", n_estimators=xgb_estimators, max_depth=8, learning_rate=0.03,
                        subsample=0.85, colsample_bytree=0.85, scale_pos_weight=neg / pos, eval_metric="auc",
                        random_state=seed, n_jobs=-1, verbosity=0)
    xgb_cal = CalibratedClassifierCV(xgb, method="isotonic", cv=3).fit(X_train, y_train)

    prob = blend(rf.predict_proba(X_test)[:, 1], xgb_cal.predict_proba(X_test)[:, 1])
    metrics = {
        "accuracy": round(float(accuracy_score(y_test, prob >= THRESHOLD)), 4),
        "roc_auc": round(float(roc_auc_score(y_test, prob)), 4),
        "n_train": int(len(y_train)),
        "n_test": int(len(y_test)),
    }
    return {
        "random_forest": rf,
        "xgb_calibrated": xgb_cal,
        "imputer": imputer,
        "features": list(FEATURES),
        "threshold": THRESHOLD,
        "weights": {"random_forest": RF_WEIGHT, "xgboost": XGB_WEIGHT},
        "metrics": metrics,
        "source": SOURCE,
        "source_sha256": hashlib.sha256(Path(csv_path).read_bytes()).hexdigest(),
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Train the tabular heart-risk ensemble.")
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    t0 = time.time()
    bundle = train(args.csv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, args.out, compress=3)
    m = bundle["metrics"]
    print(f"trained on {m['n_train']} rows in {time.time() - t0:.0f}s: accuracy {m['accuracy']:.4f} "
          f"(threshold {THRESHOLD}), ROC-AUC {m['roc_auc']:.4f}, held-out n={m['n_test']} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
