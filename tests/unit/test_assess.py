"""Per-patient risk assessment endpoints (no docker needed).

The tabular model here is a small one trained on a sample of the committed
Kaggle CSV, so the tests take seconds; the ECG model is the committed ONNX file.
"""

import io
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from node import wire
from node.assess.features import FEATURES, featurize
from node.assess.tabular import TabularModel, risk_factors, risk_tier
from node.assess.train_tabular import train
from node.config import load_config
from node.db import NullDatabase
from node.main import create_app

ROOT = Path(__file__).resolve().parents[2]
SECRET = "unit-test-secret"
AUTH = {wire.H_SECRET: SECRET}
ECG_MODEL = ROOT / "models/ecg-resnet18.onnx"

HEALTHY = {"age": 42, "gender": "female", "height": 162, "weight": 58, "ap_hi": 115, "ap_lo": 75,
           "cholesterol": 1, "gluc": 1, "smoke": False, "alco": False, "active": True}
AT_RISK = {"age": 61, "gender": "male", "height": 170, "weight": 98, "ap_hi": 165, "ap_lo": 100,
           "cholesterol": 3, "gluc": 2, "smoke": True, "alco": True, "active": False}


@pytest.fixture(scope="session")
def tabular_model_path(tmp_path_factory):
    import joblib

    path = tmp_path_factory.mktemp("models") / "heart-risk.joblib"
    joblib.dump(train(max_rows=6000, rf_estimators=40, xgb_estimators=60), path)
    return path


def make_client(tmp_path, tabular_model, ecg_model, display_name="Test Hospital"):
    cfg = tmp_path / "node.yaml"
    cfg.write_text(
        f"""node_id: test-node
display_name: {display_name}
port: 18998
data_slice: {ROOT / 'data/slices/hospital-a.csv'}
shared_secret: {SECRET}
assess:
  tabular_model: {tabular_model}
  ecg_model: {ecg_model}
"""
    )
    return TestClient(create_app(load_config(cfg), database=NullDatabase()))


@pytest.fixture
def client(tmp_path, tabular_model_path):
    with make_client(tmp_path, tabular_model_path, ECG_MODEL) as c:
        yield c


def png(size=(640, 480)) -> bytes:
    buf = io.BytesIO()
    rng = np.random.default_rng(0)
    Image.fromarray(rng.integers(0, 255, (*size[::-1], 3), dtype=np.uint8)).save(buf, format="PNG")
    return buf.getvalue()


def test_assess_response_shape(client):
    r = client.post("/assess", json=AT_RISK, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["node_id"] == "test-node" and body["hospital"] == "Test Hospital"
    assert body["prediction"] in (0, 1) and body["risk_tier"] in ("LOW", "MODERATE", "HIGH")
    for key in ("confidence", "rf_confidence", "xgb_confidence"):
        assert 0 <= body[key] <= 100
    assert body["confidence"] == pytest.approx(0.4 * body["rf_confidence"] + 0.6 * body["xgb_confidence"], abs=0.02)
    assert body["threshold"] == 0.30
    assert body["critical_factors"] == ["High blood pressure", "Age above 50", "High cholesterol", "Smoking"]
    assert body["moderate_factors"] == ["Obesity (BMI 30 or more)", "High blood glucose", "Alcohol intake"]
    assert body["lifestyle_factors"] == ["Physical inactivity"]
    assert body["model"]["trained_on"].startswith("Kaggle Cardiovascular Disease dataset")
    assert set(body["model"]["metrics"]) >= {"accuracy", "roc_auc"}


def test_assess_ranks_the_at_risk_patient_higher(client):
    healthy = client.post("/assess", json=HEALTHY, headers=AUTH).json()
    at_risk = client.post("/assess", json=AT_RISK, headers=AUTH).json()
    assert healthy["confidence"] < at_risk["confidence"]
    assert at_risk["prediction"] == 1 and at_risk["risk_tier"] != "LOW"
    assert healthy["critical_factors"] == healthy["moderate_factors"] == healthy["lifestyle_factors"] == []


def test_assess_pdf(client):
    r = client.post("/assess/pdf", json=AT_RISK, headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "risk-assessment-test-node.pdf" in r.headers["content-disposition"]
    assert r.content.startswith(b"%PDF")


def test_assess_validates_inputs(client):
    assert client.post("/assess", json={**HEALTHY, "ap_hi": 80, "ap_lo": 95}, headers=AUTH).status_code == 422
    assert client.post("/assess", json={**HEALTHY, "gender": 1}, headers=AUTH).status_code == 422
    assert client.post("/assess", json={**HEALTHY, "cholesterol": 4}, headers=AUTH).status_code == 422
    missing = {k: v for k, v in HEALTHY.items() if k != "age"}
    assert client.post("/assess", json=missing, headers=AUTH).status_code == 422


def test_assess_endpoints_require_the_shared_secret(client):
    for headers in ({}, {wire.H_SECRET: "wrong"}):
        assert client.post("/assess", json=HEALTHY, headers=headers).status_code == 401
        assert client.post("/assess/pdf", json=HEALTHY, headers=headers).status_code == 401
        files = {"ecg": ("ecg.png", png(), "image/png")}
        assert client.post("/assess/ecg", files=files, headers=headers).status_code == 401
        assert client.post("/assess/ecg/pdf", files=files, headers=headers).status_code == 401


def test_status_reports_which_models_are_loaded(client):
    assert client.get("/status").json()["assess"] == {"tabular": True, "ecg": True}


def test_missing_models_answer_503(tmp_path):
    with make_client(tmp_path, tmp_path / "absent.joblib", tmp_path / "absent.onnx") as c:
        assert c.get("/status").json()["assess"] == {"tabular": False, "ecg": False}
        assert c.post("/assess", json=HEALTHY, headers=AUTH).status_code == 503
        assert c.post("/assess/ecg", files={"ecg": ("e.png", png(), "image/png")}, headers=AUTH).status_code == 503
        assert c.get("/health").status_code == 200


def test_ecg_response_shape(client):
    r = client.post("/assess/ecg", files={"ecg": ("ecg.png", png(), "image/png")}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["prediction"] in (0, 1, 2)
    assert body["label"] in ("Normal ECG", "Myocardial infarction", "Abnormal heartbeat")
    assert set(body["probabilities"]) == {"normal", "mi", "abnormal"}
    assert sum(body["probabilities"].values()) == pytest.approx(1, abs=1e-3)
    assert body["confidence"] == pytest.approx(max(body["probabilities"].values()) * 100, abs=0.01)
    assert "Mendeley" in body["model"]["trained_on"] and "accuracy" in body["model"]["metrics"]


def test_ecg_pdf_and_bad_upload(client):
    r = client.post("/assess/ecg/pdf", files={"ecg": ("ecg.png", png(), "image/png")}, headers=AUTH)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    bad = client.post("/assess/ecg", files={"ecg": ("notes.txt", b"not an image", "text/plain")}, headers=AUTH)
    assert bad.status_code == 422
    assert client.post("/assess/ecg", headers=AUTH).status_code == 422


def test_pdfs_accept_non_latin1_names(tmp_path, tabular_model_path):
    with make_client(tmp_path, tabular_model_path, ECG_MODEL, display_name="एम्स दिल्ली — AIIMS") as c:
        name = "Screenshot 2026-09-30 at 10.15.02\u202fAM ईसीजी.png"
        r = c.post("/assess/ecg/pdf", files={"ecg": (name, png(), "image/png")}, headers=AUTH)
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        r = c.post("/assess/pdf", json=AT_RISK, headers=AUTH)
        assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_risk_tier_thresholds():
    assert risk_tier(0, 29.99) == "LOW"
    assert risk_tier(0, 99.0) == "LOW"  # the tier follows the thresholded prediction
    assert risk_tier(1, 30.0) == "MODERATE"
    assert risk_tier(1, 84.99) == "MODERATE"
    assert risk_tier(1, 85.0) == "HIGH"
    assert risk_tier(1, 100.0) == "HIGH"


class _Fixed:
    def __init__(self, p):
        self.p = p

    def predict_proba(self, X):
        return np.array([[1 - self.p, self.p]])


@pytest.mark.parametrize("rf,xgb,prediction,tier", [
    (0.20, 0.20, 0, "LOW"),
    (0.30, 0.30, 1, "MODERATE"),      # exactly on the 0.30 threshold counts as positive
    (0.10, 0.43, 0, "LOW"),           # 0.04 + 0.258 = 0.298 blended
    (0.80, 0.90, 1, "HIGH"),          # 0.32 + 0.54 = 0.86 blended
    (0.85, 0.85, 1, "HIGH"),
    (0.84, 0.84, 1, "MODERATE"),
])
def test_blend_threshold_and_tier(rf, xgb, prediction, tier):
    from sklearn.impute import SimpleImputer

    imputer = SimpleImputer().fit(np.zeros((2, len(FEATURES))))
    model = TabularModel({"features": FEATURES, "random_forest": _Fixed(rf), "xgb_calibrated": _Fixed(xgb),
                          "imputer": imputer, "threshold": 0.30, "weights": {"random_forest": 0.4, "xgboost": 0.6},
                          "metrics": {}, "source": "test"})
    coded = {**HEALTHY, "gender": 1, "smoke": 0, "alco": 0, "active": 1}
    out = model.assess(coded)
    assert out["prediction"] == prediction and out["risk_tier"] == tier
    assert out["confidence"] == pytest.approx((0.4 * rf + 0.6 * xgb) * 100, abs=0.01)


def test_risk_factors_bmi_bands():
    base = {"ap_hi": 120, "ap_lo": 80, "age": 40, "cholesterol": 1, "gluc": 1, "smoke": 0, "alco": 0, "active": 1,
            "height": 170, "weight": 60}
    assert risk_factors({**base, "weight": 72})["moderate"] == []                    # BMI 24.9
    assert risk_factors({**base, "weight": 73})["moderate"] == ["Overweight (BMI 25 to 29.9)"]
    assert risk_factors({**base, "weight": 87})["moderate"] == ["Obesity (BMI 30 or more)"]
    assert risk_factors({**base, "ap_hi": 139, "ap_lo": 90})["critical"] == ["High blood pressure"]


def test_feature_buckets_are_right_inclusive():
    # BMI 20, exactly 25.0, 31
    cols = {"gender": [1, 1, 1], "height": [200, 200, 200], "weight": [80, 100, 124], "ap_hi": [120, 140, 250],
            "ap_lo": [80, 80, 40], "cholesterol": [1, 1, 1], "gluc": [1, 1, 1], "smoke": [0, 0, 0],
            "alco": [0, 0, 0], "active": [1, 1, 1], "age": [40, 40.01, 71]}
    X = featurize({k: np.array(v, dtype=float) for k, v in cols.items()})
    f = {name: X[:, i] for i, name in enumerate(FEATURES)}
    assert f["age_group_40-50"].tolist() == [0, 1, 0] and f["age_group_70+"].tolist() == [0, 0, 1]
    assert f["bmi_category_normal"].tolist() == [1, 1, 0] and f["bmi_category_obese"].tolist() == [0, 0, 1]
    assert f["ap_hi"].tolist() == [120, 140, 200] and f["ap_lo"].tolist() == [80, 80, 60]  # clipped
    assert f["hypertension_stage"].tolist() == [0, 1, 2]
    assert f["pulse_pressure"].tolist() == [40, 60, 140]
