# MedChain

MedChain is a simulation of decentralized collaborative learning for hospitals.
Three hospital nodes train one heart-disease model together on real UCI data.
Each hospital trains only on its own patients, and only model updates travel
between neighbouring nodes on a chain A - B - C. Patient rows never leave the
node that holds them.

The training engine is **Q-GADMM** (Elgabli et al., "Q-GADMM: Quantized Group
ADMM for Communication-Efficient Decentralized Machine Learning," IEEE Trans.
Commun. 69(1), 2021). Full-precision GADMM and the original **naive gossip
baseline** (each node averages its weights with its neighbours') stay in the
codebase as selectable comparisons behind the same swappable `Engine` interface
(`node/engine.py`). See [Engines](#engines).

Each hospital node also serves a separate **per-patient risk assessment**: a
RandomForest + XGBoost heart-risk ensemble and a ResNet18 ECG image classifier,
ported from [HEALIGRAM](https://github.com/avdhoot-hande/HEALIGRAM)'s approach
and trained fresh in this repository. It is an inference tool only. It is not
part of federated training, and the patients it scores are never stored. Both
models are trained on public, **non-Indian** datasets (see
[Risk assessment](#3-assess-a-patient-optional)).

## What it looks like

These screenshots come from a real stack running this repository's code. Each
one was taken while a real training round ran through the dashboard. They were
captured on the earlier gossip-baseline MVP dashboard, so they predate the
consensus panel, the network-loss panel, the engine chip and the 32-bit
comparison described below.

### Topology

![Network topology: three hospital nodes in a chain, all alive (gossip-baseline MVP dashboard)](docs/screenshots/topology.png)

This shows the three hospitals in a chain (A - B - C). Each node has a status
dot: green means the node sent a heartbeat recently, and orange means it is
down. Under each hospital is its patient count and the last round it finished.
The current dashboard also animates an envelope along an edge on each
`/update`. (Screenshot from the gossip-baseline MVP.)

### Training monitor

![Training monitor: loss and accuracy curves for each hospital (gossip-baseline MVP dashboard)](docs/screenshots/training-monitor.png)

Each hospital has its own card with live validation loss and accuracy. Every
hospital measures these on its own held-out split, round by round. The current
dashboard adds a network training loss panel and the consensus panel
("disagreement between neighbours": primal residual per edge vs round, with a
**CONSENSUS REACHED** pill once every edge is below epsilon). (Screenshot from
the gossip-baseline MVP, before those panels existed.)

### Communication

![Communication panel: bytes sent this round and per round (gossip-baseline MVP dashboard)](docs/screenshots/communication.png)

This panel shows the bytes that crossed the network this round and in total
for the run, measured from the `payload_bytes` each node logs for every model
update. The current dashboard shows an engine chip (`GOSSIP`, `GADMM FP` or
`Q-GADMM b=2/4/8`) and compares what was sent against a 32-bit reference,
`4 x d x messages` (see [Honest accounting](#honest-accounting)). (Screenshot
from the gossip-baseline MVP, which sent plain float64 weights and had no
32-bit comparison or engine chip.)

### Audit log

![Audit log: one row per model update with sender, receiver, payload bytes and SHA-256 checksum](docs/screenshots/audit-log.png)

The audit log has one row for every `/update` a node receives. Each row shows
the timestamp, round, sender, receiver, payload size and SHA-256 checksum of
the payload. Run lifecycle events are logged here too, along with the ADMM
engines' `round_degraded`, `consensus_detected`, `snapshot_resync` (with its
byte cost) and `rejoined_from_checkpoint` events. (Screenshot from the
gossip-baseline MVP.)

### Risk assessment

![Risk assessment: patient form, LOW/MODERATE/HIGH tier, per-model confidence bars, risk factors and an ECG upload](docs/screenshots/risk-assessment.png)

A form for one patient's readings, scored by the hospital node you pick. The
result shows the risk tier, the blended probability, the RandomForest and
XGBoost confidences against the 30% decision threshold, and the critical,
moderate and lifestyle risk factors. **DOWNLOAD PDF REPORT** returns the same
assessment as a PDF. Below it, an ECG image upload is classified as normal,
myocardial infarction or abnormal heartbeat.

## Requirements

- **Docker** with the **Docker Compose v2** plugin (`docker compose ...`).
  Everything else runs inside containers: Python 3.12 with CPU-only PyTorch for
  the nodes, Node 22 for the dashboard build, and Postgres 16.
- **Free host ports:** `5432`, `8001`, `8002`, `8003` and `8080`.
- **Free container names:** `hospital-a`, `hospital-b`, `hospital-c`,
  `dashboard` and `medchain-postgres`. The compose file sets fixed
  `container_name`s, so only one copy of the stack can run on a machine at a
  time.
- **Internet access on the first build**, to pull the base images and install
  the pip and npm packages (the node image installs CPU-only PyTorch from the
  PyTorch wheel index). The data slices ship in `data/slices/`, so the dataset
  does not need to be downloaded. The first node build also trains the
  tabular risk model from `data/cardio/cardio_train.csv`, which adds about a
  minute.
- To run the tests or `experiments/compare_engines.py` outside Docker, you need
  **Python 3.12** and `pip install -r requirements-dev.txt`, which includes
  PyTorch and matplotlib (see [Tests](#tests)).

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
| `hospital-a` / `hospital-b` / `hospital-c` | 8001 / 8002 / 8003 | FastAPI node: `GET /health`, `GET /status`, `POST /round/start`, `POST /update`, `GET /snapshot`, `GET /metrics` (+ `POST /round/pause`, `/round/resume`, and the `POST /assess*` risk-assessment endpoints). All three run the same image, and the mounted `config/hospital-*.yaml` decides which hospital it is and which engine it runs. Each node mounts only its own data slice. |
| `postgres` (container `medchain-postgres`) | 5432 | Holds `nodes`, `runs`, `rounds`, `updates_meta`, `metrics` and `audit_logs`. |
| `dashboard` | 8080 | Observer UI. It reads Postgres and pushes snapshots and update events over `/ws`. |

`GET /snapshot` exists only for mirror resync after a restart: it returns a
node's quantized model snapshot, never data.

Check that a node is up. `/health`, `/status` and `/metrics` need no
authentication:

```sh
curl http://localhost:8001/health
curl http://localhost:8001/status
```

To start a training run directly against a node's API, send the shared
secret in the header. The demo secret is `shared_secret` in
`config/hospital-*.yaml`:

```sh
curl -X POST http://localhost:8001/round/start \
  -H 'Content-Type: application/json' \
  -H 'X-MedChain-Secret: medchain-demo-secret' \
  -d '{"rounds": 200}'
```

The node that gets the request floods it to its neighbours, so all three
hospitals start the same run. `rounds` is optional and defaults to the
configured round limit (`training.rounds`, 200). Q-GADMM stops earlier, on the
same round everywhere, once every edge's residual has stayed below epsilon for
3 rounds (about round 160 at b=2). A request without the header gets `401`.
The same header works for `POST /round/pause` and `POST /round/resume`.

Once the run finishes, you can read any node's per-round history:

```sh
curl http://localhost:8002/metrics
```

### 2. Open the dashboard

Open <http://localhost:8080> in a browser.

- Press **START TRAINING ROUND** in the top bar. The button changes to show
  `TRAINING: ROUND n OF 200`, and a **PAUSE** button becomes active.
- Use the top navigation (**TOPOLOGY**, **TRAINING MONITOR**,
  **COMMUNICATION**, **AUDIT LOG**) to watch the run live. With
  `engine: qgadmm`, watch the consensus panel ("disagreement between
  neighbours") collapse and the **CONSENSUS REACHED** pill fire, the network
  training loss fall, the communication counters fill with measured packet
  sizes, and envelopes fly along the chain on each `/update`.
- **RISK ASSESSMENT** is the separate per-patient tool described in step 3.

The per-hospital cards show each hospital's loss on its own held-out patients.
Hospitals A and B improve; hospital C (VA Long Beach, the most different slice)
ends slightly *worse* on its own held-out split than after its first local round,
because agreeing on one shared model costs it a little local fit. The network
training loss (all hospitals' training rows, weighted by patients) is the
objective the chain minimises, and it falls to the centralized optimum.

The dashboard only observes. Its buttons send a single request to one live
node, which floods it to the others, just like the `curl` above. The dashboard
never coordinates training, so stopping it does not affect a running round.

### 3. Assess a patient (optional)

Open **RISK ASSESSMENT** in the dashboard, pick a hospital node, enter the
readings and press **ASSESS RISK**. You can also call a node directly. The
`/assess` endpoints need the same `X-MedChain-Secret` header as `/round/*`:

```sh
curl -X POST http://localhost:8001/assess \
  -H 'Content-Type: application/json' \
  -H 'X-MedChain-Secret: medchain-demo-secret' \
  -d '{"age": 58, "gender": "male", "height": 172, "weight": 88, "ap_hi": 150, "ap_lo": 95,
       "cholesterol": 2, "gluc": 1, "smoke": true, "alco": false, "active": false}'
```

The response has `prediction`, `confidence` (the blended probability in %),
`rf_confidence`, `xgb_confidence`, `threshold`, `risk_tier`,
`critical_factors`, `moderate_factors`, `lifestyle_factors` and the model's
held-out metrics. `cholesterol` and `gluc` are 1 (normal), 2 (above normal) or
3 (well above normal).

| Endpoint | Body | Returns |
| --- | --- | --- |
| `POST /assess` | patient JSON as above | assessment JSON |
| `POST /assess/pdf` | patient JSON | PDF report |
| `POST /assess/ecg` | multipart field `ecg` (PNG/JPG of a 12-lead ECG printout) | class, confidence, per-class probabilities |
| `POST /assess/ecg/pdf` | multipart field `ecg` | PDF report |

```sh
curl -X POST http://localhost:8001/assess/ecg \
  -H 'X-MedChain-Secret: medchain-demo-secret' -F ecg=@my-ecg.jpg
```

How the models work:

- **Tabular ensemble** (`node/assess/`): the same feature engineering as
  HEALIGRAM (age groups, BMI and BMI category, pulse pressure, hypertension
  stage, two interaction terms), a class-balanced RandomForest and an
  isotonic-calibrated XGBoost, blended `0.4 * RF + 0.6 * XGB`. A blended
  probability of 30% or more is positive. Positives at 85% or more are
  **HIGH**, other positives are **MODERATE**, and the rest are **LOW**. It is
  trained on the Kaggle Cardiovascular Disease dataset (70,000 patients,
  general and international). The node image trains it at build time. Held-out
  ROC-AUC is about 0.80, and accuracy is about 68% at the 30% threshold.
- **ECG classifier**: an ImageNet-pretrained ResNet18 with a 3-class head,
  fine-tuned on the Mendeley ECG Images dataset (928 printouts from a
  cardiology institute in Multan, Pakistan; CC BY 4.0). It runs under ONNX
  Runtime, so nodes do not need torch. The trained model is committed as
  `models/ecg-resnet18.onnx`. To retrain it, run
  `pip install -r requirements-ecg-train.txt`, then
  `python3 data/prepare_ecg.py`, then `python -m node.assess.train_ecg`.

Neither dataset is Indian-sourced. Provenance and licenses are in
`data/README.md`. The output is a screening aid, not a diagnosis.

### Kill-and-rejoin demo (optional)

Start a run, then kill the middle hospital (the TAIL node) while it trains:

```sh
curl -X POST http://localhost:8001/round/start \
  -H 'Content-Type: application/json' \
  -H 'X-MedChain-Secret: medchain-demo-secret' \
  -d '{"rounds": 200}'
sleep 4
docker kill hospital-b
```

Within about 10 seconds, Hospital B's status dot turns orange and the
**TOPOLOGY** page reads `2 OF 3 NODES ALIVE`:

![Topology after docker kill hospital-b: B is down, A and C are alive (gossip-baseline MVP dashboard)](docs/screenshots/kill-topology.png)

Hospitals A and C (the heads) keep training. B is their only neighbour, so
their rounds are marked degraded and logged, and each keeps training on its own
data. B's curve stops at the round where it died:

![Training monitor after the kill: A and C reach later rounds, B stops and shows DOWN (gossip-baseline MVP dashboard)](docs/screenshots/kill-training-monitor.png)

The audit log records the failed sends and degraded rounds:

![Audit log after the kill: neighbour unreachable rows from hospital-a and hospital-c to hospital-b (gossip-baseline MVP dashboard)](docs/screenshots/kill-audit-log.png)

(These three screenshots are from the gossip-baseline MVP, where a restarted
node only joined the next run.)

Bring B back:

```sh
docker start hospital-b
```

With the ADMM engines, B reloads its last checkpoint, resyncs its mirrors from
its neighbours with one `GET /snapshot` each (audited as `snapshot_resync`,
with the byte cost) and rejoins the run at the current round; the residuals
resume falling to consensus. With `engine: gossip`, B's dot turns green again
and it joins the next run you start.

### Stop

```sh
docker compose down        # add -v to also delete the Postgres data and checkpoints
```

## Running on separate machines

The compose file above runs all three hospitals on one machine. The protocol does
not depend on that. A node needs exactly one thing from the network: an HTTP URL
for each of its chain neighbours, plus the URL its neighbours use to reach it.
Those URLs are configuration, not code, so the same image runs one hospital per
laptop with no code change.

**Use a private mesh network.** We recommend [Tailscale](https://tailscale.com) on
every hospital machine (any WireGuard-style overlay works). It connects machines
behind home and hospital NAT without port forwarding, it encrypts every packet
between them, and it gives each machine a stable name such as
`hospital-b.tail1234.ts.net`. MedChain adds no TLS of its own. Its authentication
is the shared-secret header (`X-MedChain-Secret`), and it relies on the network
layer for encryption, much as NVIDIA FLARE relies on TLS at the transport layer.
Do not expose node ports to the public internet. A kit's `--bind-ip` publishes the
port on one address only, for example the machine's Tailscale IP.

### Provision one kit per hospital

`scripts/provision.py` follows NVIDIA FLARE's "startup kit" idea. It writes one
self-contained folder (and optionally a zip) per hospital:

```sh
python scripts/provision.py --zip \
  --peers hospital-a=http://hospital-a.tail1234.ts.net:8001,hospital-b=http://hospital-b.tail1234.ts.net:8002,hospital-c=http://hospital-c.tail1234.ts.net:8003 \
  --observer-db postgresql://medchain:medchain@observer.tail1234.ts.net:5432/medchain   # optional
```

`--peers` lists every hospital in chain order, each with the address its
neighbours reach it at: a LAN IP, a Tailscale name or a domain. Addresses must be
`http://`; `https://` is rejected because the node serves plain HTTP. The command
writes `kits/hospital-a/`, `kits/hospital-b/` and `kits/hospital-c/`, plus a
`.zip` of each. Every kit holds:

- `config/hospital-X.yaml`, pre-filled with the real neighbour addresses, the chain
  and one freshly generated shared secret. Every kit from the same run gets the
  same secret. To provision a hospital later, pass that secret with `--secret`.
  Engine settings come from the repository's `config/hospital-X.yaml`.
- `docker-compose.hospital.yaml`, which runs only that node.
- `data/hospital-X.csv`, that hospital's slice. Pass `--data hospital-X=path.csv`
  to use different data.
- `source/`, the node's build context. With `--image <ref>`, the kit uses a
  prebuilt image instead.
- `README.md`, with the exact commands.

The kits contain the shared secret, so send each one privately to that
hospital's operator. The operator unpacks it and runs one command:

```sh
docker compose -f docker-compose.hospital.yaml up -d --build --wait
```

Start training from any one node (`POST /round/start`, as above). The start
floods along the chain.

**Changing an address without re-provisioning.** Any node config can be
re-pointed from the environment: `MEDCHAIN_PEER_<ID>_URL` replaces neighbour
`<ID>`'s URL, and `MEDCHAIN_ADVERTISE_URL` replaces the node's own address. `<ID>`
is the node id upper-cased, with `-` and other characters turned into `_`. For
example:

```sh
MEDCHAIN_PEER_HOSPITAL_A_URL=http://100.101.1.10:8001 \
MEDCHAIN_PEER_HOSPITAL_C_URL=http://hospital-c.tail1234.ts.net:8003 \
MEDCHAIN_ADVERTISE_URL=http://hospital-b.tail1234.ts.net:8002 \
NODE_CONFIG=config/hospital-b.yaml python -m node
```

`NODE_CONFIG` can also point at a separate copy of the YAML.

**The observer is optional.** The dashboard and its Postgres can run on any one
machine:

```sh
MEDCHAIN_SHARED_SECRET=<kit secret> docker compose up -d postgres dashboard
```

Nodes write telemetry to it only when their kit was provisioned with
`--observer-db`. It never coordinates training. A node with no observer still
trains and still serves `/status` and `/metrics`.

### Prove it

`scripts/verify_multihost.py` is the acceptance test, on real laptops or on one
machine. Point it at every node's real address. It checks that the nodes agree on
the chain, starts one run through the first node, waits for every hospital to
finish, and then reports what crossed the network: per hospital, the rounds
trained, neighbour updates heard on time, and consensus; per directed link (with
`--database-url`), packets delivered, packets received with a verified checksum,
and exact bytes. It exits non-zero if any check fails.

```sh
python scripts/verify_multihost.py --secret <kit secret> \
  --node http://hospital-a.tail1234.ts.net:8001 --node http://hospital-b.tail1234.ts.net:8002 \
  --node http://hospital-c.tail1234.ts.net:8003 \
  --database-url postgresql://medchain:medchain@observer.tail1234.ts.net:5432/medchain
```

We ran this on one machine as the stand-in for three laptops. The setup:

- Three kits, generated with this machine's LAN address (`192.168.0.101`) as
  every peer address.
- Each kit started as its own `docker compose` project, so each node had its own
  Docker network. From inside hospital-a, the names `hospital-b` and
  `medchain-hospital-b-hospital-b-1` did not resolve, so every packet went to
  `192.168.0.101:800x` through the host's network stack.
- Postgres and the dashboard ran as two more independent containers, reached
  through the same LAN address.

The full report is in [`docs/multihost/proof-report.json`](docs/multihost/proof-report.json):

```
run run-20261002-135941-99a0  engine Q-GADMM (b=2)
hospital     reached at                   rounds     heard degraded  loss first->final    acc  residual consensus
hospital-a   http://192.168.0.101:8001       163  163/163         0     0.5303->0.4325  0.789  8.54e-04       159
hospital-b   http://192.168.0.101:8002       163  326/326         0     0.4750->0.3259  0.865  8.54e-04       159
hospital-c   http://192.168.0.101:8003       163  163/163         0     0.5529->0.5584  0.660  6.89e-04       159

link                         to address                   delivered verified    bytes  float64
hospital-a -> hospital-b     http://192.168.0.101:8002     163/163       163     1956    20864
hospital-b -> hospital-a     http://192.168.0.101:8001     163/163       163     1956    20864
hospital-b -> hospital-c     http://192.168.0.101:8003     163/163       163     1956    20864
hospital-c -> hospital-b     http://192.168.0.101:8002     163/163       163     1956    20864
PASS: every hospital trained and exchanged parameters with its neighbours over the network
```

- **Every link carried traffic.** Every hospital heard every neighbour in every
  round, and each of the four directed links carried 163 checksum-verified
  updates.
- **The chain converged.** The edge residuals fell below epsilon (1e-3), and all
  three nodes stopped together at round 163, after reaching consensus at round
  159.
- **Dashboard start works too.** A second run started from the dashboard's START
  button (a fourth process relaying to `http://192.168.0.101:8001`) passed the
  same checks.

This is still one physical machine. Packets crossed real sockets on a real LAN
address, between nodes that share no Docker network, but not a wire between two
laptops. On real hardware, the same `verify_multihost.py` run is the test to
pass. CI runs a smaller version on every push: `tests/unit/test_multihost.py`
launches three separate `python -m node` processes bound to the runner's own
network address. Their YAML peer URLs are unresolvable placeholders, and the real
addresses arrive only through `MEDCHAIN_PEER_<ID>_URL`. The verifier must then
pass.

## Engines

`engine:` in `config/hospital-*.yaml` (same value on every node) picks one:

| `engine:` | Label in UI and logs | What travels per message (d = 16 parameters) |
| --- | --- | --- |
| `qgadmm` | `Q-GADMM (b=2)` (`bits:` 2, 4 or 8) | `ceil(b*d/8) + 8` bytes: b-bit stochastic quantization of `theta - theta_hat`, plus `b` and `R` |
| `gadmm_fp` | `GADMM full precision (float32 deltas)` | `4*d + 8` bytes: the same algorithm with quantization skipped |
| `gossip` | `naive gossip baseline` | `8*d` bytes: the raw float64 weights, averaged with the neighbours' |

Switch engines by editing `engine:` in the three configs and recreating the
nodes (`docker compose up -d --force-recreate hospital-a hospital-b hospital-c`).
The ADMM settings sit next to it: `rho` (24), `alpha` (1.0), `bits` (2),
`local_epochs` (50 full-batch Adam steps per primal step), `lr` (0.01),
`epsilon` (1e-3), `checkpoint_every` (5), `seed` (42, plus the chain position),
and `chain` (the node order 1..N). The gossip baseline ignores them.

### How Q-GADMM runs on the chain (`node/qgadmm/`)

- `state.py`: chain positions (odd = HEAD, even = TAIL; the boundary nodes 1 and N
  have one neighbour, so one mirror, one dual variable and one penalty term), per-node
  state (`theta`, the quantized self-snapshot `theta_hat`, mirrors of the neighbours'
  snapshots, one `lambda` per edge, all starting at zero) and atomic checkpoints.
- `quantizer.py`: `delta = theta - theta_hat`, `R = ||delta||_inf` (0 sends a
  "no change" packet), grid step `2R/(2^b - 1)`, per-coordinate stochastic rounding
  with a seeded `numpy.random.Generator`, `packbits` on the wire; reconstruction
  (Eq. 13) `theta_hat + Delta*q - R` is applied by the sender to its own snapshot and
  by the receiver to its mirror, so both stay bit-identical.
- `engine.py`: the primal step (Eq. 14/16) runs E epochs of Adam on
  `local_loss(theta)` (logistic regression, `BCEWithLogitsLoss(reduction="sum")`) plus
  `lambda^T(gap) + rho/2 ||gap||^2` for each edge the node has; the dual step (Eq. 18)
  is `lambda += alpha*rho*(theta_hat_left - theta_hat_right)`, local, no messages.
- `scheduler.py`: each round the heads rebuild mirrors from pending packets, take their
  primal step, quantize and `POST /update` both neighbours; each tail waits for both
  heads (barrier; on timeout the round is marked degraded, the stale mirror is reused
  and `round_degraded` is audited), then does the same; then every node takes its dual
  step and logs the per-edge primal residual `||theta_hat_n - theta_hat_{n+1}||_2` to
  `metrics.residuals`. An edge takes its dual step only when both directions of that
  round's exchange were on time, which each end knows from the `/update` responses, so
  both copies of `lambda` stay identical when a neighbour is slow or dies.
- Every packet carries fingerprints of the snapshot it was built on and produces; a
  receiver whose mirror does not match resyncs from the sender's `GET /snapshot`
  (model state only - `theta_hat`, its mirror of the requester and the edge's
  `lambda` - never data) and audits `snapshot_resync` with its byte cost.

## Honest accounting

- `payload_bytes` in `updates_meta` is the exact HTTP body the node sent (headers
  not counted); `full_precision_bytes` is the same d parameters as float64.
- The communication panel compares against a 32-bit reference `4 x d x messages`.
  At this project's model size (d = 16) a b=2 packet is 12 bytes against 64, i.e.
  **5.3x** less, not 16x: the 8-byte `(b, R)` header dominates. The coordinates alone
  are 16x smaller (shown, labelled as such), and the per-packet ratio approaches 16x
  for larger models (`experiments/out/payload_ratio.png`).
- The gossip baseline sends float64, twice the 32-bit reference.

## Compare the engines

```sh
pip install -r requirements-dev.txt
python experiments/compare_engines.py
```

Runs gossip, full-precision GADMM and Q-GADMM (b = 8, 4, 2) headless on the same
three hospital slices with the production engine code and writes to
`experiments/out/`: `compare_engines.png` (loss vs cumulative bits transmitted,
one curve per engine), `rho_sweep.png` (Q-GADMM b=2 at rho = 5, 15, 24, 50),
`payload_ratio.png` and `summary.json`. On this data every ADMM variant reaches
the centralized optimum; Q-GADMM b=2 does it in 7.6 kB against gossip's 102 kB
after 200 rounds (and gossip's hospitals still disagree). At d = 16, b=8 needs
even fewer total bytes than b=2 because it reaches consensus in 35 rounds instead
of 159.

## How MedChain differs from Flower and NVIDIA FLARE

[Flower](https://github.com/adap/flower) and
[NVIDIA FLARE](https://github.com/NVIDIA/NVFlare) are both hub-and-spoke. In
Flower, every client (a SuperNode) connects to one central SuperLink server,
which selects clients and aggregates their updates. NVIDIA FLARE likewise has
one server and per-site clients, with mutual-TLS certificates from a
provisioning step that hands each site a startup kit.

| | Flower / NVIDIA FLARE | MedChain |
| --- | --- | --- |
| Topology | Central server; every site connects to it | No server. Each hospital talks only to its chain neighbours (A - B - C) |
| How the model is combined | The server aggregates the site updates (e.g. FedAvg) | Q-GADMM decentralized consensus: each node solves its local ADMM step and exchanges quantized differences with its neighbours |
| Who sees updates | The server receives every site's update (unless secure aggregation is added) | No node receives every site's update; each gets only its neighbours' quantized differences |
| What goes on the wire | Model weights or gradients, full precision by default | b-bit stochastic quantization of the change since the last snapshot |
| Starting a run, observing | The server drives the rounds | A start request floods from whichever node gets it; the dashboard only observes |
| Site setup | NVIDIA FLARE: one startup kit per site | One kit per hospital (`scripts/provision.py`), the idea borrowed from NVIDIA FLARE |

**Measured bandwidth.** The model has d = 16 parameters. Each Q-GADMM b=2 packet
is 12 bytes, against 64 bytes at 32 bits (5.3x less) and 128 bytes at float64
(10.7x less). Over a whole run, Q-GADMM b=2 reaches the centralized optimum
with 7.6 kB on the wire. The gossip baseline sends 102 kB in 200 rounds and its
hospitals still disagree (`experiments/out/summary.json`). The cross-address
run above moved 7,824 bytes across its four links, against 83,456 bytes for the
same 652 messages at float64.

**Trade-offs.** The hub frameworks are mature and scale to many sites, with
client sampling, many aggregation strategies and certificate-based identity.
MedChain is a three-node prototype. A chain also has weak points: if the middle
hospital fails, the chain splits in two, and the survivors keep training on
their own data until it rejoins.

## How it fits together

- **One node image, config decides identity** (`config/hospital-*.yaml`: node_id,
  neighbours, chain, port, engine settings, data_slice). The topology is a chain
  A - B - C. Peer addresses can be overridden from the environment (see
  [Running on separate machines](#running-on-separate-machines)).
- **Peer-to-peer rounds**: a start request floods from the node that receives it;
  the gossip baseline's loop is `node/runner.py`, the ADMM engines' head/tail loop is
  `node/qgadmm/scheduler.py`. Unreachable neighbours are skipped, so survivors keep
  training.
- **Swappable engine** (`node/engine.py`, registry in `node/engines.py`):
  `train_round()`, `package_update()`, `apply_neighbor_update()`, `evaluate()`.
- **Checkpoints**: the ADMM engines pickle their state every `checkpoint_every`
  rounds to a per-node docker volume (`/checkpoints`); a restarted node rejoins its
  run from there.
- **Shared secret**: `/update`, `/snapshot`, `/round/*` and `/assess*` require the
  `X-MedChain-Secret` header to match `shared_secret` in the node config (one demo
  value for all nodes; the dashboard gets it via `MEDCHAIN_SHARED_SECRET`), otherwise
  401. `/health`, `/status` and `/metrics` stay open.
- **Observer dashboard**: it never coordinates training. Its buttons relay a
  single start/pause request to one node, which floods it to the others; it learns
  about each `/update` from the `updates_meta` rows the nodes write. Killing the
  dashboard does not affect a run (tested).
- **Risk assessment is separate from training** (`node/assess/`): its models
  are trained centrally, not by the training engines. The dashboard relays each
  request to the node you pick and stores nothing. `/status` reports whether
  each model is loaded.

## Layout

```
docker-compose.yml   config/        node/          dashboard/
design/              assets/svg/    data/          tests/        experiments/
docs/screenshots/    models/        scripts/
```

- `scripts/provision.py` writes the per-hospital kits, and
  `scripts/verify_multihost.py` checks a run across real addresses (see
  [Running on separate machines](#running-on-separate-machines)).
  `docs/multihost/proof-report.json` is the report from the single-machine
  stand-in run.

- `design/STYLE.md` and `design/tokens.css` are the Pa'lais style reference,
  adapted.
- `assets/generate_svgs.py` regenerates `assets/svg/`. `assets/preview.html`
  shows every asset on the cream canvas.
- `data/prepare_uci.py` re-downloads UCI Heart Disease and rebuilds the three
  non-IID slices (Cleveland, Hungarian, VA Long Beach). See `data/README.md`.
- `data/cardio/` holds the Kaggle CSV the tabular risk model trains on, and
  `data/prepare_ecg.py` fetches the ECG images. `models/` holds the exported
  ECG model. `python -m node.assess.train_tabular` writes the tabular model
  to `models/heart-risk.joblib` for local runs; that file is gitignored.

## Tests

```sh
pip install -r requirements-dev.txt
pytest tests/unit                                   # no docker needed
docker compose up -d --build --wait
MEDCHAIN_STACK=1 pytest tests/integration -v        # real runs, real docker kill
```

`tests/unit` covers quantizer unbiasedness, sender/receiver round-trip
determinism (exact float equality over 50 rounds), the boundary nodes' single
penalty/dual term, convergence sanity (N = 5 synthetic linear regression: Q-GADMM
b=2 within 1% of full-precision GADMM and 2% of centralized scikit-learn), and a
three-node chain of real node apps over HTTP in one process, including a tail
kill and checkpoint rejoin, plus the risk-assessment endpoints (JSON and PDF
reports for the tabular and ECG models). It also covers the peer-address environment
overrides, the provisioning kit's files and substituted addresses, and three nodes
as separate OS processes on the host's network address, checked by
`scripts/verify_multihost.py`. `tests/integration` runs against whichever engine the
configs select; CI (`.github/workflows/ci.yml`) runs it for `qgadmm`, `gadmm_fp`
and `gossip`, plus the unit tests, the dashboard build, the docker build and a
headless `compare_engines.py` run.
