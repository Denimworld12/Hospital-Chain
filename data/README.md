# Data

`slices/` holds one CSV per hospital, produced by `prepare_uci.py` from the UCI
Heart Disease collection (Janosi, Steinbrunn, Pfisterer, Detrano, 1989;
<https://doi.org/10.24432/C52P4X>; CC BY 4.0). The slices are committed so the
demo and CI do not depend on the UCI server being up; re-run the script to
regenerate them.

| Slice | Source institution | Patients |
| --- | --- | --- |
| `hospital-a.csv` | Cleveland Clinic Foundation | 303 |
| `hospital-b.csv` | Hungarian Institute of Cardiology | 294 |
| `hospital-c.csv` | V.A. Medical Center, Long Beach | 200 |

The split is non-IID by construction: each hospital holds a different real
institution's patients. In `docker-compose.yml` each node container mounts
**only its own** slice; raw patient rows never leave the container that holds
them, and only model weight arrays are exchanged between nodes.

## Risk-assessment data (not used by federated training)

The per-patient risk assessment endpoints (`node/assess/`) use two centrally
trained models. Neither dataset is Indian-sourced; swapping in Indian clinical
data is a planned follow-up.

| Data | Source | License | In the repo |
| --- | --- | --- | --- |
| `cardio/cardio_train.csv` | Kaggle [Cardiovascular Disease dataset](https://www.kaggle.com/datasets/sulianova/cardiovascular-disease-dataset) (Svetlana Ulianova): 70,000 patients, general/international, semicolon-delimited, `age` in days, `cardio` target. The same file HEALIGRAM trains on (SHA-256 `21a705d2...a10c`). | Kaggle lists it as "Unknown" | Yes (2.9 MB). The node image trains the tabular model from it at build time; the CSV is not in the final image. |
| ECG images | Mendeley [ECG Images dataset of Cardiac Patients](https://doi.org/10.17632/gwbz3fsgp8.2) (Khan & Hussain, 2021): 928 12-lead ECG printouts from the Ch. Pervaiz Elahi Institute of Cardiology, Multan, Pakistan | CC BY 4.0 | No (~615 MB). `prepare_ecg.py` downloads it; only the model trained on it, `models/ecg-resnet18.onnx`, is committed. |

Column coding for `cardio_train.csv`: `gender` 1 = female, 2 = male (the
dataset does not document it; men are the taller group and smoke far more);
`cholesterol` and `gluc` 1 = normal, 2 = above normal, 3 = well above normal;
`smoke`, `alco`, `active` 0/1.
