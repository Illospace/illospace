#!/usr/bin/env bash

# Shared host-side Python interpreter for deploy scripts. The worker-swap name
# remains an accepted input while callers migrate to the deploy-wide contract.
DEPLOY_PYTHON_BIN="${DEPLOY_PYTHON_BIN:-${WORKER_SWAP_PYTHON_BIN:-python3}}"

deploy_python_bin() {
  printf '%s\n' "$DEPLOY_PYTHON_BIN"
}
