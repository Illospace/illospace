.PHONY: dev build start test test-fast test-product test-db test-all test-frontend test-frontend-e2e architecture-check survivability survivability-pr install install-browser

## Install all dependencies
install:
	python3 -m pip install -r requirements.txt
	./ops/install-browser-runtime.sh python3
	cd frontend && npm install

install-browser:
	./ops/install-browser-runtime.sh python3

## Development — run API + frontend with hot reload
dev:
	@echo "Starting Illo Brain..."
	@echo "  API:      http://localhost:8000/api/docs"
	@echo "  Frontend: http://localhost:5173"
	@echo ""
	@cd frontend && npm run dev &
	@uvicorn brain.app.api.main:app --port 8000 --reload

## Build frontend for production
build:
	cd frontend && npm run build

## Production — serve everything from one process
start:
	cd frontend && npm run build
	uvicorn brain.app.api.main:app --host 0.0.0.0 --port 8000

## Run fast tests only (no Docker, no DB, no browser harness, no live providers)
test-fast:
	python3 -m pytest tests/ -m "not requires_db and not requires_browser and not live_provider" -q

test: test-fast

## Run DB-backed suite with Docker PostgreSQL + pgvector
test-db:
	./ops/test-with-db.sh -m "not requires_browser and not live_provider" -q

## Run the core user-facing journeys against the real API + DB stack
test-product:
	./ops/test-with-db.sh tests/test_core_product_journeys.py -q

## Run backend architecture boundary guardrails
architecture-check:
	python3 -m pytest tests/test_architecture_boundaries.py -q

## Run full test suite with Docker test DB
test-all:
	./ops/test-with-db.sh

## Run frontend unit tests, Svelte check, and production build
test-frontend:
	cd frontend && npm test
	cd frontend && npm run check
	cd frontend && npm run build

## Run browser-level product journeys against the Svelte app
test-frontend-e2e:
	cd frontend && npm run test:e2e

## Measure capability survivability evidence across the repo
survivability:
	python3 scripts/survivability_index.py

## Measure impacted capability survivability for a PR diff
survivability-pr:
	python3 scripts/survivability_index.py --base $${BASE_REF:-origin/main} --fail-impacted-under $${SURVIVABILITY_MIN:-85} --fail-impacted-thresholds --fail-on-unmapped
