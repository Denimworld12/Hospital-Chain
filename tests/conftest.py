import os
import sys
from pathlib import Path

# On macOS the xgboost wheel links Homebrew's libomp while torch bundles its
# own; with both OpenMP runtimes in one pytest process, Q-GADMM's threaded
# torch training segfaults. One OpenMP thread avoids the clash. Linux wheels
# (CI and the node images) use libgomp and are unaffected.
if sys.platform == "darwin":
    os.environ.setdefault("OMP_NUM_THREADS", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
