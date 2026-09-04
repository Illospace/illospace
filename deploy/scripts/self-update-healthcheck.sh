#!/usr/bin/env bash
set -euo pipefail

HEARTBEAT_FILE="${ILLO_SELF_UPDATE_HEARTBEAT_FILE:-/data/private/self-update/heartbeat.json}"
MAX_AGE_SECONDS="${ILLO_SELF_UPDATE_HEARTBEAT_MAX_AGE_SECONDS:-60}"

[ -f "$HEARTBEAT_FILE" ] || exit 1

updated_at="$(jq -r '.updated_at // empty' "$HEARTBEAT_FILE" 2>/dev/null)"
[ -n "$updated_at" ] || exit 1

heartbeat_epoch="$(date -u -d "$updated_at" +%s 2>/dev/null ||
  date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$updated_at" +%s 2>/dev/null)" || exit 1
now_epoch="$(date -u +%s)"

[ $((now_epoch - heartbeat_epoch)) -le "$MAX_AGE_SECONDS" ]
