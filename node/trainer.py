"""Local logistic regression on one hospital's UCI heart-disease slice (scikit-learn).

Features are scaled with fixed clinical ranges rather than statistics of the
data, so every hospital maps features identically without sharing anything
about its patients, and averaged weights stay meaningful across hospitals.
"""

from __future__ import annotations

import csv
import warnings
from pathlib import Path

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import log_loss
from sklearn.model_selection import train_test_split

CONTINUOUS = {  # fixed, published physiological ranges -> scaled to [-1, 1]
    "age": (20.0, 80.0),
    "trestbps": (80.0, 200.0),
    "chol": (100.0, 600.0),
    "thalach": (60.0, 210.0),
    "oldpeak": (-3.0, 7.0),
}
BINARY = ("sex", "fbs", "exang")
CATEGORICAL = {"cp": (1, 2, 3, 4), "restecg": (0, 1, 2)}
TARGET = "target"

FEATURE_NAMES = (
    list(CONTINUOUS)
    + list(BINARY)
    + [f"{k}={v}" for k, vals in CATEGORICAL.items() for v in vals]
)
N_FEATURES = len(FEATURE_NAMES)
N_PARAMS = N_FEATURES + 1  # + intercept


def featurize(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    X = np.zeros((len(rows), N_FEATURES))
    y = np.zeros(len(rows), dtype=int)
    for i, r in enumerate(rows):
        j = 0
        for col, (lo, hi) in CONTINUOUS.items():
            X[i, j] = 2.0 * (float(r[col]) - lo) / (hi - lo) - 1.0
            j += 1
        for col in BINARY:
            X[i, j] = float(r[col])
            j += 1
        for col, vals in CATEGORICAL.items():
            v = int(float(r[col]))
            for cat in vals:
                X[i, j] = 1.0 if v == cat else 0.0
                j += 1
        y[i] = int(float(r[TARGET]))
    return X, y


def load_slice(path: str | Path) -> list[dict]:
    with Path(path).open(newline="") as fh:
        return list(csv.DictReader(fh))


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class LocalTrainer:
    """Holds this hospital's data and a flat weight vector [coef..., intercept]."""

    def __init__(self, data_path: str | Path, *, test_fraction: float, seed: int, learning_rate: float, local_epochs: int):
        rows = load_slice(data_path)
        X, y = featurize(rows)
        self.n_patients = len(rows)
        self.X_train, self.X_test, self.y_train, self.y_test = train_test_split(
            X, y, test_size=test_fraction, random_state=seed, stratify=y
        )
        self.seed = seed
        self.learning_rate = learning_rate
        self.local_epochs = local_epochs
        self.weights = np.zeros(N_PARAMS)

    def reset(self) -> None:
        self.weights = np.zeros(N_PARAMS)

    def fit_round(self, round_num: int) -> float:
        """A few epochs of SGD on local data, warm-started from the current weights."""
        clf = SGDClassifier(
            loss="log_loss",
            penalty="l2",
            alpha=1e-4,
            learning_rate="constant",
            eta0=self.learning_rate,
            max_iter=self.local_epochs,
            tol=None,
            shuffle=True,
            random_state=self.seed + round_num,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ConvergenceWarning)
            clf.fit(
                self.X_train,
                self.y_train,
                coef_init=self.weights[:-1].reshape(1, -1),
                intercept_init=self.weights[-1:],
            )
        self.weights = np.concatenate([clf.coef_.ravel(), clf.intercept_]).astype(np.float64)
        return self.loss(self.X_train, self.y_train)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        # errstate: silences spurious matmul FP warnings from macOS Accelerate BLAS;
        # logits are clipped in _sigmoid, so real overflow cannot occur.
        with np.errstate(all="ignore"):
            return _sigmoid(X @ self.weights[:-1] + self.weights[-1])

    def loss(self, X: np.ndarray, y: np.ndarray) -> float:
        return float(log_loss(y, self.predict_proba(X), labels=[0, 1]))

    def accuracy(self, X: np.ndarray, y: np.ndarray) -> float:
        return float(np.mean((self.predict_proba(X) >= 0.5).astype(int) == y))
