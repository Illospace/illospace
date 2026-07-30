#!/usr/bin/env bash
set -euo pipefail

# Fails loudly when the Compose stack is up but a required service is absent or
# its worker has drained -- states where every HTTP probe passes while Illo
# cannot do any work (#527, #549). Cheap enough for a cron entry or systemd
# timer, with distinct exit codes for an external watcher (#512).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$ROOT/deploy/compose/docker-compose.yml"
ENV_FILE="${ILLO_COMPOSE_ENV_FILE:-$ROOT/deploy/compose/.env}"

source "$SCRIPT_DIR/compose-runtime-lib.sh"
source "$SCRIPT_DIR/agent-run-queue-health-lib.sh"

usage() {
  cat <<EOF
Usage: ./illo deploy inert-check
       deploy/scripts/inert-stack-check.sh

Asserts that every always-on Compose service is running whenever the stack is up
at all, that the worker has not entered a drained lifecycle phase, then checks
that old queued AgentRuns still have recent claim activity. The worker owns
AgentRun execution and the cycle-scheduler thread, so its absence, drained phase,
or a wedged runner can leave the stack answering HTTP health checks normally
while Illo does nothing.

Required services: $STACK_REQUIRED_SERVICES
  (override with STACK_REQUIRED_SERVICES="postgres api worker")

Exit codes:
  0  every required service is running
  1  the check could not inspect Compose or the AgentRun queue is starved
  $STACK_INERT_EXIT_CODE  INERT   - stack is up but a required service is absent
  $STACK_DOWN_EXIT_CODE  DOWN    - no Compose services are running at all
  $STACK_DRAINED_EXIT_CODE  DRAINED - worker is present but cannot claim new runs
EOF
}

case "${1:-}" in
  -h|--help|help)
    usage
    exit 0
    ;;
  "")
    ;;
  *)
    echo "Unknown argument: $1" >&2
    usage >&2
    exit 2
    ;;
esac

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE; run ./illo deploy init first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

status=0
assert_stack_not_inert || status=$?
if [ "$status" -eq 0 ]; then
  assert_worker_not_drained || status=$?
fi
if [ "$status" -eq 0 ]; then
  assert_agent_run_queue_not_starved || status=1
fi
if [ "$status" -eq 0 ]; then
  echo "OK:    every required Compose service is running, the worker is not drained, and the AgentRun queue is moving ($STACK_REQUIRED_SERVICES)"
fi
exit "$status"
