# MedChain

A decentralized collaborative learning platform for hospitals - Week 1-2 MVP.
Three hospital nodes train a heart-disease model together on real UCI data;
patient rows never leave the node that holds them, only model weight arrays
travel between neighbours. The training engine today is a **naive gossip
baseline** (neighbour weight averaging); an ADMM-based engine (Q-GADMM) replaces
it in a later phase behind the same `Engine` interface.

## Run the demo

```sh
docker compose up --build
```

Open <http://localhost:8080> and press **START TRAINING ROUND**: 20 rounds run,
and the loss/accuracy curves, byte counters and audit log fill live over a
WebSocket. Try `docker kill hospital-b` mid-run: its dot turns orange within 10s
and hospitals A and C keep training. `docker start hospital-b` brings it back.

| Service | Port | What it is |
| --- | --- | --- |
| `hospital-a` / `-b` / `-c` | 8001 / 8002 / 8003 | FastAPI node: `GET /health`, `GET /status`, `POST /round/start`, `POST /update`, `GET /metrics` (+ `POST /round/pause`, `/round/resume`) |
| `postgres` | 5432 | `nodes`, `runs`, `rounds`, `updates_meta`, `metrics`, `audit_logs` |
| `dashboard` | 8080 | Observer UI; reads Postgres, pushes snapshots over `/ws` |

## How it fits together

- **One node image, config decides identity** (`config/hospital-*.yaml`: node_id,
  neighbours, port, rho, bits, data_slice). Topology is a chain A - B - C.
- **Peer-to-peer rounds** (`node/runner.py`): a start request floods from the
  node that receives it; each node then trains locally, `POST /update`s its raw
  float64 weight vector to its neighbours, waits (bounded) for theirs, averages,
  and logs. Unreachable neighbours are skipped, so survivors keep training.
- **Swappable engine** (`node/engine.py`): `train_round()`, `package_update()`,
  `apply_neighbor_update()`, `evaluate()`. `node/gossip.py` is the baseline.
- **Honest accounting**: payload bytes are the exact HTTP body sizes (the weight
  array; headers not counted). The baseline does not quantize, so "actually
  sent" equals "full precision would cost" until Q-GADMM lands.
- **Shared secret**: `/update` and `/round/*` require the `X-MedChain-Secret`
  header to match `shared_secret` in the node config (one demo value for all
  nodes; the dashboard gets it via `MEDCHAIN_SHARED_SECRET`), otherwise 401.
  `/health`, `/status` and `/metrics` stay open.
- **Observer dashboard**: it never coordinates training. Its buttons relay a
  single start/pause request to one node, which floods it to the others; killing
  the dashboard does not affect a run (tested).

## Layout

```
docker-compose.yml   config/        node/          dashboard/
design/              assets/svg/    data/          tests/
```

- `design/STYLE.md`, `design/tokens.css` - the Pa'lais style reference, adapted.
- `assets/generate_svgs.py` regenerates `assets/svg/`; `assets/preview.html`
  shows every asset on the cream canvas.
- `data/prepare_uci.py` re-downloads UCI Heart Disease and rebuilds the three
  non-IID slices (Cleveland, Hungarian, VA Long Beach). See `data/README.md`.

## Tests

```sh
pip install -r requirements-dev.txt
pytest tests/unit                                   # no docker needed
docker compose up -d --build --wait
MEDCHAIN_STACK=1 pytest tests/integration -v        # real runs, real docker kill
```

CI (`.github/workflows/ci.yml`) runs the unit tests, the dashboard build, and
the docker build plus the live-stack integration tests.
