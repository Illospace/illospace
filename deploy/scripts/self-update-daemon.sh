#!/usr/bin/env bash
set -euo pipefail

REPO="${ILLO_SELF_UPDATE_REPO:-/repo}"
REQUEST_FILE="${ILLO_SELF_UPDATE_REQUEST_FILE:-/data/private/self-update/request.json}"
STATUS_FILE="${ILLO_SELF_UPDATE_STATUS_FILE:-/data/private/self-update/status.json}"
HEARTBEAT_FILE="${ILLO_SELF_UPDATE_HEARTBEAT_FILE:-/data/private/self-update/heartbeat.json}"
LOG_PATH="${ILLO_SELF_UPDATE_LOG_PATH:-/data/private/logs/illo-self-update.log}"
POLL_SECONDS="${ILLO_SELF_UPDATE_POLL_SECONDS:-2}"
RUNNING_FILE="${REQUEST_FILE}.running"

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
  local requested_at requested_by exit_code
  if ! mv "$REQUEST_FILE" "$RUNNING_FILE" 2>/dev/null; then
    return 0
  fi

  requested_at="$(jq -r '.requested_at // ""' "$RUNNING_FILE" 2>/dev/null || true)"
  requested_by="$(jq -r '.requested_by // ""' "$RUNNING_FILE" 2>/dev/null || true)"
  write_status "running" "Illospace update is running." "$requested_at" "$requested_by"

  mkdir -p "$(dirname "$LOG_PATH")"
  {
    echo
    echo "=== Illospace Compose self-update started at $(date -u +"%Y-%m-%dT%H:%M:%SZ") ==="
    [ -z "$requested_by" ] || echo "Requested by: $requested_by"
    cd "$REPO"
    ILLO_UPDATE_MODE=compose ILLO_COMPOSE_SKIP_UPDATER_RESTART=1 bash ./illo update --mode compose
  } >> "$LOG_PATH" 2>&1
  exit_code=$?

  if [ "$exit_code" -eq 0 ]; then
    write_status "idle" "Illospace update completed." "$requested_at" "$requested_by" "$exit_code"
  else
    write_status "idle" "Illospace update failed with exit code $exit_code. Check the update log." "$requested_at" "$requested_by" "$exit_code"
  fi
  rm -f "$RUNNING_FILE"
}

mkdir -p "$(dirname "$REQUEST_FILE")" "$(dirname "$STATUS_FILE")" "$(dirname "$HEARTBEAT_FILE")" "$(dirname "$LOG_PATH")"
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
