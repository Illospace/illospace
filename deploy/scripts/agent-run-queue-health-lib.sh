#!/usr/bin/env bash

# Thin Compose caller for the Python-owned AgentRun queue-starvation predicate.
# The caller supplies the doctor's slower operational threshold; all queue age,
# claim recency, and saturation decisions remain in queue_health.py.

assert_agent_run_queue_not_starved() {
  local stale_after_seconds="${ILLO_AGENT_RUN_QUEUED_DOCTOR_SECONDS:-600}"
  compose exec -T api \
    python -m brain.systems.runs.cortex.queue_health \
    --stale-after-seconds "$stale_after_seconds"
}
