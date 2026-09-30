#!/usr/bin/env python3
"""Download the Mendeley "ECG Images dataset of Cardiac Patients" for the ECG classifier.

928 12-lead ECG printouts (JPG) from the Ch. Pervaiz Elahi Institute of
Cardiology, Multan, in four folders. `node/risk/train_ecg.py` maps them to the
three classes the classifier predicts:

    Normal Person ECG Images                          -> normal
    ECG Images of Myocardial Infarction Patients      -> myocardial infarction
    ECG Images of Patient that have History of MI     -> myocardial infarction
    ECG Images of Patient that have abnormal heartbeat -> abnormal heartbeat

The images (~615 MB) are NOT committed; only the model trained on them is.
Stdlib only; files already on disk with the right size are skipped.

    python3 data/prepare_ecg.py                  # -> data/ecg-images/<folder>/*.jpg
    python3 data/prepare_ecg.py --out /some/dir

Source: Khan, Ali Haider; Hussain, Muzammil (2021). ECG Images dataset of Cardiac
Patients. Mendeley Data, V2. https://doi.org/10.17632/gwbz3fsgp8.2 (CC BY 4.0).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://data.mendeley.com/public-api/datasets/gwbz3fsgp8"
VERSION = 2
OUT_DIR = Path(__file__).resolve().parent / "ecg-images"
HEADERS = {"User-Agent": "medchain-prepare-ecg/1.0"}  # Mendeley answers 403 to urllib's default agent


def fetch(url: str, timeout: float = 60) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as resp:
        return resp.read()


def get_json(url: str):
    return json.loads(fetch(url))


def download(entry: tuple[dict, Path]) -> int:
    f, dest = entry
    if dest.is_file() and dest.stat().st_size == f["size"]:
        return 0
    data = fetch(f["content_details"]["download_url"], timeout=120)
    if len(data) != f["size"]:
        raise RuntimeError(f"{dest.name}: got {len(data)} bytes, expected {f['size']}")
    dest.write_bytes(data)
    return len(data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args()
    jobs = []
    for folder in get_json(f"{API}/folders/{VERSION}"):
        target = args.out / folder["name"].strip()
        target.mkdir(parents=True, exist_ok=True)
        files = get_json(f"{API}/files?folder_id={folder['id']}&version={VERSION}")
        jobs += [(f, target / f["filename"]) for f in files]
        print(f"{folder['name']}: {len(files)} images")
    with ThreadPoolExecutor(max_workers=8) as pool:
        fetched = sum(pool.map(download, jobs))
    print(f"{len(jobs)} images in {args.out} ({fetched / 1e6:.0f} MB downloaded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
