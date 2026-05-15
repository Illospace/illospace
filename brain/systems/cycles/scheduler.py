"""Background cycle scheduler."""
from __future__ import annotations

import logging
import os
import asyncio
import threading

from brain.systems.cycles.service import async_schedule_due_cycles_once

logger = logging.getLogger("cycles.scheduler")

_scheduler_running = False
_scheduler_thread: threading.Thread | None = None
_scheduler_task: asyncio.Task | None = None
_poll_interval_sec = int(os.environ.get("CYCLE_SCHEDULER_POLL_SEC", "30"))


async def _scheduler_loop() -> None:
    global _scheduler_running
    while _scheduler_running:
        try:
            await async_schedule_due_cycles_once()
        except Exception:
            logger.exception("Cycle scheduler tick failed")
        await asyncio.sleep(_poll_interval_sec)


def _scheduler_thread_main() -> None:
    asyncio.run(_scheduler_loop())


def start_cycle_scheduler() -> None:
    global _scheduler_running, _scheduler_task, _scheduler_thread
    if _scheduler_running:
        return
    _scheduler_running = True
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
    global _scheduler_running, _scheduler_task
    _scheduler_running = False
    if _scheduler_task is not None and not _scheduler_task.done():
        _scheduler_task.cancel()
    _scheduler_task = None
