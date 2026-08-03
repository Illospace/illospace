.PHONY: dev build start test test-all architecture-check install install-browser

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

## Run fast tests only (no Docker, no DB)
# No explicit path: pytest.ini's testpaths owns the selection, so this runs the
# same suite CI runs. Naming tests/ here would silently drop meetbot/tests again.
test:
	python3 -m pytest -m "not requires_db" -q

## Run backend architecture boundary guardrails
architecture-check:
	python3 -m pytest tests/test_architecture_boundaries.py -q

## Run full test suite with Docker test DB
test-all:
	./ops/test-with-db.sh
