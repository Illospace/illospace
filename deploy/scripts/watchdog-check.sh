#!/usr/bin/env bash
set -uo pipefail

# Reconciles missing services and restarts unhealthy containers without ever
# recreating a running service. The updater's in-flight marker gates every
# mutation so this host-level backstop cannot interfere with a deploy.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$ROOT/deploy/compose/docker-compose.yml"
ENV_FILE="${ILLO_COMPOSE_ENV_FILE:-$ROOT/deploy/compose/.env}"
INERT_CHECK="${ILLO_WATCHDOG_INERT_CHECK:-$SCRIPT_DIR/inert-stack-check.sh}"
SELF_UPDATE_RUNNING_FILE="${ILLO_SELF_UPDATE_RUNNING_FILE:-${ILLO_SELF_UPDATE_REQUEST_FILE:-/data/private/self-update/request.json}.running}"

DOCKER_BIN="${ILLO_WATCHDOG_DOCKER_BIN:-$(command -v docker || true)}"
if [ -z "$DOCKER_BIN" ]; then
  echo "Illospace watchdog: docker is not installed or not on PATH." >&2
  exit 1
fi
PATH="$(dirname "$DOCKER_BIN"):$PATH"

source "$SCRIPT_DIR/compose-runtime-lib.sh"

log() {
  printf 'Illospace watchdog: %s\n' "$*"
}

updater_container_id() {
  local ids id
  if ! ids="$(compose ps -q updater 2>/dev/null)"; then
    return 2
  fi
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    container_is_oneoff "$id" && continue
    printf '%s\n' "$id"
    return 0
  done <<< "$ids"
  return 1
}

update_state() {
  local updater_id status state
  if updater_id="$(updater_container_id)"; then
    :
  else
    status=$?
    if [ "$status" -eq 1 ]; then
      printf 'idle\n'
    else
      printf 'unknown\n'
    fi
    return 0
  fi

  if ! state="$($DOCKER_BIN exec "$updater_id" sh -c '
    if [ -f "$1" ]; then
      printf "running\\n"
    else
      printf "idle\\n"
    fi
  ' sh "$SELF_UPDATE_RUNNING_FILE" 2>/dev/null)"; then
    printf 'unknown\n'
    return 0
  fi
  case "$state" in
    running|idle) printf '%s\n' "$state" ;;
    *) printf 'unknown\n' ;;
  esac
}

require_idle_update_window() {
  local state
  state="$(update_state)"
  case "$state" in
    idle)
      return 0
      ;;
    running)
      log "self-update is in flight; skipping recovery actions."
      return 10
      ;;
    *)
      log "could not inspect the self-update marker; skipping recovery actions." >&2
      return 1
      ;;
  esac
}

check_action_window() {
  local status=0
  require_idle_update_window || status=$?
  case "$status" in
    0) return 0 ;;
    10) return 10 ;;
    *) return 1 ;;
  esac
}

restart_unhealthy_services() {
  local services service ids id health gate_status
  if ! services="$(compose ps --services --status running 2>/dev/null)"; then
    log "could not list running Compose services." >&2
    return 1
  fi

  while IFS= read -r service; do
    [ -n "$service" ] || continue
    if ! ids="$(compose ps -q "$service" 2>/dev/null)"; then
      log "could not inspect service $service." >&2
      return 1
    fi
    while IFS= read -r id; do
      [ -n "$id" ] || continue
      container_is_oneoff "$id" && continue
      if ! health="$($DOCKER_BIN inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$id" 2>/dev/null)"; then
        log "could not inspect container $id for service $service." >&2
        return 1
      fi
      [ "$health" = "unhealthy" ] || continue

      gate_status=0
      check_action_window || gate_status=$?
      case "$gate_status" in
        0) ;;
        10) return 0 ;;
        *) return 1 ;;
      esac
      log "restarting unhealthy service $service."
      if ! compose restart "$service"; then
        log "failed to restart service $service." >&2
        return 1
      fi
      break
    done <<< "$ids"
  done <<< "$services"
}

main() {
  local gate_status=0 inert_status=0

  if [ ! -f "$ENV_FILE" ]; then
    log "missing $ENV_FILE; run ./illo deploy init first." >&2
    return 1
  fi

  check_action_window || gate_status=$?
  case "$gate_status" in
    0) ;;
    10) return 0 ;;
    *) return 1 ;;
  esac

  "$INERT_CHECK" || inert_status=$?
  case "$inert_status" in
    0)
      ;;
    "$STACK_INERT_EXIT_CODE"|"$STACK_DOWN_EXIT_CODE")
      gate_status=0
      check_action_window || gate_status=$?
      case "$gate_status" in
        0) ;;
        10) return 0 ;;
        *) return 1 ;;
      esac
      log "reconciling the Compose stack after inert check exit $inert_status."
      if ! compose up -d; then
        log "Compose reconciliation failed." >&2
        return 1
      fi
      ;;
    *)
      log "inert stack inspection failed with exit $inert_status; no recovery action taken." >&2
      return "$inert_status"
      ;;
  esac

  restart_unhealthy_services
}

main "$@"
