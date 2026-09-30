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
COPY models/ecg-resnet18.onnx /app/models/ecg-resnet18.onnx
COPY --from=risk-model /app/models/heart-risk.joblib /app/models/heart-risk.joblib
# No training data and no config baked in: docker-compose mounts this node's own
# config and only this node's own data slice.
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import os,urllib.request,yaml;c=yaml.safe_load(open(os.environ['NODE_CONFIG']));urllib.request.urlopen(f'http://127.0.0.1:{c[\"port\"]}/health',timeout=2)"
CMD ["python", "-m", "node"]
