FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY node/requirements.txt /app/node/requirements.txt
RUN pip install --no-cache-dir -r /app/node/requirements.txt
COPY node /app/node
# No data and no config baked in: docker-compose mounts this node's own config
# and only this node's own data slice.
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import os,urllib.request,yaml;c=yaml.safe_load(open(os.environ['NODE_CONFIG']));urllib.request.urlopen(f'http://127.0.0.1:{c[\"port\"]}/health',timeout=2)"
CMD ["python", "-m", "node"]
