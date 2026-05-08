#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_DIR="$ROOT/deploy/compose"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
ENV_FILE="${ILLO_COMPOSE_ENV_FILE:-$COMPOSE_DIR/.env}"

ASSUME_YES=0
DUMP_FILE=""

usage() {
  cat <<'EOF'
Usage: ./illo deploy restore [--yes] /path/to/illospace.dump

Restores a Postgres custom-format dump into the Compose database, runs
migrations, and restarts the application services.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes|-y)
      ASSUME_YES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [ -z "$DUMP_FILE" ]; then
        DUMP_FILE="$1"
        shift
      else
        echo "Unexpected argument: $1" >&2
        exit 2
      fi
      ;;
  esac
done

if [ -z "$DUMP_FILE" ]; then
  usage >&2
  exit 2
fi

if [ ! -f "$DUMP_FILE" ]; then
  echo "Dump file not found: $DUMP_FILE" >&2
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE; run deploy/scripts/init-secrets.sh first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

if [ "$ASSUME_YES" != "1" ]; then
  echo "This will replace the ${DB_NAME:?} database in the Illospace Compose stack."
  printf "Type RESTORE to continue: "
  read -r answer
  if [ "$answer" != "RESTORE" ]; then
    echo "Restore cancelled."
    exit 1
  fi
fi

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

compose stop api worker scheduler >/dev/null || true
compose exec -T postgres pg_restore --clean --if-exists -U "${DB_USER:?}" -d "${DB_NAME:?}" < "$DUMP_FILE"
compose run --rm migrate
compose up -d api worker scheduler web

echo "Restore complete."
