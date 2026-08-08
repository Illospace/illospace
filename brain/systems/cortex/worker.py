#!/usr/bin/env python3
"""Standalone AgentRun worker."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import logging
import math
import os
import signal
import threading
import time
from typing import NoReturn

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

_SELF_RESTART_DRAIN_TIMEOUT_DEFAULT_SECONDS = 60.0
_SELF_RESTART_SHUTDOWN_GRACE_SECONDS = 30.0

_running = True
_draining = False
_restarting_for_health = False
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


def _self_restart_drain_timeout_seconds() -> float:
    """Bound the drain when the worker exits to restore its own claiming capacity.

    A deploy drains indefinitely on purpose: the handoff worker is already
    claiming, so in-flight runs may finish at leisure. A health self-exit has no
    handoff — nothing else claims — and the run that wedged the worker is the
    very thing the drain would wait on. Waiting without a floor turns the
    restart that was meant to clear the queue into a permanent outage, so this
    timeout never resolves to "forever".
    """
    raw = os.getenv("ILLO_AGENT_RUNNER_SELF_RESTART_DRAIN_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _SELF_RESTART_DRAIN_TIMEOUT_DEFAULT_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    # ``float`` accepts "infinity", the value the deploy path uses, so an
    # operator copying that setting across would restore the deadlock.
    if not math.isfinite(seconds):
        logger.warning(
            "invalid ILLO_AGENT_RUNNER_SELF_RESTART_DRAIN_TIMEOUT_SECONDS=%r; draining for %ss",
            raw,
            _SELF_RESTART_DRAIN_TIMEOUT_DEFAULT_SECONDS,
        )
        return _SELF_RESTART_DRAIN_TIMEOUT_DEFAULT_SECONDS
    return max(0.0, seconds)


def _exit_for_restart(message: str, **extra: object) -> NoReturn:
    """Record why the worker is unhealthy and exit so the container restarts it.

    Every caller has already established that this process can no longer do its
    job, so reaching ``_terminate_process`` is the point of the exit rather than
    a detail of it. ``_restarting_for_health`` tells the shutdown path that no
    other worker is covering the queue.
    """
    global _restarting_for_health
    _restarting_for_health = True
    logger.error(message, extra=extra)
    raise SystemExit(1)


def _arm_self_restart_watchdog(timeout_seconds: float) -> threading.Timer:
    """Guarantee process exit even if the shutdown sequence itself wedges.

    Only the exit restores claiming capacity, and every step between here and it
    — draining, requeuing, stopping the cycle scheduler, joining the supervisor
    — can block on the same wedged run or database the worker is escaping.
    """

    def _force_exit() -> None:
        logger.error(
            "agent-run worker shutdown exceeded %ss after a health exit; terminating",
            timeout_seconds,
        )
        _terminate_process(1)

    watchdog = threading.Timer(timeout_seconds, _force_exit)
    watchdog.daemon = True
    watchdog.start()
    return watchdog


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
    global _draining, _restarting_for_health
    exit_code = 0
    cycle_scheduler_enabled = False
    if _running:
        # ``main`` runs once in production. Resetting here also keeps direct
        # invocation in tests independent without erasing a pre-start signal.
        _draining = False
        _restarting_for_health = False
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
                _exit_for_restart(
                    "agent-run worker runner supervisor unhealthy; exiting for restart",
                    runner_health=health,
                )

            if queue_stall_monitor.should_check(now=now):
                try:
                    queue_health = asyncio.run(queued_backlog_health_snapshot_async())
                except Exception as exc:
                    logger.warning("agent-run worker queue health check failed: %s", exc)
                else:
                    stale_for_seconds = queue_stall_monitor.observe(queue_health, now=now)
                    if stale_for_seconds is not None:
                        _exit_for_restart(
                            "agent-run worker queue stalled; exiting for restart",
                            queue_health={
                                **asdict(queue_health),
                                "oldest_queued_at": (
                                    queue_health.oldest_queued_at.isoformat()
                                    if queue_health.oldest_queued_at
                                    else None
                                ),
                            },
                            stale_for_seconds=stale_for_seconds,
                        )

            if cycle_scheduler_enabled:
                seconds_since_last_tick = seconds_since_last_cycle_tick()
                if (
                    seconds_since_last_tick is not None
                    and seconds_since_last_tick > cycle_scheduler_stall_grace_seconds
                ):
                    _exit_for_restart(
                        "cycle scheduler wedged; exiting for restart",
                        seconds_since_last_tick=seconds_since_last_tick,
                    )
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
        watchdog = None
        try:
            if _restarting_for_health:
                drain_timeout_seconds = _self_restart_drain_timeout_seconds()
                watchdog = _arm_self_restart_watchdog(
                    drain_timeout_seconds + _SELF_RESTART_SHUTDOWN_GRACE_SECONDS
                )
            else:
                drain_timeout_seconds = _shutdown_drain_timeout_seconds()
            _begin_draining()
            drain_result = stop_runner(drain_timeout_seconds=drain_timeout_seconds)
            _recover_timed_out_runs(drain_result)
            if cycle_scheduler_enabled:
                stop_cycle_scheduler()
        except BaseException:
            logger.exception("agent-run worker shutdown failed")
            exit_code = 1
        finally:
            if watchdog is not None:
                watchdog.cancel()
            _publish_worker_lifecycle_phase(WorkerLifecyclePhase.STOPPED)
            logger.info("agent-run worker stopped")
            logging.shutdown()
            _terminate_process(exit_code)


if __name__ == "__main__":
    main()
