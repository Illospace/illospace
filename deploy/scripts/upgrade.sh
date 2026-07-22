#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_DIR="$ROOT/deploy/compose"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
ENV_FILE="${ILLO_COMPOSE_ENV_FILE:-$COMPOSE_DIR/.env}"

BUILD=0
BUILD_NO_CACHE="${ILLO_COMPOSE_BUILD_NO_CACHE:-0}"
PULL=1
SKIP_UPDATER_RESTART="${ILLO_COMPOSE_SKIP_UPDATER_RESTART:-0}"
WORKER_DRAIN_TIMEOUT_FILE="${ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_FILE:-}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --build)
      BUILD=1
      shift
      ;;
    --no-cache)
      BUILD=1
      BUILD_NO_CACHE=1
      shift
      ;;
    --no-pull)
      PULL=0
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: ./illo deploy upgrade [--build] [--no-cache] [--no-pull]
       deploy/scripts/upgrade.sh [--build] [--no-cache] [--no-pull]

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

[ -z "$WORKER_DRAIN_TIMEOUT_FILE" ] || rm -f "$WORKER_DRAIN_TIMEOUT_FILE" 2>/dev/null || true

COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_FILE="$WORKER_DRAIN_TIMEOUT_FILE"
COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_SECONDS="${ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_SECONDS:-86400}"

source "$SCRIPT_DIR/compose-runtime-lib.sh"

non_worker_services() {
  if [ "$SKIP_UPDATER_RESTART" = "1" ]; then
    printf '%s\n' api scheduler web
  else
    printf '%s\n' api scheduler web updater
  fi
}

all_runtime_services() {
  non_worker_services
}

schedule_updater_refresh_after_self_update() {
  local delay job_name
  [ "$SKIP_UPDATER_RESTART" = "1" ] || return 0
  delay="${ILLO_COMPOSE_UPDATER_SELF_REFRESH_DELAY_SECONDS:-15}"
  if [[ ! "$delay" =~ ^[0-9]+$ ]]; then
    delay="15"
  fi
  job_name="${COMPOSE_PROJECT_NAME:-illospace}-updater-self-refresh-$(date -u +%Y%m%d%H%M%S)"
  echo "Updater: scheduling self-refresh in ${delay}s so the host controller runs the latest code."
  compose run -d --name "$job_name" --no-deps --entrypoint sh updater -lc \
    "sleep $delay; docker compose --env-file \"\${ILLO_COMPOSE_ENV_FILE:-/repo/deploy/compose/.env}\" -f /repo/deploy/compose/docker-compose.yml up -d --force-recreate --no-deps updater; docker rm -f \"$job_name\" >/dev/null 2>&1 || true" \
    >/dev/null || echo "Updater: could not schedule delayed self-refresh; restart updater manually after this update." >&2
}

if [ "$PULL" = "1" ]; then
  compose pull postgres api web updater || {
    echo "Image pull failed. If release images are not published yet, rerun with --build." >&2
    exit 1
  }
fi

if [ "$BUILD" = "1" ]; then
  if [ "$BUILD_NO_CACHE" = "1" ]; then
    compose build --no-cache api web updater
  else
    compose build api web updater
  fi
fi

compose up -d postgres
compose run --rm migrate
NONTERMINAL_RUNS="$(nonterminal_agent_run_details)"
if [ "$NONTERMINAL_RUNS" = "unknown" ]; then
  echo "Cannot safely swap worker because non-terminal AgentRun ids are unknown." >&2
  exit 1
fi
if [ -z "$NONTERMINAL_RUNS" ]; then
  mapfile -t runtime_services < <(all_runtime_services)
  compose up -d --force-recreate --remove-orphans "${runtime_services[@]}"
  replace_idle_worker
else
  report_nonterminal_agent_runs "$NONTERMINAL_RUNS"
  echo "Updating API, scheduler, and web while preserving active worker AgentRuns."
  mapfile -t services < <(non_worker_services)
  compose up -d --force-recreate --no-deps "${services[@]}"
  update_worker_after_drain "$NONTERMINAL_RUNS" reported
fi

"$SCRIPT_DIR/doctor.sh"
schedule_updater_refresh_after_self_update
