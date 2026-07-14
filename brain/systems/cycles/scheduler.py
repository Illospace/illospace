"""Background cycle scheduler."""
from __future__ import annotations

import logging
import os
import asyncio
import threading
import time

from brain.systems.cycles.service import async_schedule_due_cycles_once

logger = logging.getLogger("cycles.scheduler")

_scheduler_running = False
_scheduler_thread: threading.Thread | None = None
_scheduler_task: asyncio.Task | None = None
_poll_interval_sec = int(os.environ.get("CYCLE_SCHEDULER_POLL_SEC", "30"))
_last_tick_ok: float | None = None
_scheduler_state_lock = threading.Lock()


def _tick_timeout_sec() -> float:
    try:
        return max(1.0, float(os.environ.get("CYCLE_SCHEDULER_TICK_TIMEOUT_SEC", "120")))
    except Exception:
        return 120.0


def is_cycle_scheduler_running() -> bool:
    with _scheduler_state_lock:
        return _scheduler_running


def seconds_since_last_cycle_tick() -> float | None:
    with _scheduler_state_lock:
        if not _scheduler_running or _last_tick_ok is None:
            return None
        last_tick_ok = _last_tick_ok
    return max(0.0, time.monotonic() - last_tick_ok)


async def _scheduler_loop() -> None:
    global _last_tick_ok
    while is_cycle_scheduler_running():
        timeout_sec = _tick_timeout_sec()
        try:
            await asyncio.wait_for(async_schedule_due_cycles_once(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            logger.error("Cycle scheduler tick timed out after %ss", timeout_sec)
        except Exception:
            logger.exception("Cycle scheduler tick failed")
        else:
            with _scheduler_state_lock:
                if _scheduler_running:
                    _last_tick_ok = time.monotonic()
        await asyncio.sleep(_poll_interval_sec)


def _scheduler_thread_main() -> None:
    asyncio.run(_scheduler_loop())


def start_cycle_scheduler() -> None:
    global _last_tick_ok, _scheduler_running, _scheduler_task, _scheduler_thread
    with _scheduler_state_lock:
        if _scheduler_running:
            return
        _scheduler_running = True
        _last_tick_ok = time.monotonic()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _scheduler_thread = threading.Thread(
                target=_scheduler_thread_main,
                daemon=True,
                name="cycle-scheduler",
            )
            _scheduler_thread.start()
        else:
            _scheduler_task = loop.create_task(_scheduler_loop(), name="cycle-scheduler")


def stop_cycle_scheduler() -> None:
    global _last_tick_ok, _scheduler_running, _scheduler_task
    with _scheduler_state_lock:
        _scheduler_running = False
        _last_tick_ok = None
        scheduler_task = _scheduler_task
        _scheduler_task = None
    if scheduler_task is not None and not scheduler_task.done():
        scheduler_task.cancel()
