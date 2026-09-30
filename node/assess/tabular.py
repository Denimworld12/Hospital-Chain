"""Serve the tabular heart-risk ensemble: probability, risk tier, risk factors."""

from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np

from .features import FEATURES, bmi, featurize

HIGH_TIER_CONFIDENCE = 85.0  # % blended probability at or above which a positive is HIGH


def risk_tier(prediction: int, confidence: float) -> str:
    """LOW below the decision threshold; positives are HIGH at >= 85% blended probability, else MODERATE."""
    if not prediction:
        return "LOW"
    return "HIGH" if confidence >= HIGH_TIER_CONFIDENCE else "MODERATE"


def risk_factors(p: dict) -> dict[str, list[str]]:
    """Critical / moderate / lifestyle factors read straight off the patient's inputs."""
    critical, moderate, lifestyle = [], [], []
    if p["ap_hi"] >= 140 or p["ap_lo"] >= 90:
        critical.append("High blood pressure")
    if p["age"] > 50:
        critical.append("Age above 50")
    if p["cholesterol"] > 1:
        critical.append("High cholesterol")
    if p["smoke"]:
        critical.append("Smoking")
    b = float(bmi(p["height"], p["weight"]))
    if b >= 30:
        moderate.append("Obesity (BMI 30 or more)")
    elif b >= 25:
        moderate.append("Overweight (BMI 25 to 29.9)")
    if p["gluc"] > 1:
        moderate.append("High blood glucose")
    if p["alco"]:
        moderate.append("Alcohol intake")
    if not p["active"]:
        lifestyle.append("Physical inactivity")
    return {"critical": critical, "moderate": moderate, "lifestyle": lifestyle}


class TabularModel:
    def __init__(self, bundle: dict):
        if list(bundle["features"]) != list(FEATURES):
            raise ValueError("model was trained on a different feature set; retrain it")
        self.rf = bundle["random_forest"]
        self.rf.n_jobs = 1  # one row at a time: worker start-up would cost more than it saves
        self.xgb = bundle["xgb_calibrated"]
        self.imputer = bundle["imputer"]
        self.threshold = float(bundle["threshold"])
        self.weights = bundle["weights"]
        self.metrics = bundle["metrics"]
        self.source = bundle["source"]

    @classmethod
    def load(cls, path: str | Path) -> "TabularModel":
        return cls(joblib.load(path))

    def info(self) -> dict:
        return {"architecture": "RandomForest + calibrated XGBoost", "weights": self.weights,
                "threshold": self.threshold, "trained_on": self.source, "metrics": self.metrics}

    def assess(self, p: dict) -> dict:
        """`p` holds the Kaggle-coded fields with `age` in years."""
        X = self.imputer.transform(featurize({k: np.array([v], dtype=float) for k, v in p.items()}))
        rf_prob = float(self.rf.predict_proba(X)[0, 1])
        xgb_prob = float(self.xgb.predict_proba(X)[0, 1])
        prob = self.weights["random_forest"] * rf_prob + self.weights["xgboost"] * xgb_prob
        prediction = int(prob >= self.threshold)
        confidence = round(prob * 100, 2)
        factors = risk_factors(p)
        return {
            "prediction": prediction,
            "label": "Cardiovascular disease risk" if prediction else "No cardiovascular disease indicated",
            "confidence": confidence,
            "rf_confidence": round(rf_prob * 100, 2),
            "xgb_confidence": round(xgb_prob * 100, 2),
            "threshold": self.threshold,
            "risk_tier": risk_tier(prediction, confidence),
            "bmi": round(float(bmi(p["height"], p["weight"])), 1),
            "critical_factors": factors["critical"],
            "moderate_factors": factors["moderate"],
            "lifestyle_factors": factors["lifestyle"],
        }
