#!/usr/bin/env bash

# Thin Compose caller for the Python-owned AgentRun queue-starvation predicate.
# The CLI stays in api so the check still works when worker is dead or drained.
# Because worker concurrency exists only in worker's container environment, the
# caller passes the value explicitly from the sourced deploy env that Compose
# uses to configure worker.

assert_agent_run_queue_not_starved() {
  local stale_after_seconds="${ILLO_AGENT_RUN_QUEUED_DOCTOR_SECONDS:-600}"
  local runner_concurrency="${ILLO_AGENT_RUNNER_CONCURRENCY:-4}"
  compose exec -T api \
    python -m brain.app.cli.agent_run_queue_health \
    --stale-after-seconds "$stale_after_seconds" \
    --runner-concurrency "$runner_concurrency"
}
