"""Per-patient risk assessment endpoints (inference only, separate from federated training).

    POST /assess           tabular JSON -> prediction, confidences, risk tier, risk factors
    POST /assess/pdf       same body -> PDF report
    POST /assess/ecg       multipart `ecg` image -> ECG class + probabilities
    POST /assess/ecg/pdf   same upload -> PDF report

All four sit behind the shared-secret header, like /update and /round/*. The
handlers are sync so FastAPI runs the CPU-bound scoring in its threadpool,
off the event loop that serves training traffic. Nothing
here touches the training engine, and patient inputs are neither stored nor logged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator

from . import report
from .ecg import EcgModel
from .tabular import TabularModel

log = logging.getLogger("medchain.assess")

MAX_ECG_BYTES = 15 * 1024 * 1024


class PatientIn(BaseModel):
    """Fields of the Kaggle Cardiovascular Disease dataset, with age in years."""

    age: float = Field(ge=18, le=100, description="years")
    gender: Literal["female", "male"]
    height: float = Field(ge=120, le=220, description="cm")
    weight: float = Field(ge=30, le=200, description="kg")
    ap_hi: float = Field(ge=60, le=250, description="systolic blood pressure, mmHg")
    ap_lo: float = Field(ge=40, le=160, description="diastolic blood pressure, mmHg")
    cholesterol: Literal[1, 2, 3] = Field(description="1 normal, 2 above normal, 3 well above normal")
    gluc: Literal[1, 2, 3] = Field(description="1 normal, 2 above normal, 3 well above normal")
    smoke: bool
    alco: bool
    active: bool

    @model_validator(mode="after")
    def diastolic_not_above_systolic(self):
        if self.ap_lo > self.ap_hi:
            raise ValueError("ap_lo (diastolic) cannot be higher than ap_hi (systolic)")
        return self

    def coded(self) -> dict:
        d = self.model_dump()
        d["gender"] = 2 if self.gender == "male" else 1
        for k in ("smoke", "alco", "active"):
            d[k] = int(d[k])
        return d


def _load(kind: str, path: str, loader):
    if not path or not Path(path).is_file():
        log.warning("%s model not found at %s; its /assess endpoints will answer 503", kind, path)
        return None
    try:
        return loader(path)
    except Exception:  # a broken artifact should disable the endpoint, not the node
        log.exception("could not load %s model from %s", kind, path)
        return None


class Assessor:
    def __init__(self, tabular_path: str, ecg_path: str):
        self.tabular: TabularModel | None = _load("tabular", tabular_path, TabularModel.load)
        self.ecg: EcgModel | None = _load("ECG", ecg_path, EcgModel)

    def status(self) -> dict:
        return {"tabular": self.tabular is not None, "ecg": self.ecg is not None}


def build_router(node_id: str, hospital: str, assessor: Assessor, dependencies: list) -> APIRouter:
    router = APIRouter(prefix="/assess", dependencies=dependencies)

    def tabular() -> TabularModel:
        if assessor.tabular is None:
            raise HTTPException(503, detail=f"the tabular risk model is not available on {node_id}")
        return assessor.tabular

    def ecg() -> EcgModel:
        if assessor.ecg is None:
            raise HTTPException(503, detail=f"the ECG model is not available on {node_id}")
        return assessor.ecg

    def read_ecg(upload: UploadFile) -> bytes:
        data = upload.file.read(MAX_ECG_BYTES + 1)
        if len(data) > MAX_ECG_BYTES:
            raise HTTPException(413, detail=f"ECG image larger than {MAX_ECG_BYTES // (1024 * 1024)} MB")
        return data

    def run_ecg(model: EcgModel, data: bytes) -> dict:
        try:
            return model.assess(data)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc))

    def pdf(content: bytes, name: str) -> Response:
        return Response(content, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{name}-{node_id}.pdf"'})

    @router.post("")
    def assess(patient: PatientIn):
        model = tabular()
        return {"node_id": node_id, "hospital": hospital, **model.assess(patient.coded()), "model": model.info()}

    @router.post("/pdf")
    def assess_pdf(patient: PatientIn):
        model = tabular()
        coded = patient.coded()
        return pdf(report.tabular_report(hospital, coded, model.assess(coded), model.info()), "risk-assessment")

    @router.post("/ecg")
    def assess_ecg(ecg_file: UploadFile = File(alias="ecg")):
        model = ecg()
        result = run_ecg(model, read_ecg(ecg_file))
        return {"node_id": node_id, "hospital": hospital, **result, "model": model.info()}

    @router.post("/ecg/pdf")
    def assess_ecg_pdf(ecg_file: UploadFile = File(alias="ecg")):
        model = ecg()
        result = run_ecg(model, read_ecg(ecg_file))
        return pdf(report.ecg_report(hospital, ecg_file.filename or "", result, model.info()), "ecg-analysis")

    return router
