# MedChain

MedChain is a simulation of decentralized collaborative learning for hospitals.
Three hospital nodes train one heart-disease model together on real UCI data.
Each hospital trains only on its own patients, and only model weight arrays
travel between neighbouring nodes. Patient rows never leave the node that holds
them.

The training engine that ships today is a **naive gossip baseline**: each node
averages its weights with its neighbours' weights. The engine sits behind a
swappable `Engine` interface (`node/engine.py`). No other engine exists yet.

## What it looks like

These screenshots come from a real stack running this repository's code. Each
one was taken while a real training round ran through the dashboard.

### Topology

![Network topology: three hospital nodes in a chain, all alive](docs/screenshots/topology.png)

This shows the three hospitals in a chain (A - B - C). Each node has a status
dot: green means the node sent a heartbeat recently, and orange means it is
down. Under each hospital is its patient count and the last round it finished.

### Training monitor

![Training monitor: loss and accuracy curves for each hospital over 20 rounds](docs/screenshots/training-monitor.png)

Each hospital has its own card with live validation loss and accuracy. Every
hospital measures these on its own held-out split, round by round.

### Communication

![Communication panel: bytes sent this round, full precision vs actually sent, bytes per round](docs/screenshots/communication.png)

This panel shows the bytes that crossed the network this round and in total
for the run. Every byte is part of a weight update. The baseline sends plain
float64 weights, so "actually sent" equals "full precision would cost".

### Audit log

![Audit log: one row per weight update with sender, receiver, payload bytes and SHA-256 checksum](docs/screenshots/audit-log.png)

The audit log has one row for every `/update` a node receives. Each row shows
the timestamp, round, sender, receiver, payload size and SHA-256 checksum of
the weights. Run lifecycle events are logged here too.

## Requirements

- **Docker** with the **Docker Compose v2** plugin (`docker compose ...`).
  Everything else runs inside containers: Python 3.12 for the nodes, Node 22
  for the dashboard build, and Postgres 16.
- **Free host ports:** `5432`, `8001`, `8002`, `8003` and `8080`.
- **Free container names:** `hospital-a`, `hospital-b`, `hospital-c`,
  `dashboard` and `medchain-postgres`. The compose file sets fixed
  `container_name`s, so only one copy of the stack can run on a machine at a
  time.
- **Internet access on the first build**, to pull the base images and install
  the pip and npm packages. The data slices ship in `data/slices/`, so the
  dataset does not need to be downloaded.
- To run the tests outside Docker, you need **Python 3.12** and `pip` (see
  [Tests](#tests)).

## Running it

The system has two parts: the **backend** (three hospital nodes plus Postgres)
and the **dashboard** (an observer UI). One compose file starts both.

### 1. Start the backend

From the repository root:

```sh
docker compose up -d --build --wait
docker compose ps
```

`--wait` returns when every service is healthy. These services come up:

| Service | Host port | What it is |
| --- | --- | --- |
| `hospital-a` / `hospital-b` / `hospital-c` | 8001 / 8002 / 8003 | FastAPI node. All three run the same image, and the mounted `config/hospital-*.yaml` decides which hospital it is. Each node mounts only its own data slice. |
| `postgres` (container `medchain-postgres`) | 5432 | Holds `nodes`, `runs`, `rounds`, `updates_meta`, `metrics` and `audit_logs`. |
| `dashboard` | 8080 | Observer UI. It reads Postgres and pushes snapshots over `/ws`. |

Check that a node is up. `/health`, `/status` and `/metrics` need no
authentication:

```sh
curl http://localhost:8001/health
curl http://localhost:8001/status
```

To start a training round directly against a node's API, send the shared
secret in the header. The demo secret is `shared_secret` in
`config/hospital-*.yaml`:

```sh
curl -X POST http://localhost:8001/round/start \
  -H 'Content-Type: application/json' \
  -H 'X-MedChain-Secret: medchain-demo-secret' \
  -d '{"rounds": 20}'
```

The node that gets the request floods it to its neighbours, so all three
hospitals start the same run. `rounds` is optional and defaults to 20, from
the config. A request without the header gets `401`. The same header works
for `POST /round/pause` and `POST /round/resume`.

Once the run finishes, you can read any node's per-round history:

```sh
curl http://localhost:8002/metrics
```

### 2. Open the dashboard

Open <http://localhost:8080> in a browser.

- Press **START TRAINING ROUND** in the top bar. The button changes to show
  `TRAINING: ROUND n OF 20`, and a **PAUSE** button becomes active.
- Use the top navigation (**TOPOLOGY**, **TRAINING MONITOR**,
  **COMMUNICATION**, **AUDIT LOG**) to watch the run live. The pages are the
  ones shown in the screenshots above. A 20-round run takes about 20 seconds.

The dashboard only observes. Its buttons send a single request to one live
node, which floods it to the others, just like the `curl` above. The dashboard
never coordinates training, so stopping it does not affect a running round.

### Kill-test demo (optional)

Start a longer run so there is time to act, then kill the middle hospital
while it trains:

```sh
curl -X POST http://localhost:8001/round/start \
  -H 'Content-Type: application/json' \
  -H 'X-MedChain-Secret: medchain-demo-secret' \
  -d '{"rounds": 40}'
sleep 4
docker kill hospital-b
```

Within about 10 seconds, Hospital B's status dot turns orange and the
**TOPOLOGY** page reads `2 OF 3 NODES ALIVE`:

![Topology after docker kill hospital-b: B is down, A and C are alive](docs/screenshots/kill-topology.png)

Hospitals A and C keep training. B is their only neighbour, so each one skips
the unreachable neighbour and keeps training on its own data. B's curve stops
at the round where it died:

![Training monitor after the kill: A and C reach later rounds, B stops at round 4 and shows DOWN](docs/screenshots/kill-training-monitor.png)

The audit log records every failed send as `neighbour unreachable`:

![Audit log after the kill: neighbour unreachable rows from hospital-a and hospital-c to hospital-b](docs/screenshots/kill-audit-log.png)

Bring B back:

```sh
docker start hospital-b
```

Within a few seconds, B's dot is green again. It joins the next run you start.

### Stop

```sh
docker compose down        # add -v to also delete the Postgres data
```

## How it fits together

- **One node image, config decides identity** (`config/hospital-*.yaml`: node_id,
  neighbours, port, data_slice, training settings). The topology is a chain
  A - B - C. `rho` and `bits` are in the config, but the baseline ignores them
  and always sends 64-bit weights.
- **Peer-to-peer rounds** (`node/runner.py`): a start request floods from the
  node that receives it. Each node then trains locally, `POST /update`s its raw
  float64 weight vector to its neighbours, waits a bounded time for theirs,
  averages, and logs. Unreachable neighbours are skipped, so the survivors keep
  training.
- **Swappable engine** (`node/engine.py`): `train_round()`, `package_update()`,
  `apply_neighbor_update()`, `evaluate()`. `node/gossip.py` is the only
  implementation.
- **Honest accounting**: payload bytes are the exact HTTP body sizes (the weight
  array, not counting headers). The baseline does not quantize, so "actually
  sent" equals "full precision would cost".
- **Shared secret**: `/update` and `/round/*` require the `X-MedChain-Secret`
  header to match `shared_secret` in the node config, or they return 401. All
  nodes use one demo value, and the dashboard gets it through
  `MEDCHAIN_SHARED_SECRET`. `/health`, `/status` and `/metrics` stay open.

## Layout

```
docker-compose.yml   config/        node/          dashboard/
design/              assets/svg/    data/          tests/
docs/screenshots/
```

- `design/STYLE.md` and `design/tokens.css` are the Pa'lais style reference,
  adapted.
- `assets/generate_svgs.py` regenerates `assets/svg/`. `assets/preview.html`
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

CI (`.github/workflows/ci.yml`) runs the unit tests, the dashboard build, the
docker build and the live-stack integration tests.
