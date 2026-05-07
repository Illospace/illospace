"""Background cycle scheduler."""
from __future__ import annotations

import logging
import os
import threading
import time

from brain.systems.cycles.service import schedule_due_cycles_once

logger = logging.getLogger("cycles.scheduler")

_scheduler_running = False
_scheduler_thread: threading.Thread | None = None
_poll_interval_sec = int(os.environ.get("CYCLE_SCHEDULER_POLL_SEC", "30"))


def _scheduler_loop() -> None:
    global _scheduler_running
    while _scheduler_running:
        try:
            schedule_due_cycles_once()
        except Exception:
            logger.exception("Cycle scheduler tick failed")
        time.sleep(_poll_interval_sec)


def start_cycle_scheduler() -> None:
    global _scheduler_running, _scheduler_thread
    if _scheduler_running:
        return
    _scheduler_running = True
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        daemon=True,
        name="cycle-scheduler",
    )
    _scheduler_thread.start()


def stop_cycle_scheduler() -> None:
    global _scheduler_running
    _scheduler_running = False
