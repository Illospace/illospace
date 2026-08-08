#!/usr/bin/env python3
"""Standalone AgentRun worker."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import signal
import threading
import time
from contextlib import suppress
from dataclasses import asdict
from typing import NoReturn

from brain.contracts.worker_lifecycle import (
    WorkerLifecyclePhase,
    publish_worker_lifecycle_phase,
)
from brain.systems.cycles import (
    seconds_since_last_cycle_tick,
    start_cycle_scheduler,
    stop_cycle_scheduler,
)
from brain.systems.runs.cortex.queue_health import (
    QueueStallMonitor,
    queued_backlog_health_snapshot_async,
)
from brain.systems.runs.cortex.runner import (
    DrainResult,
    request_runner_stop,
    runner_health_snapshot,
    start_runner,
    stop_runner,
)
from brain.systems.runs.cortex.worker_liveness import (
    record_worker_liveness_checkpoint_async,
)
from brain.systems.runs.interruption import interrupt_and_requeue_run_ids

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cortex.worker")

_SELF_RESTART_DRAIN_TIMEOUT_DEFAULT_SECONDS = 60.0
_SELF_RESTART_DRAIN_TIMEOUT_MAX_SECONDS = 3600.0
_SELF_RESTART_SHUTDOWN_GRACE_SECONDS = 60.0

_running = True
_draining = False
_draining_for_deploy = False
_shutdown_watchdog: threading.Timer | None = None
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
    global _draining_for_deploy
    # A signal is the only exit where a peer is already covering the queue: the
    # deploy starts and verifies a claiming handoff worker before it signals.
    # Every other exit leaves nobody claiming, so only this path may wait for
    # in-flight runs without a floor.
    _draining_for_deploy = True
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
    raw = (
        os.getenv("ILLO_AGENT_RUNNER_DRAIN_TIMEOUT_SECONDS", "infinity").strip().lower()
    )
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
    """Bound the drain for every exit the deploy did not ask for.

    The run that wedged the worker is the very thing the drain would wait on,
    and no handoff worker is covering the queue, so waiting without a floor
    turns the restart that was meant to clear the queue into a permanent
    outage. The ceiling matters as much as rejecting "infinity": a large finite
    value both defeats the drain and overflows the watchdog timer.
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
    return min(max(0.0, seconds), _SELF_RESTART_DRAIN_TIMEOUT_MAX_SECONDS)


def _arm_shutdown_watchdog() -> None:
    """Guarantee process exit even if the shutdown sequence itself wedges.

    Only the exit restores claiming capacity, and every step on the way to it —
    logging, draining, requeuing, stopping the cycle scheduler, joining the
    supervisor — can block on the same wedged run, database or log pipe the
    worker is escaping. Arming is idempotent so the earliest caller wins, and
    the timer is never cancelled: it is a daemon thread that dies with the
    process it exists to end.
    """
    global _shutdown_watchdog
    if _shutdown_watchdog is not None:
        return
    timeout_seconds = (
        _self_restart_drain_timeout_seconds() + _SELF_RESTART_SHUTDOWN_GRACE_SECONDS
    )

    def _force_exit() -> None:
        # Deliberately not ``logger``: a blocked log pipe is one of the hangs
        # this timer exists to survive, and taking the logging lock here would
        # make the last-resort killer share the victim's fate.
        with suppress(Exception):
            os.write(
                2,
                f"agent-run worker shutdown exceeded {timeout_seconds}s; terminating\n".encode(),
            )
        _terminate_process(1)

    _shutdown_watchdog = threading.Timer(timeout_seconds, _force_exit)
    _shutdown_watchdog.daemon = True
    _shutdown_watchdog.start()


def _exit_for_restart(message: str, **extra: object) -> NoReturn:
    """Record why the worker is unhealthy and exit so the container restarts it.

    The caller has already established that this process can no longer do its
    job, so reaching ``_terminate_process`` is the point of the exit rather than
    a detail of it. The watchdog is armed before the log line, because logging
    is itself something that can block.
    """
    _arm_shutdown_watchdog()
    logger.error(message, extra=extra)
    raise SystemExit(1)


def _runner_health_grace_seconds() -> float:
    try:
        return max(
            1.0, float(os.getenv("ILLO_AGENT_RUNNER_HEALTH_GRACE_SECONDS", "15"))
        )
    except Exception:
        return 15.0


def _queue_health_check_interval_seconds() -> float:
    try:
        return max(
            1.0, float(os.getenv("ILLO_AGENT_RUN_QUEUE_HEALTH_CHECK_SECONDS", "5"))
        )
    except Exception:
        return 5.0


def _queue_stall_grace_seconds() -> float:
    try:
        return max(
            5.0, float(os.getenv("ILLO_AGENT_RUN_QUEUE_STALL_GRACE_SECONDS", "45"))
        )
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
    raise RuntimeError(f"Embedding backend not ready before worker start: {health}")


def _cycle_scheduler_enabled() -> bool:
    disabled = os.getenv("ILLO_WORKER_DISABLE_CYCLE_SCHEDULER", "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return False
    enabled = os.getenv("ILLO_WORKER_ENABLE_CYCLE_SCHEDULER", "").strip().lower()
    return enabled in {"1", "true", "yes", "on"}


async def _queue_health_and_worker_heartbeat():
    queue_health = await queued_backlog_health_snapshot_async()
    await record_worker_liveness_checkpoint_async()
    return queue_health


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
    global _draining, _draining_for_deploy, _shutdown_watchdog
    exit_code = 0
    cycle_scheduler_enabled = False
    if _running:
        # ``main`` runs once in production. Resetting here also keeps direct
        # invocation in tests independent without erasing a pre-start signal.
        _draining = False
        _draining_for_deploy = False
        _shutdown_watchdog = None
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
                    queue_health = asyncio.run(_queue_health_and_worker_heartbeat())
                except Exception as exc:
                    logger.warning(
                        "agent-run worker queue health check failed: %s", exc
                    )
                else:
                    stale_for_seconds = queue_stall_monitor.observe(
                        queue_health, now=now
                    )
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
        try:
            if _draining_for_deploy:
                drain_timeout_seconds = _shutdown_drain_timeout_seconds()
            else:
                # Health exits and crashes alike: nobody else is claiming, so
                # the process must reach its exit whatever the runs are doing.
                _arm_shutdown_watchdog()
                drain_timeout_seconds = _self_restart_drain_timeout_seconds()
            _begin_draining()
            drain_result = stop_runner(drain_timeout_seconds=drain_timeout_seconds)
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
            try:
                _terminate_process(exit_code)
            finally:
                # Production uses ``os._exit`` and never reaches this cleanup.
                # Tests and embedded callers replace the terminator with a
                # returning function; do not leave a daemon timer behind that
                # can terminate the surrounding process later.
                if _shutdown_watchdog is not None:
                    _shutdown_watchdog.cancel()
                    _shutdown_watchdog = None


if __name__ == "__main__":
    main()
