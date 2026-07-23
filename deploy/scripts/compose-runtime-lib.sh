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

runtime_service_ids_for_all() {
  jq -r '.services[] | select(.include_in_all != false) | .id' "$RUNTIME_SERVICE_CATALOG"
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
    done < <(runtime_service_ids_for_all)
  fi

  printf '%s\n' "${expanded_services[@]}"
}

nonterminal_agent_run_details() {
  local details
  if details="$(compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -AtF: -c "
SELECT id, status FROM agent_runs
WHERE status IN ('\''queued'\'', '\''starting'\'', '\''running'\'', '\''paused'\'', '\''verifying'\'')
ORDER BY id;
"' 2>/dev/null | paste -sd, - | tr -d '[:space:]')"; then
    if [ -z "$details" ] || [[ "$details" =~ ^[0-9]+:(queued|starting|running|paused|verifying)(,[0-9]+:(queued|starting|running|paused|verifying))*$ ]]; then
      printf '%s\n' "$details"
      return 0
    fi
  fi
  printf 'unknown\n'
}

agent_run_count_from_details() {
  local details="$1"
  if [ -z "$details" ]; then
    printf '0\n'
    return 0
  fi
  if [ "$details" = "unknown" ]; then
    printf 'unknown\n'
    return 0
  fi
  awk -F, '{print NF}' <<< "$details"
}

agent_run_ids_from_details() {
  local details="$1"
  [ -n "$details" ] && [ "$details" != "unknown" ] || return 0
  printf '%s\n' "$details" | sed -E 's/:[^,]+//g'
}

active_agent_run_count() {
  local details
  details="$(nonterminal_agent_run_details)"
  agent_run_count_from_details "$details"
}

report_nonterminal_agent_runs() {
  local details="$1"
  local count ids
  count="$(agent_run_count_from_details "$details")"
  ids="$(agent_run_ids_from_details "$details")"
  echo "Worker pre-swap check: $count interactive run(s) in flight (run ids: $ids; id/status: $details)."
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
  local details="${1:-unknown}"
  local ids
  [ -n "${COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_FILE:-}" ] || return 0
  ids="$(agent_run_ids_from_details "$details")"
  mkdir -p "$(dirname "$COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_FILE")" 2>/dev/null || true
  printf '{"status":"worker_draining","updated_at":"%s","affected_run_ids":"%s","affected_runs":"%s"}\n' \
    "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$ids" "$details" \
    > "$COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_FILE" 2>/dev/null || true
}

wait_for_worker_exit() {
  local id="$1"
  local wait_seconds="${COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_SECONDS:-86400}"
  local started_at="$SECONDS"
  local deadline=$((started_at + wait_seconds))
  local wait_iterations=0
  local consecutive_zero_checks=0
  local hint_printed=0
  local active_runs affected_runs affected_ids elapsed
  while container_running "$id"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      affected_runs="$(nonterminal_agent_run_details)"
      affected_ids="$(agent_run_ids_from_details "$affected_runs")"
      echo "Worker did not drain within ${wait_seconds}s; refusing to kill it. Affected run ids: ${affected_ids:-unknown} (id/status: $affected_runs)." >&2
      record_worker_drain_timeout "$affected_runs"
      return 1
    fi
    sleep 5
    wait_iterations=$((wait_iterations + 1))
    if [ $((wait_iterations % 6)) -eq 0 ] && container_running "$id"; then
      active_runs="$(active_agent_run_count)"
      if [ "$active_runs" = "0" ]; then
        consecutive_zero_checks=$((consecutive_zero_checks + 1))
        if [ "$consecutive_zero_checks" -ge 2 ] && [ "$hint_printed" = "0" ]; then
          elapsed=$((SECONDS - started_at))
          echo "Hint: worker has 0 active AgentRuns but its process has not exited after ${elapsed}s." >&2
          echo "Do not force-remove this worker; the deploy remains blocked until it exits safely." >&2
          hint_printed=1
        fi
      else
        consecutive_zero_checks=0
      fi
    fi
  done
}

update_worker_after_drain() {
  local run_details="$1"
  local already_reported="${2:-}"
  local active_runs affected_ids worker_id handoff_id
  active_runs="$(agent_run_count_from_details "$run_details")"
  affected_ids="$(agent_run_ids_from_details "$run_details")"
  worker_id="$(worker_container_id)"
  if [ "$already_reported" != "reported" ]; then
    report_nonterminal_agent_runs "$run_details"
  fi

  if [ -z "$worker_id" ]; then
    echo "Worker container is not running; starting worker to recover affected run ids: $affected_ids."
    compose up -d --force-recreate --no-deps worker
    return 0
  fi

  handoff_id="$(start_worker_handoff)"
  echo "Worker: started handoff worker ${handoff_id:-unknown} for new AgentRuns."
  echo "Worker: ${active_runs} interactive AgentRun(s); signaling existing worker to drain. Affected run ids: $affected_ids."
  docker update --restart=no "$worker_id" >/dev/null 2>&1 || true
  docker kill -s TERM "$worker_id" >/dev/null 2>&1 || true
  if ! wait_for_worker_exit "$worker_id"; then
    docker update --restart=unless-stopped "$worker_id" >/dev/null 2>&1 || true
    if [ "${ILLO_COMPOSE_FORCE_WORKER_SWAP:-0}" != "1" ]; then
      return 1
    fi
    run_details="$(nonterminal_agent_run_details)"
    affected_ids="$(agent_run_ids_from_details "$run_details")"
    echo "FORCED WORKER SWAP: killing old worker; affected run ids: ${affected_ids:-unknown} (id/status: $run_details)." >&2
    docker update --restart=no "$worker_id" >/dev/null 2>&1 || true
    docker kill "$worker_id" >/dev/null 2>&1 || true
  fi
  compose up -d --force-recreate --no-deps worker
  if [ -n "$handoff_id" ]; then
    run_details="$(nonterminal_agent_run_details)"
    affected_ids="$(agent_run_ids_from_details "$run_details")"
    echo "Worker: regular worker is restarted; draining handoff worker $handoff_id. Open run ids at handoff shutdown: ${affected_ids:-none}."
    docker kill -s TERM "$handoff_id" >/dev/null 2>&1 || true
    (
      docker wait "$handoff_id" >/dev/null 2>&1 || true
      docker rm "$handoff_id" >/dev/null 2>&1 || true
    ) &
  fi
}

replace_idle_worker() {
  local run_details affected_ids worker_id
  worker_id="$(worker_container_id)"

  if [ -z "$worker_id" ]; then
    echo "Worker container is not running; starting worker."
    compose up -d --force-recreate --no-deps worker
    return 0
  fi

  run_details="$(nonterminal_agent_run_details)"
  if [ "$run_details" = "unknown" ]; then
    echo "Cannot safely replace worker because the non-terminal AgentRun ids are unknown." >&2
    return 1
  fi
  if [ -n "$run_details" ]; then
    affected_ids="$(agent_run_ids_from_details "$run_details")"
    echo "Worker replacement blocked: interactive runs appeared after the pre-swap check. Affected run ids: $affected_ids (id/status: $run_details)." >&2
    return 1
  fi

  echo "Worker: no interactive AgentRuns; signaling a graceful replacement."
  docker update --restart=no "$worker_id" >/dev/null 2>&1 || true
  docker kill -s TERM "$worker_id" >/dev/null 2>&1 || true
  if ! wait_for_worker_exit "$worker_id"; then
    docker update --restart=unless-stopped "$worker_id" >/dev/null 2>&1 || true
    return 1
  fi
  compose up -d --force-recreate --no-deps worker
}

restart_runtime_worker_service() {
  local run_details
  run_details="$(nonterminal_agent_run_details)"
  if [ "$run_details" = "unknown" ]; then
    echo "Cannot safely restart worker because non-terminal AgentRun ids are unknown." >&2
    return 1
  fi
  if [ -z "$run_details" ]; then
    replace_idle_worker
  else
    update_worker_after_drain "$run_details"
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
