#!/usr/bin/env bash
set -euo pipefail

QUIET=0
if [[ "${1:-}" == "--quiet" ]]; then
  QUIET=1
fi

ROOT=$(cd "$(dirname "$0")/.." && pwd)
FRONTEND_DIR="$ROOT/frontend"

if [[ ! -d "$FRONTEND_DIR" ]]; then
  exit 0
fi

if [[ $QUIET -eq 0 ]]; then
  if [[ -d "$FRONTEND_DIR/node_modules" ]]; then
    echo "=== Syncing frontend dependencies ==="
  else
    echo "=== Installing frontend dependencies ==="
  fi
fi

cd "$FRONTEND_DIR"
npm install --silent --no-fund --no-audit
