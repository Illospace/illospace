#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_DIR="$ROOT/deploy/compose"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
ENV_FILE="${ILLO_COMPOSE_ENV_FILE:-$COMPOSE_DIR/.env}"

BUILD=0
PULL=1

while [ "$#" -gt 0 ]; do
  case "$1" in
    --build)
      BUILD=1
      shift
      ;;
    --no-pull)
      PULL=0
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: ./illo deploy upgrade [--build] [--no-pull]
       deploy/scripts/upgrade.sh [--build] [--no-pull]

Pulls published images, optionally builds local images, runs migrations, and
restarts the Compose stack.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE; run deploy/scripts/init-secrets.sh first." >&2
  exit 1
fi

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

if [ "$PULL" = "1" ]; then
  compose pull postgres caddy api web || {
    echo "Image pull failed. If release images are not published yet, rerun with --build." >&2
    exit 1
  }
fi

if [ "$BUILD" = "1" ]; then
  compose build api web
fi

compose up -d postgres
compose run --rm migrate
compose up -d --remove-orphans

"$SCRIPT_DIR/doctor.sh"
