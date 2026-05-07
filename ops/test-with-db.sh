#!/usr/bin/env bash
set -euo pipefail

# --- Preflight checks ---
if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker is not installed."
    exit 1
fi
if ! docker compose version &> /dev/null; then
    echo "ERROR: docker compose not available. Need Docker Compose v2+."
    exit 1
fi

COMPOSE_FILE="docker-compose.test.yml"
cleanup() { docker compose -f "$COMPOSE_FILE" down --volumes 2>/dev/null; }
trap cleanup EXIT

# --- Start test DB ---
echo "Starting test database..."
docker compose -f "$COMPOSE_FILE" up -d --wait || {
    echo "ERROR: Container startup failed. Is port 5433 in use?"
    exit 1
}

# --- Export DB connection for brain.kernel.config and Alembic ---
export DB_HOST=localhost
export DB_PORT=5433
export DB_NAME=illo_test
export DB_USER=illo_test
export DB_PASSWORD=illo_test
export TEST_DB_URL="postgresql://illo_test:illo_test@localhost:5433/illo_test"

# --- Run migrations ---
echo "Running migrations..."
python3 -m alembic upgrade head || {
    echo "ERROR: Migration failed."
    exit 1
}

# --- Run full test suite ---
echo "Running tests..."
python3 -m pytest tests/ "$@"
