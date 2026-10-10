# MedChain shortcuts. `make up` is exactly the single-machine command from the README.
# `make hub`, `make node` and `make node-native` run the multi-laptop deployment (docs/MULTI_LAPTOP.md).

# what `make lint` compares against: the branch point with main (override: make lint BASE=<ref>)
BASE ?= $(shell git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main)
# files changed since BASE, committed or not (deleted files excluded)
CHANGED = $(shell { git diff --name-only --diff-filter=d $(BASE); git ls-files --others --exclude-standard; } | sort -u)
CHANGED_PY = $(filter %.py,$(CHANGED))
CHANGED_JS = $(patsubst dashboard/%,%,$(filter dashboard/%.js dashboard/%.jsx,$(CHANGED)))
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

# a hospital's kit folder from scripts/provision.py (its .env, pki/, data/, config/)
KIT ?=
KIT_DIR = $(abspath $(KIT))
# the hub's settings: provision.py writes them into the directory kit (template: deploy/hub.env.example)
HUB_ENV ?= kits/directory/hub.env
# make node-native: a Python 3.10+ to build its own venv from node/requirements.txt
NATIVE_PYTHON ?= $(shell command -v python3.12 || command -v python3.13 || command -v python3)
NATIVE_VENV ?= .venv-node

.PHONY: up hub node node-native check-kit lint

up:
	docker compose up -d --build --wait

# the organiser's laptop: directory, observer, postgres (loopback only) and the dashboard
hub:
	@test -f "$(HUB_ENV)" || { echo "no $(HUB_ENV): run scripts/provision.py first, or copy deploy/hub.env.example there and fill it in"; exit 2; }
	docker compose --project-directory . --env-file "$(HUB_ENV)" -f deploy/hub.yml up -d --wait

# one hospital node in Docker, from its kit (the same as `docker compose --project-directory . -f deploy/node.yml up` inside the kit)
node: check-kit
	docker compose --project-directory "$(KIT_DIR)" -f deploy/node.yml up

# the same node without Docker (when Docker's networking misbehaves): its own venv, the kit's .env,
# and the kit's folders in place of the container paths deploy/node.yml mounts
node-native: check-kit $(NATIVE_VENV)/.installed models/heart-risk.joblib
	set -a && . "$(KIT_DIR)/.env" && set +a && \
	export NODE_CONFIG="$(KIT_DIR)/config/node.yaml" DATA_PATH="$(KIT_DIR)/data" \
	  TLS_CERT="$(KIT_DIR)/pki/$$NODE_ID/tls.crt" TLS_KEY="$(KIT_DIR)/pki/$$NODE_ID/tls.key" TLS_CA="$(KIT_DIR)/pki/ca.crt" \
	  SIGNING_KEY="$(KIT_DIR)/pki/$$NODE_ID/signing.key" DIRECTORY_CREDENTIAL_FILE="$(KIT_DIR)/pki/$$NODE_ID/credential" \
	  DIRECTORY_PUBLIC_KEY="file:$(KIT_DIR)/pki/directory/signing.pub" DIRECTORY_TLS_PIN="file:$(KIT_DIR)/pki/directory/tls.crt" \
	  OBSERVER_TOKEN_FILE="$(KIT_DIR)/pki/observer/token" OBSERVER_TLS_PIN="file:$(KIT_DIR)/pki/observer/tls.crt" && \
	exec $(NATIVE_VENV)/bin/python -m node

check-kit:
	@test -n "$(KIT)" -a -f "$(KIT_DIR)/.env" || { echo "name a kit folder that has a .env, e.g. make $(MAKECMDGOALS) KIT=kits/hospital-b"; exit 2; }

$(NATIVE_VENV)/.installed: node/requirements.txt
	@"$(NATIVE_PYTHON)" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "make node-native needs Python 3.10 or newer: pass NATIVE_PYTHON=/path/to/python3.12")'
	"$(NATIVE_PYTHON)" -m venv $(NATIVE_VENV)
	$(NATIVE_VENV)/bin/pip install -r node/requirements.txt
	touch $@

# the risk-assessment model the image trains at build time (node/Dockerfile); the ECG model is committed
models/heart-risk.joblib: | $(NATIVE_VENV)/.installed
	$(NATIVE_VENV)/bin/python -m node.assess.train_tabular --csv data/cardio/cardio_train.csv --out $@

# ruff and eslint on changed files only (no repo-wide pass, no reformatting)
lint:
	@if [ -n "$(strip $(CHANGED_PY))" ]; then echo "ruff: $(words $(CHANGED_PY)) file(s)"; $(PYTHON) -m ruff check $(CHANGED_PY); else echo "ruff: no changed Python files"; fi
	@if [ -n "$(strip $(CHANGED_JS))" ]; then echo "eslint: $(words $(CHANGED_JS)) file(s)"; cd dashboard && npx eslint $(CHANGED_JS); else echo "eslint: no changed dashboard JS"; fi
