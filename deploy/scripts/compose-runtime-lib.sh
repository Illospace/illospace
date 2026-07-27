#!/usr/bin/env bash

COMPOSE_RUNTIME_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${ROOT:-$(cd "$COMPOSE_RUNTIME_LIB_DIR/../.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/deploy/compose/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-${ILLO_COMPOSE_ENV_FILE:-$ROOT/deploy/compose/.env}}"
RUNTIME_SERVICE_CATALOG="${RUNTIME_SERVICE_CATALOG:-$ROOT/deploy/compose/runtime-services.json}"
WORKER_SWAP_PYTHON_BIN="${WORKER_SWAP_PYTHON_BIN:-python3}"

source "$COMPOSE_RUNTIME_LIB_DIR/worker-swap-lib.sh"

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

worker_swap_snapshot_acquire() {
  local rows
  rows="$(
    worker_swap_contract sql \
      | compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' 2>/dev/null
  )" || return 1
  printf '%s\n' "$rows" | worker_swap_contract from-rows
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

worker_container_id() {
  compose ps -q worker 2>/dev/null || true
}

container_running() {
  local id="$1"
  [ -n "$id" ] || return 1
  [ "$(docker inspect --format '{{.State.Running}}' "$id" 2>/dev/null || echo false)" = "true" ]
}

container_restart_policy() {
  local id="$1"
  [ -n "$id" ] || return 1
  docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$id" 2>/dev/null
}

# The policy the worker is declared with in docker-compose.yml. Everything below
# treats this as the value a worker container must be left holding, so that a
# host reboot brings the worker back with the rest of the stack.
worker_declared_restart_policy() {
  printf '%s\n' "${COMPOSE_RUNTIME_WORKER_RESTART_POLICY:-unless-stopped}"
}

WORKER_RESTART_POLICY_SUSPENDED_ID=""

# A worker that is about to be drained must not be resurrected by the daemon the
# moment it exits: that races the handoff worker and doubles concurrency (#486).
# Suspending the policy is therefore deliberate — but the suspension is a
# persistent mutation of the container, so an interrupted deploy (Ctrl-C, SSH
# drop, dockerd snap refresh, power cut) used to strand the worker at
# restart=no. The Compose file still read `unless-stopped`, nothing reconciled
# the drift, and the next reboot silently dropped the worker (#527). The trap
# makes the window crash-safe.
suspend_worker_restart_policy() {
  local id="$1"
  local existing
  [ -n "$id" ] || return 0
  WORKER_RESTART_POLICY_SUSPENDED_ID="$id"
  # Chain onto whatever EXIT trap the sourcing script already installed instead
  # of clobbering it -- doctor.sh installs its own cleanup.
  existing="$(trap -p EXIT | sed -n "s/^trap -- '\(.*\)' EXIT\$/\1/p")"
  case "$existing" in
    ""|*restore_worker_restart_policy*)
      trap 'restore_worker_restart_policy' EXIT
      ;;
    *)
      trap "restore_worker_restart_policy; $existing" EXIT
      ;;
  esac
  trap 'restore_worker_restart_policy' INT TERM HUP
  docker update --restart=no "$id" >/dev/null 2>&1 || true
}

restore_worker_restart_policy() {
  local id="${WORKER_RESTART_POLICY_SUSPENDED_ID:-}"
  [ -n "$id" ] || return 0
  WORKER_RESTART_POLICY_SUSPENDED_ID=""
  docker update --restart="$(worker_declared_restart_policy)" "$id" >/dev/null 2>&1 || true
}

# Called once the suspended container has been replaced by a fresh one. The
# replacement already carries the declared policy, and restoring the policy on
# the outgoing container would arm a second worker to come back on the next
# reboot -- so drop the suspension without restoring it.
abandon_worker_restart_policy_suspension() {
  WORKER_RESTART_POLICY_SUSPENDED_ID=""
}

# Self-heal a worker stranded by an interruption in an earlier deploy. Drift is
# invisible to `docker ps` and to the Compose file, so it is only ever found by
# asking the container directly.
reconcile_worker_restart_policy() {
  local id declared policy
  id="$(worker_container_id)"
  [ -n "$id" ] || return 0
  declared="$(worker_declared_restart_policy)"
  policy="$(container_restart_policy "$id" || true)"
  [ -n "$policy" ] || return 0
  [ "$policy" != "$declared" ] || return 0
  echo "Worker: restart policy had drifted to '${policy}' (declared '${declared}'); repairing it so the worker survives a host reboot." >&2
  docker update --restart="$declared" "$id" >/dev/null 2>&1 || true
}

assert_single_running_worker() {
  local count=0 id running_ids running_ids_csv
  if ! running_ids="$(compose ps --status running -q worker 2>/dev/null)"; then
    echo "Worker invariant check failed: could not list running worker containers." >&2
    echo "Recovery: inspect workers with: docker compose --env-file \"$ENV_FILE\" -f \"$COMPOSE_FILE\" ps --all worker" >&2
    return 1
  fi
  while IFS= read -r id; do
    [ -z "$id" ] || count=$((count + 1))
  done <<< "$running_ids"
  [ "$count" -eq 1 ] && return 0

  running_ids_csv="${running_ids//$'\n'/,}"
  echo "Worker invariant failed: expected exactly one running worker container, found $count (container ids: ${running_ids_csv:-none})." >&2
  echo "Recovery: inspect workers with: docker compose --env-file \"$ENV_FILE\" -f \"$COMPOSE_FILE\" ps --all worker" >&2
  if [ "$count" -eq 0 ]; then
    echo "Recovery: start the regular worker with: docker compose --env-file \"$ENV_FILE\" -f \"$COMPOSE_FILE\" up -d --no-deps worker" >&2
  else
    echo "Recovery: keep the intended regular worker, stop and remove every extra worker container, then rerun the failed command." >&2
  fi
  return 1
}

# Services that must be running whenever the stack is up at all. The worker is
# the dangerous one: it owns AgentRun execution AND the cycle-scheduler thread,
# so when it alone is missing every outward signal still reads healthy --
# `docker ps` is green, /api/health returns 200, the dashboard loads -- while
# Illo cannot do any work. That state has to be asserted explicitly; no probe
# infers it.
STACK_REQUIRED_SERVICES="${STACK_REQUIRED_SERVICES:-postgres api web worker scheduler}"

# Distinct exit codes so a monitor can tell an invisible failure from an obvious
# one: 3 = inert (stack up, required service missing), 4 = fully down.
STACK_INERT_EXIT_CODE=3
STACK_DOWN_EXIT_CODE=4

assert_stack_not_inert() {
  local required="${*:-$STACK_REQUIRED_SERVICES}"
  local running service missing="" running_count=0 line

  if ! running="$(compose ps --services --status running 2>/dev/null)"; then
    echo "Stack presence check failed: could not list running Compose services." >&2
    return 1
  fi

  while IFS= read -r line; do
    [ -z "$line" ] || running_count=$((running_count + 1))
  done <<< "$running"

  if [ "$running_count" -eq 0 ]; then
    echo "Stack is fully down: no Compose services are running." >&2
    echo "Recovery: start it with: docker compose --env-file \"$ENV_FILE\" -f \"$COMPOSE_FILE\" up -d" >&2
    return "$STACK_DOWN_EXIT_CODE"
  fi

  for service in $required; do
    printf '%s\n' "$running" | grep -qx "$service" && continue
    missing="${missing:+$missing,}$service"
  done

  if [ -z "$missing" ]; then
    return 0
  fi

  echo "Stack is INERT: ${running_count} service(s) running but required service(s) absent: ${missing}." >&2
  case ",$missing," in
    *,worker,*)
      echo "The worker owns AgentRun execution and the cycle-scheduler thread, so Illo is structurally incapable of doing any work while it is absent -- even though HTTP health checks still pass." >&2
      ;;
  esac
  echo "Recovery: docker compose --env-file \"$ENV_FILE\" -f \"$COMPOSE_FILE\" up -d --no-deps ${missing//,/ }" >&2
  return "$STACK_INERT_EXIT_CODE"
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
  local snapshot="$1"
  local details ids
  [ -n "${COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_FILE:-}" ] || return 0
  details="$(worker_swap_snapshot_details "$snapshot")"
  ids="$(worker_swap_snapshot_run_ids "$snapshot")"
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
  local active_runs affected_runs affected_ids elapsed snapshot
  while container_running "$id"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      snapshot="$(worker_swap_snapshot)"
      affected_runs="$(worker_swap_snapshot_details "$snapshot")"
      affected_ids="$(worker_swap_snapshot_run_ids "$snapshot")"
      echo "Worker did not drain within ${wait_seconds}s; refusing to kill it. Affected run ids: ${affected_ids:-unknown} (id/status: $affected_runs)." >&2
      record_worker_drain_timeout "$snapshot"
      return 1
    fi
    sleep 5
    wait_iterations=$((wait_iterations + 1))
    if [ $((wait_iterations % 6)) -eq 0 ] && container_running "$id"; then
      snapshot="$(worker_swap_snapshot)"
      active_runs="$(worker_swap_snapshot_count "$snapshot")"
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

remove_worker_handoff_after_drain_timeout() {
  local handoff_id="$1"
  local worker_id="$2"
  echo "Worker: drain timed out; stopping and removing temporary handoff worker $handoff_id before returning." >&2
  docker update --restart=no "$handoff_id" >/dev/null 2>&1 || true
  docker kill "$handoff_id" >/dev/null 2>&1 || true
  if ! docker rm -f "$handoff_id" >/dev/null 2>&1; then
    if docker inspect "$handoff_id" >/dev/null 2>&1; then
      echo "Worker: could not remove temporary handoff worker $handoff_id; multiple workers may still be running." >&2
      return 1
    fi
  fi
  echo "Worker: removed temporary handoff worker $handoff_id; original worker $worker_id is retained as the intended sole worker container." >&2
  echo "Recovery: let the original worker finish its active AgentRuns, then rerun the failed worker restart or upgrade. New AgentRuns may remain queued until the original worker restarts." >&2
}

update_worker_after_drain() {
  local snapshot="$1"
  local already_reported="${2:-}"
  local active_runs affected_ids worker_id handoff_id run_details
  active_runs="$(worker_swap_snapshot_count "$snapshot")"
  affected_ids="$(worker_swap_snapshot_run_ids "$snapshot")"
  run_details="$(worker_swap_snapshot_details "$snapshot")"
  worker_id="$(worker_container_id)"
  if [ "$already_reported" != "reported" ]; then
    echo "$(worker_swap_snapshot_report "$snapshot")."
  fi

  if [ -z "$worker_id" ]; then
    echo "Worker container is not running; starting worker to recover affected run ids: $affected_ids."
    compose up -d --force-recreate --no-deps worker
    return 0
  fi

  handoff_id="$(start_worker_handoff)"
  echo "Worker: started handoff worker ${handoff_id:-unknown} for new AgentRuns."
  echo "Worker: ${active_runs} interactive AgentRun(s); signaling existing worker to drain. Affected run ids: $affected_ids."
  suspend_worker_restart_policy "$worker_id"
  docker kill -s TERM "$worker_id" >/dev/null 2>&1 || true
  if ! wait_for_worker_exit "$worker_id"; then
    restore_worker_restart_policy
    if [ "${ILLO_COMPOSE_FORCE_WORKER_SWAP:-0}" != "1" ]; then
      remove_worker_handoff_after_drain_timeout "$handoff_id" "$worker_id" || true
      return 1
    fi
    snapshot="$(worker_swap_snapshot)"
    run_details="$(worker_swap_snapshot_details "$snapshot")"
    affected_ids="$(worker_swap_snapshot_run_ids "$snapshot")"
    echo "FORCED WORKER SWAP: killing old worker; affected run ids: ${affected_ids:-unknown} (id/status: $run_details)." >&2
    suspend_worker_restart_policy "$worker_id"
    docker kill "$worker_id" >/dev/null 2>&1 || true
  fi
  # Only drop the suspension once a replacement actually exists. If the recreate
  # failed there is no new worker, so the outgoing container is still the only
  # one -- leaving the suspension armed lets the trap hand it back its restart
  # policy instead of stranding it at restart=no.
  if compose up -d --force-recreate --no-deps worker; then
    abandon_worker_restart_policy_suspension
  fi
  if [ -n "$handoff_id" ]; then
    snapshot="$(worker_swap_snapshot)"
    affected_ids="$(worker_swap_snapshot_run_ids "$snapshot")"
    echo "Worker: regular worker is restarted; draining handoff worker $handoff_id. Open run ids at handoff shutdown: ${affected_ids:-none}."
    docker kill -s TERM "$handoff_id" >/dev/null 2>&1 || true
    remove_worker_handoff_bounded "$handoff_id"
  fi
}

# Reap the temporary handoff worker without blocking the deploy indefinitely.
# It runs with ILLO_AGENT_RUNNER_DRAIN_TIMEOUT_SECONDS=infinity, so a graceful
# SIGTERM drain lasts as long as its longest in-flight AgentRun (observed at
# 40-100 minutes). The regular worker is already up by this point, and runs
# interrupted here are requeued by the stale-run reaper, so bound the wait and
# then force removal rather than hanging the caller.
remove_worker_handoff_bounded() {
  local handoff_id="$1"
  local wait_seconds="${COMPOSE_RUNTIME_HANDOFF_REAP_TIMEOUT_SECONDS:-120}"
  local deadline=$((SECONDS + wait_seconds))
  while container_running "$handoff_id"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "Worker: handoff worker $handoff_id still draining after ${wait_seconds}s; forcing removal so exactly one worker remains. Its open AgentRuns are requeued by the stale-run reaper." >&2
      break
    fi
    sleep 5
  done
  docker rm -f "$handoff_id" >/dev/null 2>&1 || true
}

replace_idle_worker() {
  local action affected_ids run_details snapshot worker_id
  worker_id="$(worker_container_id)"

  if [ -z "$worker_id" ]; then
    echo "Worker container is not running; starting worker."
    compose up -d --force-recreate --no-deps worker
    return 0
  fi

  snapshot="$(worker_swap_snapshot)"
  action="$(worker_swap_snapshot_decision "$snapshot")"
  if [ "$action" = "unknown" ]; then
    echo "Cannot safely replace worker because the non-terminal AgentRun ids are unknown." >&2
    return 1
  fi
  if [ "$action" = "drain" ]; then
    run_details="$(worker_swap_snapshot_details "$snapshot")"
    affected_ids="$(worker_swap_snapshot_run_ids "$snapshot")"
    echo "Worker replacement blocked: interactive runs appeared after the pre-swap check. Affected run ids: $affected_ids (id/status: $run_details)." >&2
    return 1
  fi

  echo "Worker: no interactive AgentRuns; signaling a graceful replacement."
  suspend_worker_restart_policy "$worker_id"
  docker kill -s TERM "$worker_id" >/dev/null 2>&1 || true
  if ! wait_for_worker_exit "$worker_id"; then
    restore_worker_restart_policy
    return 1
  fi
  if compose up -d --force-recreate --no-deps worker; then
    abandon_worker_restart_policy_suspension
  fi
}

restart_runtime_worker_service() {
  local action snapshot status=0
  reconcile_worker_restart_policy
  snapshot="$(worker_swap_snapshot)"
  action="$(worker_swap_snapshot_decision "$snapshot")"
  case "$action" in
    replace) replace_idle_worker || status=$? ;;
    drain) update_worker_after_drain "$snapshot" || status=$? ;;
    *)
      echo "Cannot safely restart worker because non-terminal AgentRun ids are unknown." >&2
      return 1
      ;;
  esac
  assert_single_running_worker || status=1
  return "$status"
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
