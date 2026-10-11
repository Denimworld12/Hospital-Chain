# MedChain

MedChain is a simulation of decentralized collaborative learning for hospitals.
A chain of hospital nodes trains one heart-disease model together on real UCI
data. The chain can be any length, odd or even: the demo stack starts three
hospitals (A - B - C), a fourth (D, the Swiss hospitals' data) can join while a
run is going, and a hospital can leave on purpose with its two neighbours
bridging the gap. Each hospital
trains only on its own patients, and only model updates travel, between chain
neighbours, over TLS, each one signed by its sender. Patient rows never leave the
node that holds them. No server ever sees or coordinates training; a small
directory service decides chain membership (who joins where, who bridges when
someone leaves) and nothing else. See [Joining and leaving](#joining-and-leaving).

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
one was taken while a real training round ran through the dashboard. Most were
captured on the earlier three-hospital gossip-baseline MVP dashboard, so they
predate the consensus panel, the network-loss panel, the engine chip, the
32-bit comparison and dynamic membership described below.

### A hospital joins, another leaves (live)

These four are from the dynamic-membership dashboard, on this repository's
compose stack (TLS, signed updates, the directory), during two Q-GADMM b=2 runs.
They were captured when the stack started four hospitals and `hospital-e` was
the joiner. Today it starts A - B - C and `hospital-d` joins.

1. Four hospitals were training (A - B - C - D) when `hospital-e` was started. The
   directory appended it from round 24 (membership epoch 11): every member
   switched on that round, and D and E each fetched the other's full snapshot
   (384 bytes each way) before any quantized packet crossed the new link. The
   five-hospital chain reached consensus at round 182.

![Topology after hospital-e joined: five hospitals in chain order, alternating head and tail](docs/screenshots/dynamic-join-topology.png)

2. On the next run, `hospital-b` announced a planned departure at round 21
   (`python -m node.leave`). From round 24 (epoch 12) A and C bridged with a new
   link (again one full snapshot each way), C and D flipped from head/tail to
   tail/head, and B stopped after round 23. The chain A - C - D - E reached
   consensus at round 200, with the network training loss settling at 0.447
   (the centralized optimum for those four hospitals' data is 0.4475). Neither
   run had a single degraded round.

![Topology after hospital-b left: A - C - D - E, roles flipped after the gap, B listed as left the chain](docs/screenshots/dynamic-leave-topology.png)

The consensus panel shows only the current links (A - C, C - D, D - E), says
when the chain changed and that the consensus check restarted, and the network
loss now averages over the four hospitals in the chain:

![Training monitor after the leave: three pair lines falling below epsilon, consensus reached, network loss over 4 hospitals](docs/screenshots/dynamic-training-monitor.png)

Bytes per round step down from 8 packets to 6 at round 24, when the chain went
from five hospitals (four links) to four (three links):

![Communication page after the leave: 4 hospitals, 3 links, bytes per round drop at round 24](docs/screenshots/dynamic-communication.png)

The same sequence (the starting hospitals, D joining, a middle hospital leaving,
consensus, and each dashboard page following it) is an automated test against
the live stack (`tests/integration/test_stack.py`).

### Topology

![Network topology: three hospital nodes in a chain, all alive (gossip-baseline MVP dashboard)](docs/screenshots/topology.png)

This shows the hospitals in chain order. Each node has a status dot: green means
the node sent a heartbeat recently, and orange means it is down. Under each
hospital is its patient count and the last round it finished. The current
dashboard draws whatever chain the nodes report (any number of hospitals, in
chain order, each labelled with its position and head/tail group), lists
hospitals that left the chain separately, shows the membership epoch, and
animates an envelope along an edge on each `/update`. (Screenshot from the
three-hospital gossip-baseline MVP.)

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
- **Free host ports:** `5432` (on loopback), `8001` to `8005`, `8080`, `8100`
  and `8200`.
- **Free container names:** `hospital-a` to `hospital-e`, `directory`,
  `observer`, `dashboard`, `medchain-pki` and `medchain-postgres`. The compose file sets
  fixed `container_name`s, so only one copy of the stack can run on a machine at
  a time.
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

The system has two parts: the **backend** (three hospital nodes, the membership
directory, the observer that takes the nodes' telemetry, and Postgres) and the
**dashboard** (an observer UI). One compose file starts both.

### 1. Start the backend

From the repository root:

```sh
make up              # = docker compose up -d --build --wait
docker compose ps
```

`--wait` returns when every service is healthy. These services come up:

| Service | Host port | What it is |
| --- | --- | --- |
| `pki` (container `medchain-pki`) | - | Runs once on start: `scripts/pki.py` issues the demo network CA, each node's, the directory's and the observer's TLS certificate, each node's Ed25519 update-signing key and each hospital's directory credential into `./.pki/` (gitignored). Later starts keep what exists. |
| `directory` | 8100 | The membership directory (`directory/`): decides where a hospital sits in the chain and tells hospitals who to connect to when one joins or leaves. Its own SQLite store, not Postgres. See [The directory](#the-directory). |
| `hospital-a` ... `hospital-d` | 8001 ... 8004 | FastAPI node over HTTPS: `GET /health`, `GET /status`, `POST /round/start`, `POST /update`, `GET /snapshot`, `GET /metrics`, `GET`/`POST /membership`, `POST /membership/leave` (+ `POST /round/pause`, `/round/resume`, and the `POST /assess*` risk-assessment endpoints). All run the same image, and the mounted `config/hospital-*.yaml` decides which hospital it is and which engine it runs. Each node mounts only its own data slice. `hospital-d` (8004, the Swiss data) is the mid-demo joiner and only starts on request (profile `join`). `hospital-e` (8005) is a spare (profile `spare`). |
| `observer` | 8200 | Takes the nodes' telemetry (`POST /ingest`, over HTTPS with its token; `observer/`) and writes it to Postgres with the same schema. Nodes push to it from a background queue, so training never waits for it. Nodes on other machines need only its URL and token, never Postgres credentials. |
| `postgres` (container `medchain-postgres`) | 5432, loopback only | Holds `nodes`, `runs`, `rounds`, `updates_meta`, `metrics` and `audit_logs`. Only the observer writes it and only the dashboard reads it. |
| `dashboard` | 8080 | Observer UI. It reads Postgres and pushes snapshots and update events over `/ws`. |

On start each node registers with the directory, which places the three
hospitals in its configured order, A - B - C: positions 1 and 3 are the HEAD
group, 2 the TAIL group. `GET /snapshot` exists only for a full resync
(after a restart, a mirror mismatch or a new link): it returns a node's quantized
model snapshot, never data.

Every node serves HTTPS with a certificate from the stack's own CA, so point
`curl` at it. `/health`, `/status` and `/metrics` need no authentication:

```sh
curl --cacert .pki/ca.crt https://localhost:8001/health
curl --cacert .pki/ca.crt https://localhost:8001/status     # chain, position, group, neighbours, epoch
```

To start a training run directly against a node's API, send the shared
secret in the header. The demo secret is `shared_secret` in
`config/hospital-*.yaml`:

```sh
curl --cacert .pki/ca.crt -X POST https://localhost:8001/round/start \
  -H 'Content-Type: application/json' \
  -H 'X-MedChain-Secret: medchain-demo-secret' \
  -d '{"rounds": 400}'
```

The node that gets the request floods it to its neighbours, so every hospital
in the chain starts the same run. `rounds` is optional and defaults to the
configured round limit (`training.rounds`, 400). Q-GADMM stops earlier, on the
same round everywhere, once every edge's residual has stayed below epsilon for
3 rounds (about round 70 for this three-hospital chain at b=2; a longer chain
takes longer, which is expected). A request without the header gets `401`. The
same header works for `POST /round/pause` and `POST /round/resume`.

Once the run finishes, you can read any node's per-round history:

```sh
curl --cacert .pki/ca.crt https://localhost:8002/metrics
```

### 2. Open the dashboard

Open <http://localhost:8080> in a browser and log in. The dashboard has one admin
account and shows nothing, not even the live snapshot, to anyone without its session
(`dashboard/server/auth.js`). The account (user `admin`) is created on first run from
`MEDCHAIN_ADMIN_PASSWORD` (at least 10 characters): set it before `make up`, or in
`hub.env` for `make hub`. Without it nobody can log in, and the login screen says to
set it and restart the dashboard.

The password is stored as a bcrypt hash. The session is an httpOnly, SameSite=Strict
cookie (12 hours; **LOG OUT** in the top bar ends it), and Postgres keeps only its
SHA-256. Five wrong passwords from one address lock it out for 5 minutes. **JOIN CODES**
(admin) creates and revokes the one-time codes hospitals enroll with (see
[Join with a code instead of a kit](#join-with-a-code-instead-of-a-kit)). **NODES**
shows each hospital's signed proof of origin.

- Press **START TRAINING ROUND** in the top bar. The button changes to show
  `TRAINING: ROUND n OF 400`, and a **PAUSE** button becomes active.
- Use the top navigation (**TOPOLOGY**, **TRAINING MONITOR**,
  **COMMUNICATION**, **AUDIT LOG**) to watch the run live. With
  `engine: qgadmm`, watch the consensus panel ("disagreement between
  neighbours") collapse and the **CONSENSUS REACHED** pill fire, the network
  training loss fall, the communication counters fill with measured packet
  sizes, and envelopes fly along the chain on each `/update`.
- **RISK ASSESSMENT** is the separate per-patient tool described in step 3.

The per-hospital cards show each hospital's loss on its own held-out patients.
Some hospitals improve; a hospital whose patients differ most from the rest can
end slightly *worse* on its own held-out split than after its first local round,
because agreeing on one shared model costs it a little local fit. The network
training loss (all chain members' training rows, weighted by patients) is the
objective the chain minimises, and it falls to the centralized optimum.

The dashboard only observes. Its buttons send a single request to one live
node, which floods it to the others, just like the `curl` above. The dashboard
never coordinates training, so stopping it does not affect a running round.

### 3. Assess a patient (optional)

Open **RISK ASSESSMENT** in the dashboard, pick a hospital node, enter the
readings and press **ASSESS RISK**. You can also call a node directly. The
`/assess` endpoints need the same `X-MedChain-Secret` header as `/round/*`:

```sh
curl --cacert .pki/ca.crt -X POST https://localhost:8001/assess \
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
curl --cacert .pki/ca.crt -X POST https://localhost:8001/assess/ecg \
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

This is the *unplanned* case: a hospital that dies without notice. (A planned
departure is different; see [Joining and leaving](#joining-and-leaving).) Start a
run, then kill hospital B (position 2, a TAIL node) while it trains:

```sh
curl --cacert .pki/ca.crt -X POST https://localhost:8001/round/start \
  -H 'Content-Type: application/json' \
  -H 'X-MedChain-Secret: medchain-demo-secret' \
  -d '{"rounds": 400}'
sleep 4
docker kill hospital-b
```

Within about 10 seconds, Hospital B's status dot turns orange and the
**TOPOLOGY** page reads `2 OF 3 IN THE CHAIN ALIVE`. B stays in the chain: a
crash is not a departure, so nothing is rewired. (These screenshots are from the
three-hospital MVP.)

![Topology after docker kill hospital-b: B is down, A and C are alive (gossip-baseline MVP dashboard)](docs/screenshots/kill-topology.png)

Hospitals A and C (B's neighbours, both heads) keep training. Their rounds are
marked degraded and logged: each has only B as a neighbour, so each trains on its
own data. The chain is split around the gap until B comes
back. B's curve stops at the round where it died:

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

## Joining and leaving

The chain is not fixed. Hospitals join and leave while the system keeps
running, and the model state is handled, not just the connections.

**Chain of N, not a ring.** Any number of hospitals, odd or even: positions
1, 3, 5, ... are the HEAD group and 2, 4, ... the TAIL group, so every link joins
a head to a tail and Q-GADMM's two-phase schedule (heads, then tails) holds at
every length. A ring is deliberately not used: it only splits into two
alternating groups when the hospital count is even.

**Only at a round boundary.** Every membership change is a numbered *epoch* with
the round it takes effect at; every member switches on that same round, never
mid-round. A new link starts at `lambda = 0`; a removed link's `lambda` (and
mirror) is discarded; links that survive keep theirs. Because a quantized update
is decoded relative to the shared `theta_hat`, a new neighbour cannot decode
anything until it has the other side's full snapshot, so both ends of a new link
first exchange one full-precision snapshot (`GET /snapshot`, the same resync
rule a rejoin uses, audited as `snapshot_resync` with its byte cost), and no
quantized packet crosses the link before that. Consensus detection restarts on
the new chain: a stop agreed on the old chain is dropped, and the dashboard's
**CONSENSUS REACHED** pill resets.

**Join (append-only).** A new hospital registers with the directory. While a run
is going, the directory appends it at the end of the chain, from a round
boundary a few rounds ahead of the furthest member (`margin_rounds`), and tells
every member. Appending is the only placement mid-run: inserting a hospital in
the middle would flip the head/tail role of every hospital after it. The new
node learns the run from its neighbour, starts from that neighbour's published
model, and trains with the others from that round. In the demo stack:

```sh
docker compose --profile join up -d hospital-d      # A - B - C  ->  A - B - C - D
```

**Planned leave (chain repair).** A leaving hospital asks the directory to take
it out at the next boundary and tells its two neighbours directly; they bridge
to each other with a new link (`lambda = 0`, snapshot exchange, as for a join),
and the chain heals instead of splitting into two dead ends. Hospitals after the
gap move up one position, so their head/tail roles flip, all on the same round.

```sh
docker compose exec hospital-b python -m node.leave  # A - B - C - D  ->  A - C - D
```

**A crash is not a departure.** A hospital that dies without notice stays in
the chain: its neighbours skip it and reuse its last mirror until it comes back
and rejoins from its checkpoint (the kill-and-rejoin demo above). Only a planned
leave rewires the chain.

Where a hospital is placed affects how fast the chain agrees: a longer chain
needs more rounds (in the committed experiments, consensus takes about 69
rounds for three hospitals and about 160 for five at b=2). That is expected, not
a bug. The ADMM penalty is `rho: 5`, which reaches consensus at b=2 on every
chain length tried (3, 4 and 5 hospitals); the old `rho: 24` stalls just above
epsilon on four or more.

### The directory

`directory/` is a small FastAPI service with three membership calls: `POST /register`,
`POST /leave` and `GET /membership` (plus `POST /enroll` for join codes, below). Be clear about what it is: it **does
coordinate membership**. It decides where a new hospital goes in the chain and
tells the existing hospitals who to connect to, so it is more than a phone
book. It is a **single point of failure for joins and leaves, not for
training**: nothing in a node's round loop calls it, so if it goes down, the
hospitals already in the chain keep training uninterrupted (tested), and only
joining and leaving wait for it to come back.

A hospital that **restarts** while the directory is down (a crash, a laptop
reboot) can still rejoin. Every node saves the directory-signed membership
documents it holds to its data directory (`data_path`, default the checkpoint
directory). The chain epoch in them goes up on every join, leave (bridge) and
re-registration. On restart, if the directory is unreachable, the node offers
its saved chain to its saved neighbours (`POST /membership/rejoin`, over pinned
TLS). A neighbour accepts only if:

- the epoch it presents is the neighbour's own latest epoch, so the chain has not
  changed; and
- the node connects with the TLS key on record for it.

If either check fails, the node waits for the directory, as before, and never
trusts stale state (tested both ways). In the Step 0 reproduction (A-B-C, the
directory stopped and B killed and restarted at round 12), A used to run to
the 400-round limit with 381 rounds degraded. It now reaches consensus at
round 86 with 17 degraded.

- It never sees model weights and never runs or influences training. Members'
  public `GET /status` (run id, round, agreed stop) is all it reads, to pick the
  round boundary for a change.
- Its storage is its own SQLite file (the `directory-data` volume): every
  membership document it issued, and a log of registrations, joins, leaves and
  failed authentications. It is separate from the Postgres the dashboard reads,
  so "the dashboard is not a coordinator" stays true: the dashboard draws the
  chain from what each node reports about itself (`nodes.epoch`, `chain`,
  `in_chain`) and never talks to the directory.
- Registration needs the hospital's own pre-shared credential
  (`Authorization: Bearer ...`); the directory stores only its SHA-256 hash, in
  `hospitals.yaml`, which also gives the placement order between runs. A wrong
  credential, or one hospital's credential used to register as another, is
  refused, so nothing else on the network can join the chain.
- Every membership document is signed with the directory's Ed25519 key. Nodes
  verify the signature whoever delivers it (the directory, the leaving hospital,
  or a neighbour that has a newer epoch), so changes can travel peer to peer and
  nobody can forge one.

### Security between nodes

A checksum only catches accidental corruption: someone who controls the wire can
change the bytes and recompute it. That was acceptable inside one Docker network;
it is not once hospitals run on separate machines. So:

- **TLS on every link, pinned to identity.** Nodes and the directory serve HTTPS
  with certificates from the network CA. Certificate verification stays on, but a
  peer is identified by its certificate's key, not by the address it was dialled
  at (`node/pinning.py`). Each hospital registers its key's SHA-256 pin with the
  directory. The registration uses its own kit credential and must connect with
  that very certificate. The directory publishes the pin in its signed membership
  documents. The directory's own pin comes from each hospital's kit
  (`directory.tls_pin`), never from the directory.
  - **The dialler** checks the server's key right after the handshake, before
    sending anything.
  - **The receiver** asks for a client certificate and checks it against the pin
    of the hospital the caller claims to be, on `/update`, `/snapshot` and
    `GET /membership`.
  - **On a mismatch** the connection is refused with a message that says what to
    do (e.g. "hospital-b's TLS key changed ... re-register it with the directory").
    It is audited as `tls_pin_mismatch` and never falls back to checking the
    hostname.
  - **When the directory is down**, the pins still work: they come from the signed
    documents each node already holds.
  - **Plain HTTP** is not served at all. The dashboard's relay still checks node
    certificates by address.
- **Signed updates.** Every `/update` carries an Ed25519 signature over the
  body's SHA-256 and every `X-MedChain-*` header that steers training (run,
  round, sender, receiver, sequence, snapshot fingerprints, residual view, stop
  proposal, epoch). The receiver checks it with the sender's public key, which
  it learned from the directory's signed membership document. A tampered packet
  (even with a recomputed checksum), an injected header, a packet signed by
  another key, one replayed to another node, or one from a sender with no key on
  record is rejected and audited as `update_rejected` (tested). `GET /snapshot`
  responses are signed the same way, since a snapshot replaces a mirror
  wholesale.
- The shared secret (`X-MedChain-Secret`) still guards `/round/*`,
  `/snapshot`, `GET /membership`, `/membership/leave` and `/assess*`.

The demo issues everything itself (`scripts/pki.py`: a self-signed CA,
certificates, signing keys, credentials). **For a real deployment**, issue the
node and directory TLS certificates from the hospitals' own PKI (or an
enterprise CA they trust) and keep any CA key offline; generate each signing key
on the hospital's own machine and register only its public half; deliver each
directory credential out of band and rotate it like a password; restrict the
node and directory ports to the hospitals' private network (a mesh such as
Tailscale is still worth it for NAT traversal). Also consider requiring a client
certificate on every endpoint. Today `/round/*` and `/assess*` also accept the
dashboard, which has no certificate, on the shared secret alone.

## Running on separate machines

The compose file above runs every hospital on one machine. The protocol does not
depend on that. A node needs an HTTPS address its neighbours and the directory
can reach it at, and the directory's address; its neighbours' addresses arrive
from the directory. Those addresses are configuration, not code, so the same
image runs one hospital per laptop with no code change.

**Configuration from the environment.** Every node setting can come from the
environment, which takes precedence over the YAML named by `NODE_CONFIG` (now
optional), which takes precedence over the defaults (`node/settings.py` lists
them all). The main ones are:
`NODE_ID`, `BIND_HOST`, `NODE_PORT`, `ADVERTISE_ADDR`, `DIRECTORY_URL`,
`OBSERVER_URL`, `DATA_PATH`, `ALGORITHM`, `BITS`, `EPSILON`, `ROUND_TIMEOUT_S`
and `IMAGE_VERSION`. The keys and secrets come in as `TLS_CERT`, `TLS_KEY`,
`TLS_CA`, `SIGNING_KEY`, `DIRECTORY_CREDENTIAL_FILE`, `DIRECTORY_PUBLIC_KEY`,
`DIRECTORY_TLS_PIN`, `SHARED_SECRET` and `OBSERVER_TOKEN`.
- `ADVERTISE_ADDR` (`host`, `host:port` or a URL) is required with a directory.
  A node never falls back to its id as a hostname.
- One `NODE_PORT` sets both the bind port and the advertised port.
- The `MEDCHAIN_*` names below still work as aliases.

**Telemetry goes to the observer.** Nodes push their writes to `OBSERVER_URL`
in batches, with its token. Each node keeps a bounded queue that drops the
oldest writes when full and is never awaited by a round. Each node also keeps
its own copy of every audit event in `DATA_PATH/audit.db` (SQLite). The
single-machine stack and the kits from `scripts/provision.py` use this same
path.

**Preflight.** On each machine, `python scripts/preflight.py --node
https://localhost:<port> --ca pki/ca.crt` lists the node's neighbours and
reports, for each, whether this machine reaches it directly or through a
Tailscale DERP relay. A relay is allowed as a fallback: it is still end-to-end
WireGuard between the two machines, but slower. The report also says whether
each neighbour answers.

**Use a private mesh network.** We recommend [Tailscale](https://tailscale.com) on
every hospital machine (any WireGuard-style overlay works). It connects machines
behind home and hospital NAT without port forwarding and gives each machine a
stable name such as `hospital-b.tail1234.ts.net`. MedChain encrypts and
authenticates its own traffic (TLS plus signed updates), so the mesh is for
reachability, not for security. Do not expose node ports to the public internet.
The hub listens for the hospitals on `HUB_BIND_IP` (its `hub.env`), for example
the hub machine's Tailscale IP.

**Step by step for a friend's laptop:** [`docs/MULTI_LAPTOP.md`](docs/MULTI_LAPTOP.md)
is the guide for someone who has never used Docker: installing Docker Desktop and
Tailscale, the kit, the preflight, the hotspot fallback and a troubleshooting table.

### The deployment: one file per laptop

- `deploy/node.yml` runs one hospital node: a single `node` service from the
  published image `ghcr.io/denimworld12/medchain-node:${IMAGE_VERSION}`, configured by
  the `.env` next to it. It mounts `./data` at `DATA_PATH` (the slice, checkpoints,
  saved membership, `audit.db`), `./pki` read-only and `./config` (the engine settings).
  It publishes `NODE_PORT` on every interface. It has no `container_name` and relies on
  no Compose DNS: every address comes from `.env` and the directory.
- `deploy/hub.yml` runs the organiser's laptop: the directory, the observer, Postgres
  (on `127.0.0.1` only) and the dashboard.
- `deploy/node.env.example` and `deploy/hub.env.example` list their settings, each
  with a Tailscale block and a hotspot block that differ only in addresses.
- `make node KIT=kits/hospital-b` and `make hub` run them from a repository checkout.
  `make node-native KIT=...` runs the same node without Docker, in its own venv
  (`.venv-node`, from `node/requirements.txt`), with the kit's `.env` and folders.
- `.github/workflows/image.yml` builds the image for `linux/amd64` and `linux/arm64`,
  each on a native runner, and publishes it to GHCR when a `v*` tag is pushed, with
  `IMAGE_VERSION` baked in (`/health` reports it).

### Provision one kit per hospital

`scripts/provision.py` follows NVIDIA FLARE's "startup kit" idea. It writes one
self-contained folder (and optionally a zip) per hospital, plus one for the hub:

```sh
python scripts/provision.py --zip \
  --peers hospital-a=hospital-a.tail1234.ts.net:8001,hospital-b=hospital-b.tail1234.ts.net:8002,hospital-c=hospital-c.tail1234.ts.net:8003,hospital-d=hospital-d.tail1234.ts.net:8004 \
  --directory directory.tail1234.ts.net:8100 \
  --observer directory.tail1234.ts.net:8200   # optional: this is the default (the directory's host)
```

`--peers` lists every hospital with the address the others reach it at: a LAN
IP, a Tailscale name or a domain. Every address is HTTPS (a bare `host:port`
gets `https://`; `http://` is rejected). The order is the directory's placement
order between runs. The command writes `kits/hospital-a/` ... `kits/hospital-d/`
and `kits/directory/`, plus a `.zip` of each. The network's CA and keys live in
`kits/.pki/` (or `--pki DIR`) and are reused on every later run, so the network
is never re-keyed. Every hospital kit holds:

- `.env`: filled in. `NODE_ID`, `NODE_PORT`, its own address (`ADVERTISE_ADDR`), the
  directory's and the observer's, and one freshly generated shared secret (the same
  for every kit from the same run; pass it with `--secret` to provision a hospital
  later). The hotspot block is there to switch to. There is no chain in it: the
  directory places the hospital.
- `deploy/node.yml`, a copy of the repository's.
- `config/node.yaml`: the hospital's display name and engine settings, from the
  repository's `config/hospital-X.yaml`. Nothing about where it runs.
- `pki/`: only this hospital's own TLS certificate and key (issued for its
  address), its update-signing key and its directory credential, plus the CA
  certificate, the directory's public key and the directory's certificate (whose
  key the node pins), and the observer's certificate (also pinned) and ingest
  token (`pki/observer/token`). No other party's private key, and no database
  credentials.
- `data/hospital-X.csv`, that hospital's slice. Pass `--data hospital-X=path.csv`
  to use different data.
- `README.md`, with the exact commands, including how to leave the chain.

`--image repo[:tag]` names a different image (for example a private registry). By
default the kits use the published `ghcr.io/denimworld12/medchain-node:v1.1.0`.

The directory kit is the hub's. It holds the directory's config, its TLS certificate,
its document-signing key and `hospitals.yaml` (who may register: ids and credential
hashes only), the observer's TLS certificate, key and token, and a filled `hub.env`
for `deploy/hub.yml`. Send each hospital kit privately to its operator. Start the hub,
then each hospital, each with one command:

```sh
make hub                                                          # on the hub, from the repository
docker compose --project-directory . -f deploy/node.yml up -d     # on each hospital machine, in its kit
```

Start training from any one node (`POST /round/start`, as above). The start
floods along the chain.

**Adding a hospital for real.** Provision it into the same network, then admit
it and start it:

```sh
python scripts/provision.py --hospital hospital-e --secret <the network secret> \
  --peers <the same list>,hospital-e=hospital-e.tail1234.ts.net:8005 --directory directory.tail1234.ts.net:8100
# copy kits/.pki/directory/hospitals.yaml to the hub's kits/directory/pki/directory/
#   (re-read on every registration, no restart); send kits/hospital-e/ to its operator, who runs:
docker compose --project-directory . -f deploy/node.yml up -d
```

The node registers; if a run is going, the directory appends it at the end of
the chain from a round boundary a few rounds ahead, and it joins the run.

**Removing a hospital for real.** On that hospital's machine, in its kit:

```sh
docker compose --project-directory . -f deploy/node.yml exec node python -m node.leave
docker compose --project-directory . -f deploy/node.yml down
```

The directory takes it out at the next boundary and its neighbours bridge. To
stop it from ever registering again, delete its line from `hospitals.yaml`.

**Changing an address without re-provisioning.** Edit `ADVERTISE_ADDR` (alias
`MEDCHAIN_ADVERTISE_URL`), a node's own address, or `DIRECTORY_URL` (alias
`MEDCHAIN_DIRECTORY_URL`), the directory's, in the kit's `.env` and run `up -d` again.
That is all the hotspot fallback does. On restart the node re-registers its new address and the
directory passes it to its neighbours. Node-to-node links pin the certificate's
key, not its address, so they keep working. Only the dashboard's relay checks the
address. For the relay, re-run `provision.py` for that hospital with the new
address: its certificate is re-issued for the same key, so its pin is unchanged. A node with a static chain (`chain:` and `neighbours:`
in its YAML, no `directory:`, as the tests use) still takes
`MEDCHAIN_PEER_<ID>_URL` for each neighbour: `<ID>` is the node id upper-cased,
with `-` and other characters turned into `_`.

**The observer only observes.** Kit nodes push their telemetry to the observer
(`--observer`, by default the directory's host on port 8200), which `deploy/hub.yml`
runs next to Postgres. It never coordinates training. A node with no observer still
trains and still serves `/status` and `/metrics`.

### Join with a code instead of a kit

A kit has to be made, zipped and sent, and it carries the network's shared secret.
For a hospital on another laptop there is a second way in, next to the kits (which
keep working unchanged): a **join code**.

1. The organiser creates a code on the dashboard's admin page. It works **once** and
   **expires** (30 minutes by default); the organiser can revoke it. The directory
   keeps only its SHA-256.
2. On the hospital's laptop, `python -m node.enroll` (run by the install script in
   `deploy/install/`, inside the published image) makes the hospital's keys **on that
   laptop**: a TLS key, an Ed25519 signing key and a directory credential. It sends the
   hub only a certificate signing request, the public signing key and the credential's
   hash, with the display name the person typed, to `POST /enroll` on the directory.
   It proves it holds the code with an HMAC keyed by the code's hash, so the code itself
   never crosses the network.
3. The directory checks the code (known, unused, unrevoked, unexpired, the right
   secret; five wrong tries revoke it), marks it used, and answers with: a certificate
   for the hospital's key from the network CA, the CA certificate, the directory's
   document key and certificate, the observer's certificate and port, an **observer
   token for this hospital alone**, a generated node id, the engine settings, a data
   slice, and the network secret. Its answer carries an HMAC under the same key, and
   the node checks it, the nonce, and that the TLS key that answered is the
   directory's, before trusting any of it.
4. The node writes a kit folder with the same layout as a provisioned one, so
   `deploy/node.yml` runs it.

On the hospital's laptop this is one command: `deploy/install/install.ps1` (Windows)
or `deploy/install/install.sh` (macOS, Linux), fetched from the release tag. It checks
Docker, Tailscale, the hub and disk space (each with a plain-English fix), asks for
the name and the code, reads the Tailscale address, enrolls inside the published
image (no Python, no checkout) and starts the node. Running it again keeps the
enrollment. The dashboard's **JOIN CODES** page shows the command with the hub's
address filled in (`HUB_ADDRESS`). The secret goes to `pki/network/shared_secret`
   (`SHARED_SECRET_FILE`): nobody types it, nothing prints it.

**Names.** The person enrolling types the hospital's display name as free text; it
is what the dashboard, the node's logs and the audit log show. No two hospitals may
share one (compared ignoring case and spacing; the code stays valid for another try).
The node id is separate and generated by the directory, a slug of the name plus random
hex (`st-marys-leeds-3f9a`). TLS pins, signatures and chain placement stay keyed on the
id and its keys, never on the name, and an enrolled hospital always appears under the
name it enrolled with. `hospital-a` ... `hospital-e` stay as they are for `make up`
and the kits.

Every attempt is in the audit log (`enrolled`, `enroll_rejected`,
`join_code_created`, `join_code_revoked`). An enrolled hospital may only register
with the keys it enrolled with. Its observer token only writes rows as that hospital;
the observer checks it against `hospital_keys`, which only the directory writes. The
directory needs the CA key for this: `provision.py` puts it in the hub kit
(`pki/ca.key`), and `deploy/hub.yml` mounts `data/slices/` and
`config/hospital-a.yaml` (the engine settings) into it.

### Proof of origin on the dashboard

Every node with a signing key and an observer sends the observer a status message every
10 seconds (`node/proof.py`, `POST /proof`), signed with the Ed25519 key that signs its
training updates. It holds the computer's hostname, OS and version, uptime and Tailscale
device name (read on the host by the install script; without it, what the container
sees, labelled as such), the round-trip time it measures right then to each chain
neighbour over the pinned TLS links training uses, and how many data rows it holds (the
count only). It also carries a per-start boot id and a rising sequence number.

The observer checks the signature against the key the directory has on record for that
hospital (`hospital_keys`, written by the directory when the hospital registers or
enrolls). It refuses a message that does not verify, comes from a hospital with no key
on record, is a replay or is more than 10 minutes old, and writes each refusal to the
audit log (`proof_rejected`). A caller without that hospital's observer token (or the
network's) is refused before any of this and only logged, so it cannot mark someone
else's hospital rejected. The dashboard's **Nodes** page shows each hospital's facts
with a **Signature verified** badge and the key's fingerprint, or **Rejected** with the
reason. This is in addition to the Start/Pause controls. One limit: the host's uptime
counts from the boot time the install script saw, so after a reboot, run the installer
again to refresh it. The round-trip times and data rows are always live.

### Prove it

`scripts/verify_multihost.py` is the acceptance test, on real laptops or on one
machine. Point it at every node currently in the chain (any number), with the
network CA. It checks that the nodes agree on the chain, starts one run through
the first node, waits for every hospital to finish, and then reports what crossed
the network: per hospital, the rounds trained, neighbour updates heard on time,
and consensus; per directed link (with `--database-url`, run on the hub, where
Postgres listens on loopback only), packets delivered,
packets received and verified, and exact bytes. It exits non-zero if any check
fails.

```sh
python scripts/verify_multihost.py --secret <kit secret> --ca kits/.pki/ca.crt \
  --node https://hospital-a.tail1234.ts.net:8001 --node https://hospital-b.tail1234.ts.net:8002 \
  --node https://hospital-c.tail1234.ts.net:8003 --node https://hospital-d.tail1234.ts.net:8004 \
  --database-url postgresql://medchain:medchain@127.0.0.1:5432/medchain
```

The proof below predates dynamic membership, TLS and signed updates: three
static-chain kits over plain HTTP. We ran it on one machine as the stand-in for
three laptops. The setup:

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
launches three separate `python -m node` processes (static chain) bound to the
runner's own network address. Their YAML peer URLs are unresolvable placeholders,
and the real addresses arrive only through `MEDCHAIN_PEER_<ID>_URL`. The verifier
must then pass.

## Engines

`engine:` in `config/hospital-*.yaml` (same value on every node) picks one:

| `engine:` | Label in UI and logs | What travels per message (d = 16 parameters) |
| --- | --- | --- |
| `qgadmm` | `Q-GADMM (b=2)` (`bits:` 2, 4 or 8) | `ceil(b*d/8) + 8` bytes: b-bit stochastic quantization of `theta - theta_hat`, plus `b` and `R` |
| `gadmm_fp` | `GADMM full precision (float32 deltas)` | `4*d + 8` bytes: the same algorithm with quantization skipped |
| `gossip` | `naive gossip baseline` | `8*d` bytes: the raw float64 weights, averaged with the neighbours' |

Switch engines by editing `engine:` in every config and recreating the nodes
(`docker compose up -d --force-recreate hospital-a hospital-b hospital-c hospital-d`).
The ADMM settings sit next to it: `rho` (5), `alpha` (1.0), `bits` (2),
`local_epochs` (50 full-batch Adam steps per primal step), `lr` (0.01),
`epsilon` (1e-3), `checkpoint_every` (5) and `seed` (42, plus a per-hospital
offset). The gossip baseline ignores them. The chain order comes from the
directory; a node without one can instead list `chain` (the node order 1..N) and
`neighbours` in its YAML, which is what the in-process tests do.

### How Q-GADMM runs on the chain (`node/qgadmm/`)

- `state.py`: chain positions for any N (odd = HEAD, even = TAIL; the boundary nodes 1 and N
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
- Membership changes (`node/membership.py`, applied in `scheduler.py` and the engine's
  `relink`) land on a round boundary: positions are recomputed from the new chain, a
  removed link's `lambda` and mirror are dropped, a new link starts at `lambda = 0`
  and stays silent until both ends have each other's full snapshot, and the
  consensus streak only counts rounds of the current epoch.

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

Runs gossip, full-precision GADMM and Q-GADMM (b = 8, 4, 2) headless on a chain
of hospital slices with the production engine code and writes to
`experiments/out/`: `compare_engines.png` (loss vs cumulative bits transmitted,
one curve per engine), `rho_sweep.png` (Q-GADMM b=2 at rho = 5, 15, 24, 50),
`payload_ratio.png` and `summary.json`. The committed figures use the chain
A - B - C (the three whole UCI institutions); `--chain` takes any chain of the
hospitals in `config/`, e.g. `--chain hospital-a,hospital-b,hospital-c,hospital-d,hospital-e`.
On A - B - C every ADMM variant reaches the centralized optimum; Q-GADMM b=2
does it in 69 rounds and 3.3 kB against gossip's 204.8 kB after 400 rounds (and
gossip's hospitals still disagree). At d = 16, b=4 needs even fewer total bytes
(2.8 kB) because it reaches consensus in 44 rounds. On all five hospitals, b=2
reaches consensus in 158 rounds and 15.2 kB.

## How MedChain differs from Flower and NVIDIA FLARE

[Flower](https://github.com/adap/flower) and
[NVIDIA FLARE](https://github.com/NVIDIA/NVFlare) are both hub-and-spoke. In
Flower, every client (a SuperNode) connects to one central SuperLink server,
which selects clients and aggregates their updates. NVIDIA FLARE likewise has
one server and per-site clients, with mutual-TLS certificates from a
provisioning step that hands each site a startup kit.

| | Flower / NVIDIA FLARE | MedChain |
| --- | --- | --- |
| Topology | Central server; every site connects to it | No training server. Each hospital talks only to its chain neighbours, on a chain of any length |
| Membership | The server admits clients and selects them each round | A directory decides membership only: where a hospital joins, who bridges when one leaves. It never sees updates and never runs or influences training; if it is down, training goes on |
| How the model is combined | The server aggregates the site updates (e.g. FedAvg) | Q-GADMM decentralized consensus: each node solves its local ADMM step and exchanges quantized differences with its neighbours |
| Who sees updates | The server receives every site's update (unless secure aggregation is added) | No node receives every site's update; each gets only its neighbours' quantized differences |
| What goes on the wire | Model weights or gradients, full precision by default | b-bit stochastic quantization of the change since the last snapshot |
| Starting a run, observing | The server drives the rounds | A start request floods from whichever node gets it; the dashboard only observes |
| Site setup | NVIDIA FLARE: one startup kit per site | One kit per hospital plus one for the directory (`scripts/provision.py`), the idea borrowed from NVIDIA FLARE |
| Transport security | TLS (NVIDIA FLARE: mutual TLS from the provisioned kits) | TLS on every link plus an Ed25519 signature on every update, keys from the provisioned kits |

**Measured bandwidth.** The model has d = 16 parameters. Each Q-GADMM b=2 packet
is 12 bytes, against 64 bytes at 32 bits (5.3x less) and 128 bytes at float64
(10.7x less). Over a whole run on A - B - C, Q-GADMM b=2 reaches the
centralized optimum with 3.3 kB on the wire. The gossip baseline sends 204.8 kB
in 400 rounds and its hospitals still disagree (`experiments/out/summary.json`).
The cross-address run above (at the earlier `rho: 24`) moved 7,824 bytes across
its four links, against 83,456 bytes for the same 652 messages at float64.

**Trade-offs.** The hub frameworks are mature and scale to many sites, with
client sampling and many aggregation strategies. MedChain is a prototype tested
with up to five hospitals. A chain also has weak points. A longer chain needs
more rounds to agree. A planned departure is repaired (the neighbours bridge),
but a hospital that crashes without notice still splits the chain in two until
it rejoins, and its neighbours train on their own data meanwhile. Mid-run joins
only append at the end. The directory is a single point of failure for joins and
leaves, though not for training.

## How it fits together

- **One node image, config decides identity** (`config/hospital-*.yaml`: node_id,
  port, address, directory, keys, engine settings, data_slice; or the same from
  the environment, `node/settings.py`). The topology is a
  chain of any length that the directory (`directory/`) assigns and changes at
  round boundaries; a node can also be given a static chain in its YAML.
  Addresses can be overridden from the environment (see
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
- **Identity and transport**: HTTPS on every link, an Ed25519 signature on every
  `/update` and `/snapshot` (`node/security.py`), and directory-signed membership
  documents (see [Security between nodes](#security-between-nodes)).
- **Shared secret**: `/update`, `/snapshot`, `/round/*`, `GET /membership`,
  `/membership/leave` and `/assess*` require the `X-MedChain-Secret` header to
  match `shared_secret` in the node config (one demo value for all nodes; the
  dashboard gets it via `MEDCHAIN_SHARED_SECRET`), otherwise 401. `/health`,
  `/status`, `/metrics` and `POST /membership` (accepted only with the
  directory's signature) stay open.
- **Observer service and dashboard**: nodes push their telemetry to the observer
  (`observer/`, `POST /ingest`), which writes Postgres; the dashboard reads it.
  Neither coordinates training. The dashboard's buttons relay a
  single start/pause request to one node in the chain, which floods it to the
  others; it learns about each `/update` from the `updates_meta` rows the nodes
  report. Killing the dashboard does not affect a run (tested). It follows
  membership from what the nodes write: the topology page draws the current chain
  in order, the consensus panel shows only the current links and resets with each
  change, the network training loss averages over the members of each round's
  chain, and the communication page shows the current hospital and link count and
  each round's hospital count.
- **Risk assessment is separate from training** (`node/assess/`): its models
  are trained centrally, not by the training engines. The dashboard relays each
  request to the node you pick and stores nothing. `/status` reports whether
  each model is loaded.

## Layout

```
docker-compose.yml   config/        node/          directory/     observer/      dashboard/
design/              assets/svg/    data/          tests/         experiments/
docs/screenshots/    models/        scripts/       Makefile
```

- `make up` runs the single-machine stack (`docker compose up -d --build --wait`).
  `make lint` runs ruff (`ruff.toml`) and eslint (`dashboard/eslint.config.js`)
  on the files changed since the branch point with main only. CI does the same
  for each change. There is no repo-wide reformatting. `make hub`, `make node` and
  `make node-native` run the multi-laptop deployment in `deploy/`
  ([`docs/MULTI_LAPTOP.md`](docs/MULTI_LAPTOP.md)).
- `observer/` is the telemetry ingest service (`python -m observer`, same image).

- `directory/` is the membership directory (`python -m directory`, same image as
  the nodes). `scripts/pki.py` issues the CA, certificates, signing keys and
  directory credentials (the compose `pki` service runs it).
- `scripts/provision.py` writes the per-hospital and hub kits for `deploy/`,
  `scripts/preflight.py` reports each neighbour's path (direct or DERP-relayed), and
  `scripts/verify_multihost.py` checks a run across real addresses (see
  [Running on separate machines](#running-on-separate-machines)).
  `docs/multihost/proof-report.json` is the report from the single-machine
  stand-in run.

- `design/STYLE.md` and `design/tokens.css` are the Pa'lais style reference,
  adapted.
- `assets/generate_svgs.py` regenerates `assets/svg/`. `assets/preview.html`
  shows every asset on the cream canvas.
- `data/prepare_uci.py` re-downloads UCI Heart Disease and rebuilds the five
  non-IID slices (Cleveland, Hungarian, VA Long Beach, and the Swiss file split
  in two halves for hospitals D and E). See `data/README.md`.
- `data/cardio/` holds the Kaggle CSV the tabular risk model trains on, and
  `data/prepare_ecg.py` fetches the ECG images. `models/` holds the exported
  ECG model. `python -m node.assess.train_tabular` writes the tabular model
  to `models/heart-risk.joblib` for local runs; that file is gitignored.

## Tests

```sh
pip install -r requirements-dev.txt
pytest tests/unit                                   # no docker needed
(cd dashboard && npm ci && npm test)                # dashboard snapshot maths
docker compose up -d --build --wait
MEDCHAIN_STACK=1 pytest tests/integration -v        # real runs, real docker kill, a join and a leave
```

`tests/unit` covers quantizer unbiasedness, sender/receiver round-trip
determinism (exact float equality over 50 rounds), the boundary nodes' single
penalty/dual term, convergence sanity (N = 5 synthetic linear regression: Q-GADMM
b=2 within 1% of full-precision GADMM and 2% of centralized scikit-learn), and
chains of real node apps over HTTP in one process: three nodes, including a tail
kill and checkpoint rejoin; four and five nodes (head/tail alternation, every link
every round, consensus); and, through the directory over TLS with signed updates,
a fourth hospital joining a running chain of three, a middle hospital of four
leaving with its neighbours bridging, a crash that changes no membership, the
directory going down mid-run, a restart while the directory is down (rejoin
accepted for an unchanged chain, refused for a changed chain or a changed TLS
key), TLS key pinning on both ends, rejected tampered, unsigned, foreign-key, replayed
and unknown-sender updates, forged membership documents and bad credentials
(`test_chain_n.py`, `test_membership.py`, `test_rejoin.py`, `test_pinning.py`). It also covers
the settings precedence (`test_settings.py`), the telemetry push to the observer
with its drop-oldest queue and local `audit.db` (`test_observer.py`) and the
preflight's direct/relayed classification (`test_preflight.py`). It also covers the risk-assessment
endpoints, the address overrides, the provisioning kits and three nodes as
separate OS processes on the host's network address, checked by
`scripts/verify_multihost.py`. `dashboard/server/snapshot.test.js` covers the
dashboard's topology, consensus, network loss and communication numbers across a
join and a leave. `tests/integration` runs against whichever engine the configs
select, including a live join then leave checked on the dashboard; CI
(`.github/workflows/ci.yml`) runs it for `qgadmm`, `gadmm_fp` and `gossip`, plus
the unit and dashboard tests, the dashboard build, the docker build and headless
`compare_engines.py` runs on three and five hospitals.
