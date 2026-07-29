#!/usr/bin/env bash

# Shared adapter for the import-safe worker lifecycle contract.

worker_lifecycle_python_bin() {
  printf '%s\n' "${WORKER_LIFECYCLE_PYTHON_BIN:-python3}"
}

worker_lifecycle_contract() {
  local python_bin
  python_bin="$(worker_lifecycle_python_bin)"
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$python_bin" -m brain.contracts.worker_lifecycle "$@"
}

worker_lifecycle_cover_observe() {
  worker_lifecycle_contract observe "$1" "$2"
}
