# Northstar claims analytics warehouse. `make help` lists the targets.
PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin
STAMP  := $(VENV)/.installed
SOURCE := data/generated
DB     := warehouse/claims_warehouse.duckdb
METABASE_PORT ?= 3000
export METABASE_PORT

.DEFAULT_GOAL := help
.PHONY: help setup data build test lint format check analyses demo demo-down demo-reset docs ui manifest clean

help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-11s %s\n", $$1, $$2}'

# The stamp is written only after pip succeeds, and goes stale when a pin changes, so a
# failed or outdated install is redone instead of being reported as up to date.
$(STAMP): requirements.txt requirements-dev.txt
	@$(PYTHON) -c 'import sys; sys.exit("Python 3.11+ is required (try: make setup PYTHON=python3.11)") if sys.version_info < (3, 11) else None'
	$(PYTHON) -m venv --clear $(VENV)
	$(BIN)/python -m pip install --quiet --upgrade pip
	$(BIN)/python -m pip install --quiet -r requirements-dev.txt
	@touch $@

setup: $(STAMP) ## create .venv and install the pinned dependencies

data: $(STAMP) ## generate the synthetic sources and verify them against the pinned manifest
	$(BIN)/python -m claims_warehouse.synthetic --out $(SOURCE) --check-manifest data/synthetic-manifest.json

build: data ## full reload: synthetic sources -> warehouse/claims_warehouse.duckdb
	$(BIN)/python -m claims_warehouse.build --source $(SOURCE) --db $(DB)

test: $(STAMP) ## run the test suite (tests build their own warehouses)
	$(BIN)/python -m pytest

lint: $(STAMP) ## ruff lint + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: $(STAMP) ## apply ruff formatting and safe fixes
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

check: lint test analyses ## lint, tests, a clean rebuild and every analysis (CI also builds the demo image)

analyses: build ## run every example analysis against the warehouse
	$(BIN)/python -m claims_warehouse.query analyses/*.sql

demo: build ## serve Metabase on 127.0.0.1, connect the warehouse, load example questions
	$(BIN)/python demo/demo.py up

demo-down: $(STAMP) ## stop Metabase (keeps its state)
	$(BIN)/python demo/demo.py down

demo-reset: $(STAMP) ## stop Metabase and delete its application database
	$(BIN)/python demo/demo.py reset

docs: ## open the data-model page in a browser
	@$(PYTHON) -m webbrowser -t "file://$(CURDIR)/docs/data-model.html" >/dev/null

ui: build ## optional: DuckDB local UI over a read-only attach (downloads the UI extension)
	$(BIN)/python demo/duckdb_ui.py --db $(DB)

manifest: $(STAMP) ## re-pin data/synthetic-manifest.json after an intentional generator change
	$(BIN)/python -m claims_warehouse.synthetic --out $(SOURCE) --write-manifest data/synthetic-manifest.json

clean: ## remove generated data, the warehouse and caches (keeps .venv)
	rm -rf $(SOURCE) $(DB) .pytest_cache .ruff_cache
