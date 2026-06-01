#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$ROOT/deploy/compose/docker-compose.yml"
ENV_FILE="${ILLO_COMPOSE_ENV_FILE:-$ROOT/deploy/compose/.env}"
COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_SECONDS="${ILLO_RUNTIME_SERVICE_WORKER_DRAIN_TIMEOUT_SECONDS:-${ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_SECONDS:-120}}"

source "$SCRIPT_DIR/compose-runtime-lib.sh"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/runtime-services.sh list
       deploy/scripts/runtime-services.sh restart <service-id> [service-id...]

Service ids:
EOF
  runtime_service_ids | sed 's/^/  /'
  printf '  all\n'
}

case "${1:-}" in
  list)
    runtime_service_ids
    ;;
  restart)
    shift
    restart_runtime_services "$@"
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    echo "Unknown command: $1" >&2
    usage >&2
    exit 2
    ;;
esac
