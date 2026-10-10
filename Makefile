# MedChain shortcuts. `make up` is exactly the single-machine command from the README.

# what `make lint` compares against: the branch point with main (override: make lint BASE=<ref>)
BASE ?= $(shell git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main)
# files changed since BASE, committed or not (deleted files excluded)
CHANGED = $(shell { git diff --name-only --diff-filter=d $(BASE); git ls-files --others --exclude-standard; } | sort -u)
CHANGED_PY = $(filter %.py,$(CHANGED))
CHANGED_JS = $(patsubst dashboard/%,%,$(filter dashboard/%.js dashboard/%.jsx,$(CHANGED)))
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: up lint

up:
	docker compose up -d --build --wait

# ruff and eslint on changed files only (no repo-wide pass, no reformatting)
lint:
	@if [ -n "$(strip $(CHANGED_PY))" ]; then echo "ruff: $(words $(CHANGED_PY)) file(s)"; $(PYTHON) -m ruff check $(CHANGED_PY); else echo "ruff: no changed Python files"; fi
	@if [ -n "$(strip $(CHANGED_JS))" ]; then echo "eslint: $(words $(CHANGED_JS)) file(s)"; cd dashboard && npx eslint $(CHANGED_JS); else echo "eslint: no changed dashboard JS"; fi
