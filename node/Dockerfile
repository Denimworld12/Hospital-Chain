FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# xgboost links the system OpenMP runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY node/requirements.txt /app/node/requirements.txt
RUN pip install --no-cache-dir -r /app/node/requirements.txt

# Train the tabular risk-assessment model from the public Kaggle CSV. Only the
# trained model leaves this stage; the CSV is not in the final image.
FROM base AS risk-model
COPY node/__init__.py /app/node/__init__.py
COPY node/assess/__init__.py node/assess/features.py node/assess/train_tabular.py /app/node/assess/
COPY data/cardio/cardio_train.csv /tmp/cardio_train.csv
RUN python -m node.assess.train_tabular --csv /tmp/cardio_train.csv --out /app/models/heart-risk.joblib

FROM base
COPY node /app/node
# the same image runs the directory service (python -m directory) and issues the demo keys
COPY directory /app/directory
COPY scripts/pki.py /app/scripts/pki.py
COPY models/ecg-resnet18.onnx /app/models/ecg-resnet18.onnx
COPY --from=risk-model /app/models/heart-risk.joblib /app/models/heart-risk.joblib
# No training data, no config and no keys baked in: docker-compose mounts this node's
# own config, only this node's own data slice, and the keys from scripts/pki.py.
# The healthcheck is local liveness only (127.0.0.1), so it skips certificate checks.
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import os,ssl,urllib.request,yaml;c=yaml.safe_load(open(os.environ['NODE_CONFIG']));s='https' if c.get('tls') else 'http';urllib.request.urlopen(f'{s}://127.0.0.1:{c[\"port\"]}/health',timeout=2,context=ssl._create_unverified_context())"
CMD ["python", "-m", "node"]
