#!/usr/bin/env python3
"""Standalone AgentRun worker."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time

from brain.contracts.worker_lifecycle import (
    WorkerLifecyclePhase,
    publish_worker_lifecycle_phase,
)
from brain.systems.runs.cortex.queue_health import QueueStallMonitor, queued_backlog_health_snapshot_async
from brain.systems.runs.cortex.runner import (
    DrainResult,
    request_runner_stop,
    runner_health_snapshot,
    start_runner,
    stop_runner,
)
from brain.systems.runs.interruption import interrupt_and_requeue_run_ids
from brain.systems.cycles import (
    seconds_since_last_cycle_tick,
    start_cycle_scheduler,
    stop_cycle_scheduler,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cortex.worker")

_running = True
_draining = False
_terminate_process = os._exit


def _publish_worker_lifecycle_phase(phase: WorkerLifecyclePhase) -> None:
    try:
        publish_worker_lifecycle_phase(phase)
    except Exception:
        # Losing the signal must not itself take claiming capacity down. Deploy
        # treats an unreadable phase as unknown and refuses destructive steps.
        logger.exception("could not publish worker lifecycle phase %s", phase.value)


def _begin_draining() -> None:
    """Idempotently stop claim capacity before any runner drain begins."""

    global _draining, _running
    _running = False
    if _draining:
        return
    _draining = True
    _publish_worker_lifecycle_phase(WorkerLifecyclePhase.DRAINING)
    request_runner_stop()


def _signal_handler(signum, _frame):
    logger.info("received %s, draining agent-run worker", signal.Signals(signum).name)
    _begin_draining()


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


def _queue_health_check_interval_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("ILLO_AGENT_RUN_QUEUE_HEALTH_CHECK_SECONDS", "5")))
    except Exception:
        return 5.0


def _queue_stall_grace_seconds() -> float:
    try:
        return max(5.0, float(os.getenv("ILLO_AGENT_RUN_QUEUE_STALL_GRACE_SECONDS", "45")))
    except Exception:
        return 45.0


def _cycle_scheduler_stall_grace_seconds() -> float:
    try:
        return max(
            1.0,
            float(os.getenv("ILLO_CYCLE_SCHEDULER_STALL_GRACE_SECONDS", "300")),
        )
    except Exception:
        return 300.0


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


def _recover_timed_out_runs(drain_result: DrainResult) -> None:
    run_ids = drain_result.timed_out_run_ids
    if not run_ids:
        return
    try:
        interruptions = asyncio.run(
            interrupt_and_requeue_run_ids(
                run_ids,
                reason="worker_shutdown_drain_timeout",
            )
        )
    except Exception:
        logger.exception(
            "runner shutdown could not requeue affected run ids: %s",
            list(run_ids),
        )
        return
    requeued_run_ids = [interruption.run_id for interruption in interruptions]
    if requeued_run_ids:
        logger.warning(
            "runner shutdown interrupted and requeued run ids: %s",
            requeued_run_ids,
        )


def main() -> None:
    global _draining
    exit_code = 0
    cycle_scheduler_enabled = False
    if _running:
        # ``main`` runs once in production. Resetting here also keeps direct
        # invocation in tests independent without erasing a pre-start signal.
        _draining = False
    try:
        _publish_worker_lifecycle_phase(WorkerLifecyclePhase.STARTING)
        logger.info("starting agent-run worker")
        _require_embedding_backend_ready()
        if not _running:
            return
        cycle_scheduler_enabled = _cycle_scheduler_enabled()
        if cycle_scheduler_enabled:
            start_cycle_scheduler()
        else:
            logger.info("cycle scheduler disabled for this worker")
        if not _running:
            return
        start_runner()
        if not _running:
            return
        _publish_worker_lifecycle_phase(WorkerLifecyclePhase.CLAIMING)
        last_healthy = time.monotonic()
        health_grace_seconds = _runner_health_grace_seconds()
        queue_stall_monitor = QueueStallMonitor(
            check_interval_seconds=_queue_health_check_interval_seconds(),
            stall_grace_seconds=_queue_stall_grace_seconds(),
        )
        cycle_scheduler_stall_grace_seconds = _cycle_scheduler_stall_grace_seconds()
        while _running:
            now = time.monotonic()
            health = runner_health_snapshot()
            if health.get("runner_running"):
                last_healthy = now
            elif now - last_healthy > health_grace_seconds:
                logger.error(
                    "agent-run worker runner supervisor unhealthy; exiting for restart",
                    extra={"runner_health": health},
                )
                raise SystemExit(1)

            if queue_stall_monitor.should_check(now=now):
                try:
                    queue_health = asyncio.run(queued_backlog_health_snapshot_async())
                except Exception as exc:
                    logger.warning("agent-run worker queue health check failed: %s", exc)
                else:
                    stale_for_seconds = queue_stall_monitor.observe(queue_health, now=now)
                    if stale_for_seconds is not None:
                        logger.error(
                            "agent-run worker queue stalled; exiting for restart",
                            extra={
                                "queue_health": queue_health,
                                "stale_for_seconds": stale_for_seconds,
                            },
                        )
                        raise SystemExit(1)

            if cycle_scheduler_enabled:
                seconds_since_last_tick = seconds_since_last_cycle_tick()
                if (
                    seconds_since_last_tick is not None
                    and seconds_since_last_tick > cycle_scheduler_stall_grace_seconds
                ):
                    logger.error(
                        "cycle scheduler wedged; exiting for restart",
                        extra={"seconds_since_last_tick": seconds_since_last_tick},
                    )
                    raise SystemExit(1)
            time.sleep(_poll_interval())
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1
        raise
    except BaseException:
        # _terminate_process preempts the interpreter's top-level traceback print.
        logger.exception("agent-run worker crashed; exiting")
        exit_code = 1
        raise
    finally:
        try:
            _begin_draining()
            drain_result = stop_runner(
                drain_timeout_seconds=_shutdown_drain_timeout_seconds()
            )
            _recover_timed_out_runs(drain_result)
            if cycle_scheduler_enabled:
                stop_cycle_scheduler()
        except BaseException:
            logger.exception("agent-run worker shutdown failed")
            exit_code = 1
        finally:
            _publish_worker_lifecycle_phase(WorkerLifecyclePhase.STOPPED)
            logger.info("agent-run worker stopped")
            logging.shutdown()
            _terminate_process(exit_code)


if __name__ == "__main__":
    main()
