#!/usr/bin/env bash

# Shared worker-swap snapshot parsing, presentation, and decision helpers.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy-python-lib.sh"

# Backward-compatible name for callers outside this repository.
worker_swap_python_bin() {
  deploy_python_bin
}

worker_swap_contract() {
  local python_bin
  python_bin="$(deploy_python_bin)"
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$python_bin" -m brain.contracts.worker_swap "$@"
}

worker_swap_snapshot_acquire() {
  local python_bin
  python_bin="$(deploy_python_bin)"
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$python_bin" -m brain.app.cli.worker_swap_snapshot
}

worker_swap_snapshot() {
  local snapshot
  if snapshot="$(worker_swap_snapshot_acquire 2>/dev/null)" \
    && printf '%s\n' "$snapshot" | worker_swap_contract validate >/dev/null 2>&1; then
    printf '%s\n' "$snapshot"
    return 0
  fi
  worker_swap_contract unknown
}

worker_swap_snapshot_field() {
  local snapshot="$1"
  local field="$2"
  printf '%s\n' "$snapshot" | worker_swap_contract field "$field"
}

worker_swap_snapshot_decision() {
  worker_swap_snapshot_field "$1" decision
}

worker_swap_snapshot_count() {
  worker_swap_snapshot_field "$1" count
}

worker_swap_snapshot_run_ids() {
  worker_swap_snapshot_field "$1" ids
}

worker_swap_snapshot_details() {
  worker_swap_snapshot_field "$1" details
}

worker_swap_snapshot_report() {
  worker_swap_snapshot_field "$1" report
}
