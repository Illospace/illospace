#!/usr/bin/env bash

COMPOSE_RUNTIME_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ROOT:-$(cd "$COMPOSE_RUNTIME_LIB_DIR/../.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/deploy/compose/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-${ILLO_COMPOSE_ENV_FILE:-$ROOT/deploy/compose/.env}}"
RUNTIME_SERVICE_CATALOG="${RUNTIME_SERVICE_CATALOG:-$ROOT/deploy/compose/runtime-services.json}"

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

runtime_service_ids() {
  jq -r '.services[].id' "$RUNTIME_SERVICE_CATALOG"
}

runtime_service_name() {
  local service="$1"
  local compose_name
  compose_name="$(jq -er --arg id "$service" '.services[] | select(.id == $id) | .compose_service' "$RUNTIME_SERVICE_CATALOG")" || {
    echo "Unknown runtime service id: $service" >&2
    return 2
  }
  printf '%s\n' "$compose_name"
}

normalize_service_id() {
  printf '%s\n' "$1" | tr '[:upper:]-' '[:lower:]_'
}

append_unique_service() {
  local service="$1"
  local existing
  for existing in "${expanded_services[@]:-}"; do
    [ "$existing" != "$service" ] || return 0
  done
  expanded_services+=("$service")
}

expand_runtime_services() {
  local raw service include_all=0
  expanded_services=()
  if [ "$#" -eq 0 ]; then
    echo "At least one runtime service id is required." >&2
    return 2
  fi

  for raw in "$@"; do
    service="$(normalize_service_id "$raw")"
    if [ "$service" = "all" ]; then
      include_all=1
      continue
    fi
    runtime_service_name "$service" >/dev/null
    append_unique_service "$service"
  done

  if [ "$include_all" = "1" ]; then
    while IFS= read -r service; do
      append_unique_service "$service"
    done < <(runtime_service_ids)
  fi

  printf '%s\n' "${expanded_services[@]}"
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

container_running() {
  local id="$1"
  [ -n "$id" ] || return 1
  [ "$(docker inspect --format '{{.State.Running}}' "$id" 2>/dev/null || echo false)" = "true" ]
}

start_worker_handoff() {
  compose run \
    -d \
    --no-deps \
    -e ILLO_WORKER_DISABLE_CYCLE_SCHEDULER=1 \
    -e ILLO_AGENT_RUNNER_DRAIN_TIMEOUT_SECONDS="${ILLO_AGENT_RUNNER_DRAIN_TIMEOUT_SECONDS:-infinity}" \
    worker
}

record_worker_drain_timeout() {
  [ -n "${COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_FILE:-}" ] || return 0
  mkdir -p "$(dirname "$COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_FILE")" 2>/dev/null || true
  printf '{"status":"worker_draining","updated_at":"%s"}\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    > "$COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_FILE" 2>/dev/null || true
}

wait_for_worker_exit() {
  local id="$1"
  local wait_seconds="${COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_SECONDS:-86400}"
  local deadline=$((SECONDS + wait_seconds))
  while container_running "$id"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "Worker did not drain within ${wait_seconds}s; leaving it running to avoid interrupting active AgentRuns." >&2
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
    echo "Worker container is not running; starting worker."
    compose up -d --force-recreate --no-deps worker
    return 0
  fi

  handoff_id="$(start_worker_handoff)"
  echo "Worker: started handoff worker ${handoff_id:-unknown} for new AgentRuns."
  echo "Worker: ${active_runs} active AgentRun(s); signaling existing worker to drain."
  docker update --restart=no "$worker_id" >/dev/null 2>&1 || true
  docker kill -s TERM "$worker_id" >/dev/null 2>&1 || true
  if ! wait_for_worker_exit "$worker_id"; then
    docker update --restart=unless-stopped "$worker_id" >/dev/null 2>&1 || true
    return 0
  fi
  compose up -d --force-recreate --no-deps worker
  if [ -n "$handoff_id" ]; then
    echo "Worker: regular worker is restarted; draining handoff worker $handoff_id."
    docker kill -s TERM "$handoff_id" >/dev/null 2>&1 || true
    (
      docker wait "$handoff_id" >/dev/null 2>&1 || true
      docker rm "$handoff_id" >/dev/null 2>&1 || true
    ) &
  fi
}

replace_idle_worker() {
  local worker_id stop_seconds
  worker_id="$(worker_container_id)"
  stop_seconds="${ILLO_COMPOSE_IDLE_WORKER_STOP_TIMEOUT_SECONDS:-30}"

  if [ -z "$worker_id" ]; then
    echo "Worker container is not running; starting worker."
    compose up -d --force-recreate --no-deps worker
    return 0
  fi

  echo "Worker: no active AgentRuns; replacing worker with a ${stop_seconds}s stop timeout."
  docker update --restart=no "$worker_id" >/dev/null 2>&1 || true
  if ! docker stop -t "$stop_seconds" "$worker_id" >/dev/null 2>&1; then
    echo "Worker did not stop within ${stop_seconds}s despite no active AgentRuns; forcing replacement." >&2
    docker kill "$worker_id" >/dev/null 2>&1 || true
  fi
  compose up -d --force-recreate --no-deps worker
}

restart_runtime_worker_service() {
  local active_runs
  active_runs="$(active_agent_run_count)"
  if [ "$active_runs" = "unknown" ]; then
    echo "Cannot safely restart worker because active AgentRun count is unknown." >&2
    return 1
  fi
  if [ "$active_runs" = "0" ]; then
    replace_idle_worker
  else
    update_worker_after_drain "$active_runs"
  fi
}

restart_runtime_service() {
  local service="$1"
  local compose_name
  if [ "$service" = "worker" ]; then
    restart_runtime_worker_service
    return $?
  fi
  compose_name="$(runtime_service_name "$service")"
  echo "Restarting runtime service: $service"
  compose up -d --force-recreate --no-deps "$compose_name"
}

restart_runtime_services() {
  local service
  if [ ! -f "$ENV_FILE" ]; then
    echo "Missing $ENV_FILE; run ./illo deploy init first." >&2
    return 1
  fi
  mapfile -t services < <(expand_runtime_services "$@")
  if [ "${#services[@]}" -eq 0 ]; then
    echo "No runtime services selected." >&2
    return 2
  fi
  compose ps >/dev/null
  for service in "${services[@]}"; do
    restart_runtime_service "$service"
  done
}
