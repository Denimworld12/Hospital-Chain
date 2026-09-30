"""Node API with no database and an unreachable neighbour (no docker needed)."""

import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from node import wire
from node.config import load_config
from node.db import NullDatabase
from node.main import create_app

ROOT = Path(__file__).resolve().parents[2]
SECRET = "unit-test-secret"
AUTH = {wire.H_SECRET: SECRET}


@pytest.fixture
def client(tmp_path):
    cfg = tmp_path / "node.yaml"
    cfg.write_text(
        f"""node_id: test-node
display_name: Test Node
port: 18999
data_slice: {ROOT / 'data/slices/hospital-a.csv'}
shared_secret: {SECRET}
neighbours:
  - id: peer
    url: http://127.0.0.1:9
training: {{rounds: 3}}
timing: {{heartbeat_interval_s: 5, round_timeout_s: 0.5, round_pause_s: 0}}
"""
    )
    app = create_app(load_config(cfg), database=NullDatabase())
    with TestClient(app) as c:
        yield c


def post_update(client, body, sender="peer", checksum=None, run="run-x", rnd=1, secret=SECRET):
    return client.post(
        "/update",
        content=body,
        headers={
            wire.H_SENDER: sender,
            wire.H_RUN: run,
            wire.H_ROUND: str(rnd),
            wire.H_CHECKSUM: checksum or wire.checksum(body),
            wire.H_DTYPE: "float64",
            **({wire.H_SECRET: secret} if secret is not None else {}),
        },
    )


def test_health_and_status(client):
    assert client.get("/health").json() == {"status": "ok", "node_id": "test-node"}
    s = client.get("/status").json()
    assert s["engine"] == "naive gossip baseline"
    assert s["patients"] == 303 and s["run_state"] == "idle"
    assert [n["id"] for n in s["neighbours"]] == ["peer"]


def test_update_checks_sender_and_checksum(client):
    body = wire.encode(np.zeros(16))
    ok = post_update(client, body)
    assert ok.status_code == 200 and ok.json()["participating"] is False
    assert post_update(client, body, checksum="0" * 64).status_code == 400
    assert post_update(client, body, sender="stranger").status_code == 403


def test_update_and_round_control_require_the_shared_secret(client):
    body = wire.encode(np.zeros(16))
    assert post_update(client, body, secret=None).status_code == 401
    assert post_update(client, body, secret="wrong").status_code == 401
    assert client.post("/round/start", json={}).status_code == 401
    assert client.post("/round/start", json={}, headers={wire.H_SECRET: "wrong"}).status_code == 401
    assert client.post("/round/pause", json={}).status_code == 401
    assert client.post("/round/resume", json={}).status_code == 401
    assert client.get("/status").json()["run_state"] == "idle"
    assert client.get("/health").status_code == 200 and client.get("/metrics").status_code == 200


def test_run_survives_an_unreachable_neighbour(client):
    r = client.post("/round/start", json={}, headers=AUTH).json()
    assert r["status"] == "started" and r["rounds"] == 3
    assert r["forwarded"]["peer"].startswith("unreachable")
    assert client.post("/round/start", json={"run_id": "other"}, headers=AUTH).status_code == 409
    deadline = time.time() + 20
    while time.time() < deadline and client.get("/metrics").json()["run_state"] != "completed":
        time.sleep(0.2)
    m = client.get("/metrics").json()
    assert m["run_state"] == "completed"
    assert [h["round"] for h in m["history"]] == [1, 2, 3]
    assert all(h["neighbours_heard"] == 0 for h in m["history"])
