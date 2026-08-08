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

export ILLO_BUILD_COMMIT="${ILLO_BUILD_COMMIT:-$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)}"
export ILLO_BUILD_TIME="${ILLO_BUILD_TIME:-$(date -u +"%Y-%m-%dT%H:%M:%SZ")}"
export ILLO_DEPLOY_TIME="${ILLO_DEPLOY_TIME_OVERRIDE:-$(date -u +"%Y-%m-%dT%H:%M:%SZ")}"

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE; run deploy/scripts/init-secrets.sh first." >&2
  exit 1
fi

[ -z "$WORKER_DRAIN_TIMEOUT_FILE" ] || rm -f "$WORKER_DRAIN_TIMEOUT_FILE" 2>/dev/null || true

COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_FILE="$WORKER_DRAIN_TIMEOUT_FILE"
COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_SECONDS="${ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_SECONDS:-86400}"

source "$SCRIPT_DIR/compose-runtime-lib.sh"

MEETBOT_ENABLED=0
if compose_service_enabled meetbot; then
  MEETBOT_ENABLED=1
fi

non_worker_services() {
  printf '%s\n' api scheduler web
  [ "$MEETBOT_ENABLED" = "0" ] || printf '%s\n' meetbot
  if [ "$SKIP_UPDATER_RESTART" = "1" ]; then
    return 0
  else
    printf '%s\n' updater
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
  pull_services=(postgres api web updater)
  [ "$MEETBOT_ENABLED" = "0" ] || pull_services+=(meetbot)
  compose pull "${pull_services[@]}" || {
    echo "Image pull failed. If release images are not published yet, rerun with --build." >&2
    exit 1
  }
fi

if [ "$BUILD" = "1" ]; then
  build_services=(api web updater)
  [ "$MEETBOT_ENABLED" = "0" ] || build_services+=(meetbot)
  if [ "$BUILD_NO_CACHE" = "1" ]; then
    compose build --no-cache "${build_services[@]}"
  else
    compose build "${build_services[@]}"
  fi
fi

compose up -d postgres
compose run --rm migrate
WORKER_SWAP_SNAPSHOT="$(worker_swap_snapshot)"
WORKER_RESTART_STATUS=0
case "$(worker_swap_snapshot_decision "$WORKER_SWAP_SNAPSHOT")" in
  replace)
    mapfile -t runtime_services < <(all_runtime_services)
    compose up -d --force-recreate --remove-orphans "${runtime_services[@]}"
    replace_idle_worker || WORKER_RESTART_STATUS=$?
    ;;
  drain)
    echo "$(worker_swap_snapshot_report "$WORKER_SWAP_SNAPSHOT")."
    echo "Updating API, scheduler, and web while preserving active worker AgentRuns."
    mapfile -t services < <(non_worker_services)
    compose up -d --force-recreate --no-deps "${services[@]}"
    update_worker_after_drain "$WORKER_SWAP_SNAPSHOT" reported || WORKER_RESTART_STATUS=$?
    ;;
  *)
    echo "Cannot safely swap worker because non-terminal AgentRun ids are unknown." >&2
    exit 1
    ;;
esac
assert_single_running_worker || WORKER_RESTART_STATUS=1
if [ "$WORKER_RESTART_STATUS" -ne 0 ]; then
  exit "$WORKER_RESTART_STATUS"
fi

"$SCRIPT_DIR/doctor.sh"
echo "Cycle context admission: checking every enabled live Cycle."
compose exec -T api python3 -m brain.jobs.check_cycle_context_admission --live
schedule_updater_refresh_after_self_update
