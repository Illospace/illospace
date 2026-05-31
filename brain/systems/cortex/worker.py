#!/usr/bin/env python3
"""Standalone AgentRun worker."""

from __future__ import annotations

import logging
import os
import signal
import time

from brain.systems.runs.cortex.runner import runner_health_snapshot, start_runner, stop_runner
from brain.systems.cycles import start_cycle_scheduler, stop_cycle_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cortex.worker")

_running = True


def _signal_handler(signum, _frame):
    global _running
    logger.info("received %s, draining agent-run worker", signal.Signals(signum).name)
    _running = False


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def _poll_interval() -> float:
    try:
        return max(0.1, float(os.getenv("AGENT_RUN_WORKER_POLL_SECONDS", "0.5")))
    except Exception:
        return 0.5


def _shutdown_drain_timeout_seconds() -> float | None:
    raw = os.getenv("ILLO_AGENT_RUNNER_DRAIN_TIMEOUT_SECONDS", "infinity").strip().lower()
    if raw in {"", "none", "infinite", "infinity", "forever"}:
        return None
    try:
        return max(0.0, float(raw))
    except Exception:
        logger.warning(
            "invalid ILLO_AGENT_RUNNER_DRAIN_TIMEOUT_SECONDS=%r; draining indefinitely",
            raw,
        )
        return None


def _runner_health_grace_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("ILLO_AGENT_RUNNER_HEALTH_GRACE_SECONDS", "15")))
    except Exception:
        return 15.0


def _require_embedding_backend_ready() -> None:
    """Block worker startup until the configured GPU embedding worker is ready."""
    import brain.kernel.config as cfg

    if cfg.EMBEDDING_BACKEND != "gpu":
        return

    from brain.systems.memory import embeddings

    if embeddings.wait_for_embedding_backend_ready():
        return
    health = embeddings.server_health()
    raise RuntimeError(
        "Embedding backend not ready before worker start: "
        f"{health}"
    )


def _cycle_scheduler_enabled() -> bool:
    disabled = os.getenv("ILLO_WORKER_DISABLE_CYCLE_SCHEDULER", "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return False
    enabled = os.getenv("ILLO_WORKER_ENABLE_CYCLE_SCHEDULER", "").strip().lower()
    return enabled in {"1", "true", "yes", "on"}


def main() -> None:
    logger.info("starting agent-run worker")
    _require_embedding_backend_ready()
    cycle_scheduler_enabled = _cycle_scheduler_enabled()
    if cycle_scheduler_enabled:
        start_cycle_scheduler()
    else:
        logger.info("cycle scheduler disabled for this worker")
    start_runner()
    last_healthy = time.monotonic()
    health_grace_seconds = _runner_health_grace_seconds()
    try:
        while _running:
            health = runner_health_snapshot()
            if health.get("runner_running"):
                last_healthy = time.monotonic()
            elif time.monotonic() - last_healthy > health_grace_seconds:
                logger.error(
                    "agent-run worker runner supervisor unhealthy; exiting for restart",
                    extra={"runner_health": health},
                )
                raise SystemExit(1)
            time.sleep(_poll_interval())
    finally:
        stop_runner(drain_timeout_seconds=_shutdown_drain_timeout_seconds())
        if cycle_scheduler_enabled:
            stop_cycle_scheduler()
        logger.info("agent-run worker stopped")


if __name__ == "__main__":
    main()
