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
