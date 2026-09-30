"""Train the ECG image classifier and export it to ONNX (offline; needs torch).

Approach after HEALIGRAM (github.com/avdhoot-hande/HEALIGRAM): an
ImageNet-pretrained torchvision ResNet18 with a 3-class linear head, fine-tuned
with Adam (lr 1e-4) for 10 epochs on 224x224 ECG printouts, 80/20 split.
Trained fresh here on the public Mendeley ECG dataset (CC BY 4.0, see
data/prepare_ecg.py); the images are not committed, the exported model is.

    pip install -r requirements-ecg-train.txt
    python3 data/prepare_ecg.py
    python -m node.assess.train_ecg                  # -> models/ecg-resnet18.onnx
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torchvision
from PIL import Image
from sklearn.metrics import confusion_matrix
from sklearn.model_selection import train_test_split
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from .ecg import CLASSES, IMAGE_SIZE, MEAN, STD

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = ROOT / "data/ecg-images"
DEFAULT_OUT = ROOT / "models/ecg-resnet18.onnx"
SOURCE = ("Mendeley ECG Images dataset of Cardiac Patients (Khan & Hussain 2021, "
          "doi:10.17632/gwbz3fsgp8.2, CC BY 4.0), 928 images")

# Folder prefix -> class; History-of-MI patients count as MI, as in HEALIGRAM.
FOLDERS = {
    "Normal Person ECG Images": "normal",
    "ECG Images of Myocardial Infarction Patients": "mi",
    "ECG Images of Patient that have History of MI": "mi",
    "ECG Images of Patient that have abnormal heartbeat": "abnormal",
}


def load_images(root: Path) -> tuple[np.ndarray, np.ndarray]:
    images, labels = [], []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        cls = next((c for prefix, c in FOLDERS.items() if folder.name.startswith(prefix)), None)
        if cls is None:
            continue
        for f in sorted(folder.glob("*.jpg")):
            img = Image.open(f).convert("RGB").resize((IMAGE_SIZE, IMAGE_SIZE), Image.BILINEAR)
            images.append(np.asarray(img, dtype=np.uint8))
            labels.append(CLASSES.index(cls))
    if not images:
        raise SystemExit(f"no ECG images under {root}; run data/prepare_ecg.py first")
    return np.stack(images), np.array(labels)


def to_tensor(images: np.ndarray) -> torch.Tensor:
    x = torch.from_numpy(images).float().div(255.0)
    return ((x - torch.from_numpy(MEAN)) / torch.from_numpy(STD)).permute(0, 3, 1, 2).contiguous()


def main() -> int:
    ap = argparse.ArgumentParser(description="Train the ECG ResNet18 classifier and export ONNX.")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    torch.manual_seed(args.seed)
    t0 = time.time()

    images, labels = load_images(args.data)
    X_train, X_test, y_train, y_test = train_test_split(images, labels, test_size=0.2, random_state=args.seed,
                                                        stratify=labels)
    print(f"{len(labels)} images, per class {np.bincount(labels).tolist()}; train {len(y_train)}, test {len(y_test)}")
    train_loader = DataLoader(TensorDataset(to_tensor(X_train), torch.from_numpy(y_train)), batch_size=32, shuffle=True)
    test_loader = DataLoader(TensorDataset(to_tensor(X_test), torch.from_numpy(y_test)), batch_size=64)

    device = torch.device("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")
    model = torchvision.models.resnet18(weights=torchvision.models.ResNet18_Weights.IMAGENET1K_V1)
    model.fc = nn.Linear(model.fc.in_features, len(CLASSES))
    model = model.to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-4)
    criterion = nn.CrossEntropyLoss()
    for epoch in range(args.epochs):
        model.train()
        total = 0.0
        for xb, yb in train_loader:
            xb, yb = xb.to(device), yb.to(device)
            optimizer.zero_grad()
            loss = criterion(model(xb), yb)
            loss.backward()
            optimizer.step()
            total += loss.item() * len(yb)
        print(f"epoch {epoch + 1}/{args.epochs}: train loss {total / len(y_train):.4f}")

    model.eval()
    preds = []
    with torch.no_grad():
        for xb, _ in test_loader:
            preds.append(model(xb.to(device)).argmax(1).cpu().numpy())
    preds = np.concatenate(preds)
    metrics = {
        "accuracy": round(float((preds == y_test).mean()), 4),
        "n_train": int(len(y_train)),
        "n_test": int(len(y_test)),
        "confusion_matrix": confusion_matrix(y_test, preds, labels=range(len(CLASSES))).tolist(),
        "classes": list(CLASSES),
    }
    print(f"held-out accuracy {metrics['accuracy']:.4f}; confusion {metrics['confusion_matrix']}")

    import onnx

    args.out.parent.mkdir(parents=True, exist_ok=True)
    model = model.cpu()
    torch.onnx.export(model, (torch.zeros(1, 3, IMAGE_SIZE, IMAGE_SIZE),), str(args.out), input_names=["image"],
                      output_names=["logits"], dynamo=False)
    proto = onnx.load(str(args.out))
    for key, value in {"metrics": json.dumps(metrics), "source": SOURCE}.items():
        proto.metadata_props.add(key=key, value=value)
    onnx.save(proto, str(args.out))
    print(f"exported {args.out} ({args.out.stat().st_size / 1e6:.1f} MB) in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
