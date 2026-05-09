#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$ROOT/deploy/compose/docker-compose.yml"

BUILD=0
KEEP_RUNNING=0
ENV_FILE=""
PROJECT_NAME="illospace-smoke-${RANDOM}${RANDOM}"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/smoke-test.sh [--build] [--keep-running] [--env-file path]

Boots a disposable Compose project, runs migrations, starts the API and web
entrypoint, and checks API health through both loopback ports. By default it
pulls published images; use --build when testing local Dockerfile changes.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --build)
      BUILD=1
      shift
      ;;
    --keep-running)
      KEEP_RUNNING=1
      shift
      ;;
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose v2 is required" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required" >&2
  exit 1
fi

pick_free_port() {
  python3 - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

SMOKE_API_PORT="${ILLO_SMOKE_API_PORT:-$(pick_free_port)}"
SMOKE_WEB_PORT="${ILLO_SMOKE_WEB_PORT:-$(pick_free_port)}"
while [ "$SMOKE_WEB_PORT" = "$SMOKE_API_PORT" ]; do
  SMOKE_WEB_PORT="$(pick_free_port)"
done

if [ -z "$ENV_FILE" ]; then
  ENV_FILE="$(mktemp "${TMPDIR:-/tmp}/illospace-smoke-env.XXXXXX")"
  rm -f "$ENV_FILE"
  "$SCRIPT_DIR/init-secrets.sh" \
    --env-file "$ENV_FILE" \
    --public-url "http://127.0.0.1:${SMOKE_WEB_PORT}" >/dev/null
else
  case "$ENV_FILE" in
    /*) ;;
    *) ENV_FILE="$ROOT/$ENV_FILE" ;;
  esac
fi

python3 - "$ENV_FILE" "$PROJECT_NAME" "$SMOKE_API_PORT" "$SMOKE_WEB_PORT" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
project_name = sys.argv[2]
api_port = sys.argv[3]
web_port = sys.argv[4]
text = env_path.read_text(encoding="utf-8")

updates = {
    "COMPOSE_PROJECT_NAME": project_name,
    "ILLO_PUBLIC_URL": f"http://127.0.0.1:{web_port}",
    "ILLO_API_PORT": api_port,
    "ILLO_WEB_PORT": web_port,
}

for key, value in updates.items():
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    replacement = f"{key}={value}"
    if pattern.search(text):
        text = pattern.sub(replacement, text)
    else:
        text = text.rstrip() + f"\n{replacement}\n"

env_path.write_text(text, encoding="utf-8")
PY

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo
    echo "Smoke test failed; recent Compose logs:"
    compose logs --no-color --tail=200 || true
  fi
  if [ "$KEEP_RUNNING" != "1" ]; then
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    case "$ENV_FILE" in
      "${TMPDIR:-/tmp}"/illospace-smoke-env.*|/tmp/illospace-smoke-env.*)
        rm -f "$ENV_FILE"
        ;;
    esac
  else
    echo "Keeping smoke stack running: COMPOSE_PROJECT_NAME=$PROJECT_NAME"
    echo "Env file: $ENV_FILE"
  fi
  exit "$status"
}
trap cleanup EXIT

echo "Rendering Compose config..."
compose config --quiet

if [ "$BUILD" = "1" ]; then
  echo "Building API and web images..."
  compose build api web
else
  echo "Pulling images..."
  compose pull postgres api web
fi

echo "Starting Postgres..."
compose up -d postgres

echo "Running migrations..."
compose run --rm migrate

echo "Starting API..."
compose up -d api

echo "Waiting for API health..."
deadline=$((SECONDS + 120))
while [ "$SECONDS" -lt "$deadline" ]; do
  if curl -fsS "http://127.0.0.1:${SMOKE_API_PORT}/api/health/live" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

curl -fsS "http://127.0.0.1:${SMOKE_API_PORT}/api/health/live" >/dev/null
curl -fsS "http://127.0.0.1:${SMOKE_API_PORT}/api/health/ready" >/dev/null

echo "Starting web entrypoint..."
compose up -d web

echo "Waiting for web route health..."
deadline=$((SECONDS + 120))
while [ "$SECONDS" -lt "$deadline" ]; do
  if curl -fsSL "http://127.0.0.1:${SMOKE_WEB_PORT}/api/health/live" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

curl -fsSL "http://127.0.0.1:${SMOKE_WEB_PORT}/api/health/live" >/dev/null
curl -fsSL "http://127.0.0.1:${SMOKE_WEB_PORT}/" >/dev/null

echo "Smoke test passed."
