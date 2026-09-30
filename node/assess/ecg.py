"""Serve the ECG image classifier (ResNet18, 3 classes) with ONNX Runtime.

The network is trained offline by `train_ecg.py` and exported to ONNX, so the
node image needs neither torch nor the training images, only onnxruntime.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, UnidentifiedImageError

CLASSES = ("normal", "mi", "abnormal")
LABELS = {"normal": "Normal ECG", "mi": "Myocardial infarction", "abnormal": "Abnormal heartbeat"}
IMAGE_SIZE = 224
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)  # ImageNet statistics (pretrained backbone)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def preprocess(data: bytes) -> np.ndarray:
    """Image bytes -> float32 array of shape (1, 3, 224, 224); raises ValueError on a non-image."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("could not decode the upload as an image (send a PNG or JPG ECG)") from exc
    arr = np.asarray(img.resize((IMAGE_SIZE, IMAGE_SIZE), Image.BILINEAR), dtype=np.float32) / 255.0
    return ((arr - MEAN) / STD).transpose(2, 0, 1)[None]


def softmax(z: np.ndarray) -> np.ndarray:
    e = np.exp(z - z.max())
    return e / e.sum()


class EcgModel:
    def __init__(self, path: str | Path):
        import onnxruntime as ort

        self.session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        meta = self.session.get_modelmeta().custom_metadata_map
        self.metrics = json.loads(meta.get("metrics", "{}"))
        self.source = meta.get("source", "")

    def info(self) -> dict:
        return {"architecture": "ResNet18 CNN (ONNX)", "classes": [LABELS[c] for c in CLASSES],
                "trained_on": self.source, "metrics": self.metrics}

    def assess(self, data: bytes) -> dict:
        logits = self.session.run(None, {self.input: preprocess(data)})[0][0]
        probs = softmax(logits.astype(np.float64))
        k = int(np.argmax(probs))
        return {
            "prediction": k,
            "label": LABELS[CLASSES[k]],
            "confidence": round(float(probs[k]) * 100, 2),
            "probabilities": {c: round(float(p), 4) for c, p in zip(CLASSES, probs)},
        }
