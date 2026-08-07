#!/usr/bin/env bash
set -euo pipefail

REPO="${ILLO_SELF_UPDATE_REPO:-/repo}"
REQUEST_FILE="${ILLO_SELF_UPDATE_REQUEST_FILE:-/data/private/self-update/request.json}"
STATUS_FILE="${ILLO_SELF_UPDATE_STATUS_FILE:-/data/private/self-update/status.json}"
HEARTBEAT_FILE="${ILLO_SELF_UPDATE_HEARTBEAT_FILE:-/data/private/self-update/heartbeat.json}"
LOG_PATH="${ILLO_SELF_UPDATE_LOG_PATH:-/data/private/logs/illo-self-update.log}"
RUNTIME_SERVICES_REQUEST_FILE="${ILLO_RUNTIME_SERVICES_REQUEST_FILE:-/data/private/runtime-services/request.json}"
RUNTIME_SERVICES_STATUS_FILE="${ILLO_RUNTIME_SERVICES_STATUS_FILE:-/data/private/runtime-services/status.json}"
RUNTIME_SERVICES_HEARTBEAT_FILE="${ILLO_RUNTIME_SERVICES_HEARTBEAT_FILE:-/data/private/runtime-services/heartbeat.json}"
RUNTIME_SERVICES_LOG_PATH="${ILLO_RUNTIME_SERVICES_LOG_PATH:-/data/private/logs/illo-runtime-services.log}"
WORKSPACE_TOOLS_REQUEST_FILE="${ILLO_WORKSPACE_TOOLS_REQUEST_FILE:-/data/private/workspace-tools/request.json}"
WORKSPACE_TOOLS_STATUS_FILE="${ILLO_WORKSPACE_TOOLS_STATUS_FILE:-/data/private/workspace-tools/status.json}"
WORKSPACE_TOOLS_HEARTBEAT_FILE="${ILLO_WORKSPACE_TOOLS_HEARTBEAT_FILE:-/data/private/workspace-tools/heartbeat.json}"
WORKSPACE_TOOLS_LOG_PATH="${ILLO_WORKSPACE_TOOLS_LOG_PATH:-/data/private/logs/illo-workspace-tools.log}"
WORKER_DRAIN_TIMEOUT_FILE="${ILLO_SELF_UPDATE_WORKER_DRAIN_TIMEOUT_FILE:-/data/private/self-update/worker-drain-timeout.json}"
POLL_SECONDS="${ILLO_SELF_UPDATE_POLL_SECONDS:-2}"
AUTO_UPDATE_ENABLED="${ILLO_AUTO_UPDATE_ENABLED:-1}"
AUTO_UPDATE_POLL_SECONDS="${ILLO_AUTO_UPDATE_POLL_SECONDS:-300}"
APP_UID="${ILLO_APP_UID:-10001}"
APP_GID="${ILLO_APP_GID:-10001}"
RUNNING_FILE="${REQUEST_FILE}.running"
START_LOCK_FILE="$(dirname "$REQUEST_FILE")/.$(basename "$REQUEST_FILE").starting"
RUNTIME_SERVICES_RUNNING_FILE="${RUNTIME_SERVICES_REQUEST_FILE}.running"
WORKSPACE_TOOLS_RUNNING_FILE="${WORKSPACE_TOOLS_REQUEST_FILE}.running"

prepare_shared_paths() {
  mkdir -p "$(dirname "$REQUEST_FILE")" "$(dirname "$STATUS_FILE")" "$(dirname "$HEARTBEAT_FILE")" "$(dirname "$LOG_PATH")" "$(dirname "$RUNTIME_SERVICES_REQUEST_FILE")" "$(dirname "$RUNTIME_SERVICES_STATUS_FILE")" "$(dirname "$RUNTIME_SERVICES_HEARTBEAT_FILE")" "$(dirname "$RUNTIME_SERVICES_LOG_PATH")" "$(dirname "$WORKSPACE_TOOLS_REQUEST_FILE")" "$(dirname "$WORKSPACE_TOOLS_STATUS_FILE")" "$(dirname "$WORKSPACE_TOOLS_HEARTBEAT_FILE")" "$(dirname "$WORKSPACE_TOOLS_LOG_PATH")"
  if [ "$(id -u)" = "0" ]; then
    chown "$APP_UID:$APP_GID" "$(dirname "$REQUEST_FILE")" "$(dirname "$STATUS_FILE")" "$(dirname "$HEARTBEAT_FILE")" "$(dirname "$RUNTIME_SERVICES_REQUEST_FILE")" "$(dirname "$RUNTIME_SERVICES_STATUS_FILE")" "$(dirname "$RUNTIME_SERVICES_HEARTBEAT_FILE")" "$(dirname "$WORKSPACE_TOOLS_REQUEST_FILE")" "$(dirname "$WORKSPACE_TOOLS_STATUS_FILE")" "$(dirname "$WORKSPACE_TOOLS_HEARTBEAT_FILE")" 2>/dev/null || true
    chmod 0775 "$(dirname "$REQUEST_FILE")" "$(dirname "$STATUS_FILE")" "$(dirname "$HEARTBEAT_FILE")" "$(dirname "$RUNTIME_SERVICES_REQUEST_FILE")" "$(dirname "$RUNTIME_SERVICES_STATUS_FILE")" "$(dirname "$RUNTIME_SERVICES_HEARTBEAT_FILE")" "$(dirname "$WORKSPACE_TOOLS_REQUEST_FILE")" "$(dirname "$WORKSPACE_TOOLS_STATUS_FILE")" "$(dirname "$WORKSPACE_TOOLS_HEARTBEAT_FILE")" 2>/dev/null || true
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

write_runtime_services_status() {
  local status="$1"
  local detail="$2"
  local requested_at="${3:-}"
  local requested_by="${4:-}"
  local exit_code="${5:-}"
  local services_json="${6:-[]}"
  json_write "$RUNTIME_SERVICES_STATUS_FILE" \
    --arg status "$status" \
    --arg detail "$detail" \
    --arg requested_at "$requested_at" \
    --arg requested_by "$requested_by" \
    --arg updated_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    --arg exit_code "$exit_code" \
    --argjson services "$services_json" \
    '{
      status: $status,
      detail: $detail,
      services: $services,
      requested_at: (if $requested_at | length > 0 then $requested_at else null end),
      started_at: (if $requested_at | length > 0 then $requested_at else null end),
      requested_by: (if $requested_by | length > 0 then $requested_by else null end),
      updated_at: $updated_at,
      exit_code: (if $exit_code | length > 0 then ($exit_code | tonumber) else null end)
    }'
}

write_workspace_tools_status() {
  local status="$1"
  local detail="$2"
  local requested_at="${3:-}"
  local requested_by="${4:-}"
  local exit_code="${5:-}"
  local bundle_id="${6:-}"
  local org_id="${7:-}"
  json_write "$WORKSPACE_TOOLS_STATUS_FILE" \
    --arg status "$status" \
    --arg detail "$detail" \
    --arg requested_at "$requested_at" \
    --arg requested_by "$requested_by" \
    --arg updated_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    --arg exit_code "$exit_code" \
    --arg bundle_id "$bundle_id" \
    --arg org_id "$org_id" \
    '{
      status: $status,
      detail: $detail,
      bundle_id: (if $bundle_id | length > 0 then $bundle_id else null end),
      org_id: (if $org_id | length > 0 then $org_id else null end),
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

write_runtime_services_heartbeat() {
  json_write "$RUNTIME_SERVICES_HEARTBEAT_FILE" \
    --arg updated_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    '{status: "ready", updated_at: $updated_at}'
}

write_workspace_tools_heartbeat() {
  json_write "$WORKSPACE_TOOLS_HEARTBEAT_FILE" \
    --arg updated_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    '{status: "ready", updated_at: $updated_at}'
}

process_request() {
  local requested_at requested_by request_detail build_no_cache worker_drain_timeout_seconds exit_code
  if ! mv "$REQUEST_FILE" "$RUNNING_FILE" 2>/dev/null; then
    return 0
  fi

  requested_at="$(jq -r '.requested_at // ""' "$RUNNING_FILE" 2>/dev/null || true)"
  requested_by="$(jq -r '.requested_by // ""' "$RUNNING_FILE" 2>/dev/null || true)"
  request_detail="$(jq -r '.detail // ""' "$RUNNING_FILE" 2>/dev/null || true)"
  build_no_cache="$(jq -r 'if .build_no_cache == true then "1" else "" end' "$RUNNING_FILE" 2>/dev/null || true)"
  worker_drain_timeout_seconds="$(jq -r '.worker_drain_timeout_seconds // ""' "$RUNNING_FILE" 2>/dev/null || true)"
  write_status "running" "Illospace update is syncing origin/main, running migrations, and recreating runtime services." "$requested_at" "$requested_by"

  mkdir -p "$(dirname "$LOG_PATH")"
  rm -f "$WORKER_DRAIN_TIMEOUT_FILE"
  {
    echo
    echo "=== Illospace Compose self-update started at $(date -u +"%Y-%m-%dT%H:%M:%SZ") ==="
    [ -z "$requested_by" ] || echo "Requested by: $requested_by"
    [ -z "$request_detail" ] || echo "Request detail: $request_detail"
    [ -z "$build_no_cache" ] || echo "Build no-cache: enabled"
    cd "$REPO"
    ILLO_UPDATE_MODE=compose \
      ILLO_COMPOSE_SKIP_UPDATER_RESTART=1 \
      ILLO_COMPOSE_BUILD_NO_CACHE="${build_no_cache:-${ILLO_COMPOSE_BUILD_NO_CACHE:-0}}" \
      ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_FILE="$WORKER_DRAIN_TIMEOUT_FILE" \
      ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_SECONDS="${worker_drain_timeout_seconds:-${ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_SECONDS:-120}}" \
      ILLO_DOCTOR_SKIP_LOCAL_HTTP_PROBES=1 \
      bash ./illo update --mode compose
  } >> "$LOG_PATH" 2>&1
  exit_code=$?

  if [ "$exit_code" -eq 0 ]; then
    if [ -f "$WORKER_DRAIN_TIMEOUT_FILE" ]; then
      write_status "idle" "Illospace update completed, but the old worker would not drain: it was force-replaced, and the AgentRuns it was holding are requeued by the stale-run reaper. Affected run ids: $(jq -r '.affected_run_ids // "unknown"' "$WORKER_DRAIN_TIMEOUT_FILE" 2>/dev/null || echo unknown)." "$requested_at" "$requested_by" "$exit_code"
    else
      write_status "idle" "Illospace update completed." "$requested_at" "$requested_by" "$exit_code"
    fi
  else
    write_status "idle" "Illospace update failed with exit code $exit_code. Check the update log." "$requested_at" "$requested_by" "$exit_code"
  fi
  rm -f "$RUNNING_FILE"
}

maybe_queue_auto_update() {
  local old_sha new_sha requested_at detail
  [ "$AUTO_UPDATE_ENABLED" = "1" ] || return 0
  [ ! -e "$REQUEST_FILE" ] || return 0
  [ ! -e "$RUNNING_FILE" ] || return 0

  mkdir -p "$(dirname "$LOG_PATH")" "$(dirname "$REQUEST_FILE")"
  if ! timeout 60 git -C "$REPO" fetch origin main >> "$LOG_PATH" 2>&1; then
    echo "Auto-update fetch failed at $(date -u +"%Y-%m-%dT%H:%M:%SZ"); retrying next interval." >> "$LOG_PATH"
    return 0
  fi

  old_sha="$(git -C "$REPO" rev-parse refs/heads/main 2>> "$LOG_PATH" || true)"
  new_sha="$(git -C "$REPO" rev-parse refs/remotes/origin/main 2>> "$LOG_PATH" || true)"
  if [ -z "$old_sha" ] || [ -z "$new_sha" ]; then
    echo "Auto-update could not resolve main refs; retrying next interval." >> "$LOG_PATH"
    return 0
  fi
  [ "$old_sha" != "$new_sha" ] || return 0
  if ! git -C "$REPO" merge-base --is-ancestor "$old_sha" "$new_sha" >> "$LOG_PATH" 2>&1; then
    echo "Auto-update skipped because origin/main is not ahead of local main ($old_sha -> $new_sha)." >> "$LOG_PATH"
    return 0
  fi

  [ ! -e "$REQUEST_FILE" ] || return 0
  [ ! -e "$RUNNING_FILE" ] || return 0
  if ! (set -o noclobber; : > "$START_LOCK_FILE") 2>/dev/null; then
    return 0
  fi
  if [ -e "$REQUEST_FILE" ] || [ -e "$RUNNING_FILE" ]; then
    rm -f "$START_LOCK_FILE"
    return 0
  fi

  requested_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  detail="origin/main advanced from $old_sha to $new_sha."
  if ! json_write "$REQUEST_FILE" \
    --arg requested_at "$requested_at" \
    --arg requested_by "auto-update" \
    --arg detail "$detail" \
    '{requested_at: $requested_at, requested_by: $requested_by, detail: $detail}'; then
    rm -f "$START_LOCK_FILE"
    return 1
  fi
  if ! write_status "queued" "$detail" "$requested_at" "auto-update"; then
    rm -f "$START_LOCK_FILE"
    return 1
  fi
  rm -f "$START_LOCK_FILE"
}

initialize_auto_update_poll() {
  local now_epoch="$1"
  case "$AUTO_UPDATE_POLL_SECONDS" in
    ''|*[!0-9]*|0) AUTO_UPDATE_POLL_SECONDS=300 ;;
  esac
  AUTO_UPDATE_NEXT_POLL_AT=$(( now_epoch + AUTO_UPDATE_POLL_SECONDS ))
}

poll_auto_update_if_due() {
  local now_epoch="$1"
  local exit_code=0
  [ "$now_epoch" -ge "$AUTO_UPDATE_NEXT_POLL_AT" ] || return 0
  maybe_queue_auto_update || exit_code=$?
  if [ "$exit_code" -ne 0 ]; then
    echo "Auto-update poll failed safely with exit code $exit_code; retrying next interval." >> "$LOG_PATH"
  fi
  AUTO_UPDATE_NEXT_POLL_AT=$(( now_epoch + AUTO_UPDATE_POLL_SECONDS ))
}

process_runtime_services_request() {
  local requested_at requested_by services_json exit_code
  local services=()
  if ! mv "$RUNTIME_SERVICES_REQUEST_FILE" "$RUNTIME_SERVICES_RUNNING_FILE" 2>/dev/null; then
    return 0
  fi

  requested_at="$(jq -r '.requested_at // ""' "$RUNTIME_SERVICES_RUNNING_FILE" 2>/dev/null || true)"
  requested_by="$(jq -r '.requested_by // ""' "$RUNTIME_SERVICES_RUNNING_FILE" 2>/dev/null || true)"
  services_json="$(jq -c '.services // []' "$RUNTIME_SERVICES_RUNNING_FILE" 2>/dev/null || echo '[]')"
  mapfile -t services < <(jq -r '.services[]?' "$RUNTIME_SERVICES_RUNNING_FILE" 2>/dev/null || true)
  write_runtime_services_status "running" "Runtime service restart is running." "$requested_at" "$requested_by" "" "$services_json"

  mkdir -p "$(dirname "$RUNTIME_SERVICES_LOG_PATH")"
  {
    echo
    echo "=== Illospace runtime service restart started at $(date -u +"%Y-%m-%dT%H:%M:%SZ") ==="
    [ -z "$requested_by" ] || echo "Requested by: $requested_by"
    [ "${#services[@]}" -eq 0 ] || printf 'Services: %s\n' "${services[*]}"
    cd "$REPO"
    bash "$REPO/deploy/scripts/runtime-services.sh" restart "${services[@]}"
  } >> "$RUNTIME_SERVICES_LOG_PATH" 2>&1
  exit_code=$?

  if [ "$exit_code" -eq 0 ]; then
    write_runtime_services_status "idle" "Runtime service restart completed." "$requested_at" "$requested_by" "$exit_code" "$services_json"
  else
    write_runtime_services_status "idle" "Runtime service restart failed with exit code $exit_code. Check the runtime service log." "$requested_at" "$requested_by" "$exit_code" "$services_json"
  fi
  rm -f "$RUNTIME_SERVICES_RUNNING_FILE"
}

process_workspace_tools_request() {
  local action requested_at requested_by bundle_id org_id version exit_code detail
  if ! mv "$WORKSPACE_TOOLS_REQUEST_FILE" "$WORKSPACE_TOOLS_RUNNING_FILE" 2>/dev/null; then
    return 0
  fi

  action="$(jq -r '.action // "install"' "$WORKSPACE_TOOLS_RUNNING_FILE" 2>/dev/null || echo install)"
  requested_at="$(jq -r '.requested_at // ""' "$WORKSPACE_TOOLS_RUNNING_FILE" 2>/dev/null || true)"
  requested_by="$(jq -r '.requested_by // ""' "$WORKSPACE_TOOLS_RUNNING_FILE" 2>/dev/null || true)"
  bundle_id="$(jq -r '.bundle_id // ""' "$WORKSPACE_TOOLS_RUNNING_FILE" 2>/dev/null || true)"
  org_id="$(jq -r '.org_id // ""' "$WORKSPACE_TOOLS_RUNNING_FILE" 2>/dev/null || true)"
  version="$(jq -r '.version // ""' "$WORKSPACE_TOOLS_RUNNING_FILE" 2>/dev/null || true)"
  write_workspace_tools_status "running" "Workspace tool operation is running." "$requested_at" "$requested_by" "" "$bundle_id" "$org_id"

  mkdir -p "$(dirname "$WORKSPACE_TOOLS_LOG_PATH")"
  {
    echo
    echo "=== Illospace workspace tool operation started at $(date -u +"%Y-%m-%dT%H:%M:%SZ") ==="
    [ -z "$requested_by" ] || echo "Requested by: $requested_by"
    echo "Action: $action"
    echo "Bundle: $bundle_id"
    echo "Org: $org_id"
    cd "$REPO"
    case "$action" in
      install)
        bash "$REPO/deploy/scripts/workspace-tools.sh" install "$bundle_id" "$org_id" "$version"
        ;;
      check)
        bash "$REPO/deploy/scripts/workspace-tools.sh" check "$bundle_id" "$org_id" "$version"
        ;;
      *)
        echo "Unsupported workspace tool action: $action" >&2
        exit 2
        ;;
    esac
  } >> "$WORKSPACE_TOOLS_LOG_PATH" 2>&1
  exit_code=$?

  if [ "$exit_code" -eq 0 ]; then
    detail="Workspace tool operation completed."
  else
    detail="Workspace tool operation failed with exit code $exit_code. Check the workspace tool log."
  fi
  write_workspace_tools_status "idle" "$detail" "$requested_at" "$requested_by" "$exit_code" "$bundle_id" "$org_id"
  rm -f "$WORKSPACE_TOOLS_RUNNING_FILE"
}

main() {
  local exit_code now_epoch

  prepare_shared_paths
  write_status "idle" "Compose updater sidecar is ready." "" ""
  write_runtime_services_status "idle" "Runtime service host controller is ready." "" "" "" "[]"
  write_workspace_tools_status "idle" "Workspace tool host controller is ready." "" "" "" "" ""
  initialize_auto_update_poll "$(date +%s)"

  while true; do
    write_heartbeat
    write_runtime_services_heartbeat
    write_workspace_tools_heartbeat
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
    if [ -f "$RUNTIME_SERVICES_REQUEST_FILE" ]; then
      set +e
      process_runtime_services_request
      exit_code=$?
      set -e
      if [ "$exit_code" -ne 0 ]; then
        write_runtime_services_status "idle" "Runtime service restart failed with exit code $exit_code. Check the runtime service log." "" "" "$exit_code" "[]"
        rm -f "$RUNTIME_SERVICES_RUNNING_FILE"
      fi
    fi
    if [ -f "$WORKSPACE_TOOLS_REQUEST_FILE" ]; then
      set +e
      process_workspace_tools_request
      exit_code=$?
      set -e
      if [ "$exit_code" -ne 0 ]; then
        write_workspace_tools_status "idle" "Workspace tool operation failed with exit code $exit_code. Check the workspace tool log." "" "" "$exit_code" "" ""
        rm -f "$WORKSPACE_TOOLS_RUNNING_FILE"
      fi
    fi

    now_epoch="$(date +%s)"
    poll_auto_update_if_due "$now_epoch"
    sleep "$POLL_SECONDS"
  done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
