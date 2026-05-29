#!/usr/bin/env bash
set -euo pipefail

REPO="${ILLO_SELF_UPDATE_REPO:-/repo}"
REQUEST_FILE="${ILLO_SELF_UPDATE_REQUEST_FILE:-/data/private/self-update/request.json}"
STATUS_FILE="${ILLO_SELF_UPDATE_STATUS_FILE:-/data/private/self-update/status.json}"
HEARTBEAT_FILE="${ILLO_SELF_UPDATE_HEARTBEAT_FILE:-/data/private/self-update/heartbeat.json}"
LOG_PATH="${ILLO_SELF_UPDATE_LOG_PATH:-/data/private/logs/illo-self-update.log}"
WORKER_DRAIN_TIMEOUT_FILE="${ILLO_SELF_UPDATE_WORKER_DRAIN_TIMEOUT_FILE:-/data/private/self-update/worker-drain-timeout.json}"
POLL_SECONDS="${ILLO_SELF_UPDATE_POLL_SECONDS:-2}"
APP_UID="${ILLO_APP_UID:-10001}"
APP_GID="${ILLO_APP_GID:-10001}"
RUNNING_FILE="${REQUEST_FILE}.running"

prepare_shared_paths() {
  mkdir -p "$(dirname "$REQUEST_FILE")" "$(dirname "$STATUS_FILE")" "$(dirname "$HEARTBEAT_FILE")" "$(dirname "$LOG_PATH")"
  if [ "$(id -u)" = "0" ]; then
    chown "$APP_UID:$APP_GID" "$(dirname "$REQUEST_FILE")" "$(dirname "$STATUS_FILE")" "$(dirname "$HEARTBEAT_FILE")" 2>/dev/null || true
    chmod 0775 "$(dirname "$REQUEST_FILE")" "$(dirname "$STATUS_FILE")" "$(dirname "$HEARTBEAT_FILE")" 2>/dev/null || true
  fi
  git config --global --add safe.directory "$REPO" >/dev/null 2>&1 || true
}

json_write() {
  local path="$1"
  shift
  mkdir -p "$(dirname "$path")"
  jq -n "$@" > "${path}.tmp"
  mv "${path}.tmp" "$path"
}

write_status() {
  local status="$1"
  local detail="$2"
  local requested_at="${3:-}"
  local requested_by="${4:-}"
  local exit_code="${5:-}"
  json_write "$STATUS_FILE" \
    --arg status "$status" \
    --arg detail "$detail" \
    --arg requested_at "$requested_at" \
    --arg requested_by "$requested_by" \
    --arg updated_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    --arg exit_code "$exit_code" \
    '{
      status: $status,
      detail: $detail,
      requested_at: (if $requested_at | length > 0 then $requested_at else null end),
      started_at: (if $requested_at | length > 0 then $requested_at else null end),
      requested_by: (if $requested_by | length > 0 then $requested_by else null end),
      updated_at: $updated_at,
      exit_code: (if $exit_code | length > 0 then ($exit_code | tonumber) else null end)
    }'
}

write_heartbeat() {
  json_write "$HEARTBEAT_FILE" \
    --arg updated_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    '{status: "ready", updated_at: $updated_at}'
}

process_request() {
  local requested_at requested_by build_no_cache worker_drain_timeout_seconds exit_code
  if ! mv "$REQUEST_FILE" "$RUNNING_FILE" 2>/dev/null; then
    return 0
  fi

  requested_at="$(jq -r '.requested_at // ""' "$RUNNING_FILE" 2>/dev/null || true)"
  requested_by="$(jq -r '.requested_by // ""' "$RUNNING_FILE" 2>/dev/null || true)"
  build_no_cache="$(jq -r 'if .build_no_cache == true then "1" else "" end' "$RUNNING_FILE" 2>/dev/null || true)"
  worker_drain_timeout_seconds="$(jq -r '.worker_drain_timeout_seconds // ""' "$RUNNING_FILE" 2>/dev/null || true)"
  write_status "running" "Illospace update is syncing origin/main, running migrations, and recreating runtime services." "$requested_at" "$requested_by"

  mkdir -p "$(dirname "$LOG_PATH")"
  rm -f "$WORKER_DRAIN_TIMEOUT_FILE"
  {
    echo
    echo "=== Illospace Compose self-update started at $(date -u +"%Y-%m-%dT%H:%M:%SZ") ==="
    [ -z "$requested_by" ] || echo "Requested by: $requested_by"
    [ -z "$build_no_cache" ] || echo "Build no-cache: enabled"
    cd "$REPO"
    ILLO_UPDATE_MODE=compose \
      ILLO_COMPOSE_SKIP_UPDATER_RESTART=1 \
      ILLO_COMPOSE_BUILD_NO_CACHE="${build_no_cache:-${ILLO_COMPOSE_BUILD_NO_CACHE:-0}}" \
      ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_FILE="$WORKER_DRAIN_TIMEOUT_FILE" \
      ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_SECONDS="${worker_drain_timeout_seconds:-${ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_SECONDS:-120}}" \
      bash ./illo update --mode compose
  } >> "$LOG_PATH" 2>&1
  exit_code=$?

  if [ "$exit_code" -eq 0 ]; then
    if [ -f "$WORKER_DRAIN_TIMEOUT_FILE" ]; then
      write_status "idle" "Illospace update completed; active AgentRuns are still draining on the old worker while new runs use a handoff worker." "$requested_at" "$requested_by" "$exit_code"
    else
      write_status "idle" "Illospace update completed." "$requested_at" "$requested_by" "$exit_code"
    fi
  else
    write_status "idle" "Illospace update failed with exit code $exit_code. Check the update log." "$requested_at" "$requested_by" "$exit_code"
  fi
  rm -f "$RUNNING_FILE"
}

prepare_shared_paths
write_status "idle" "Compose updater sidecar is ready." "" ""

while true; do
  write_heartbeat
  if [ -f "$REQUEST_FILE" ]; then
    set +e
    process_request
    exit_code=$?
    set -e
    if [ "$exit_code" -ne 0 ]; then
      write_status "idle" "Illospace update failed with exit code $exit_code. Check the update log." "" "" "$exit_code"
      rm -f "$RUNNING_FILE"
    fi
  fi
  sleep "$POLL_SECONDS"
done
