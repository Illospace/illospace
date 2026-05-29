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

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

non_worker_services() {
  if [ "$SKIP_UPDATER_RESTART" = "1" ]; then
    printf '%s\n' api scheduler web
  else
    printf '%s\n' api scheduler web updater
  fi
}

all_runtime_services() {
  if [ "$SKIP_UPDATER_RESTART" = "1" ]; then
    printf '%s\n' api worker scheduler web
  else
    printf '%s\n' api worker scheduler web updater
  fi
}

active_agent_run_count() {
  local count
  if count="$(compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "
SELECT count(*) FROM agent_runs WHERE status IN ('\''starting'\'', '\''running'\'', '\''paused'\'', '\''verifying'\'');
"' 2>/dev/null | tr -d '[:space:]')"; then
    if [[ "$count" =~ ^[0-9]+$ ]]; then
      printf '%s\n' "$count"
      return 0
    fi
  fi
  printf 'unknown\n'
}

worker_container_id() {
  compose ps -q worker 2>/dev/null || true
}

start_worker_handoff() {
  compose run \
    -d \
    --no-deps \
    -e ILLO_WORKER_DISABLE_CYCLE_SCHEDULER=1 \
    -e ILLO_AGENT_RUNNER_DRAIN_TIMEOUT_SECONDS="${ILLO_AGENT_RUNNER_DRAIN_TIMEOUT_SECONDS:-infinity}" \
    worker
}

container_running() {
  local id="$1"
  [ -n "$id" ] || return 1
  [ "$(docker inspect --format '{{.State.Running}}' "$id" 2>/dev/null || echo false)" = "true" ]
}

record_worker_drain_timeout() {
  [ -n "$WORKER_DRAIN_TIMEOUT_FILE" ] || return 0
  mkdir -p "$(dirname "$WORKER_DRAIN_TIMEOUT_FILE")" 2>/dev/null || true
  printf '{"status":"worker_draining","updated_at":"%s"}\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    > "$WORKER_DRAIN_TIMEOUT_FILE" 2>/dev/null || true
}

wait_for_worker_exit() {
  local id="$1"
  local wait_seconds="${ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_SECONDS:-86400}"
  local deadline=$((SECONDS + wait_seconds))
  while container_running "$id"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "Worker did not drain within ${wait_seconds}s; leaving it on the old image to avoid killing active AgentRuns." >&2
      record_worker_drain_timeout
      return 1
    fi
    sleep 5
  done
}

update_worker_after_drain() {
  local active_runs="$1"
  local worker_id handoff_id
  worker_id="$(worker_container_id)"

  if [ -z "$worker_id" ]; then
    echo "Worker container is not running; starting worker on the new image."
    compose up -d --force-recreate --no-deps worker
    return 0
  fi

  handoff_id="$(start_worker_handoff)"
  echo "Worker: started handoff worker ${handoff_id:-unknown} on the new image for new AgentRuns."
  echo "Worker: ${active_runs} active AgentRun(s); signaling drain before replacing worker."
  docker update --restart=no "$worker_id" >/dev/null 2>&1 || true
  docker kill -s TERM "$worker_id" >/dev/null 2>&1 || true
  if ! wait_for_worker_exit "$worker_id"; then
    docker update --restart=unless-stopped "$worker_id" >/dev/null 2>&1 || true
    return 0
  fi
  compose up -d --force-recreate --no-deps worker
  if [ -n "$handoff_id" ]; then
    echo "Worker: regular worker is on the new image; draining handoff worker $handoff_id."
    docker kill -s TERM "$handoff_id" >/dev/null 2>&1 || true
    (
      docker wait "$handoff_id" >/dev/null 2>&1 || true
      docker rm "$handoff_id" >/dev/null 2>&1 || true
    ) &
  fi
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
ACTIVE_RUNS="$(active_agent_run_count)"
if [ "$ACTIVE_RUNS" = "0" ]; then
  mapfile -t runtime_services < <(all_runtime_services)
  compose up -d --force-recreate --remove-orphans "${runtime_services[@]}"
else
  echo "Updating API, scheduler, and web while preserving active worker AgentRuns."
  mapfile -t services < <(non_worker_services)
  compose up -d --force-recreate --no-deps "${services[@]}"
  update_worker_after_drain "$ACTIVE_RUNS"
fi

"$SCRIPT_DIR/doctor.sh"
