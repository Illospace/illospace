"""Cancellation token for active AgentRun execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time
from typing import Callable

from sqlalchemy import select

from brain.systems.runs.status import RunStatus, TERMINAL_RUN_STATUSES
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.repositories.unit_of_work import UnitOfWork


@dataclass
class RunCancelToken:
    """Small DB-backed cancellation token consumed by provider/tool loops."""

    run_id: int
    poll_interval_sec: float = 0.5
    uow_factory: Callable[[], UnitOfWork] = UnitOfWork
    _last_check: float = field(default=0.0, init=False)
    _canceled: bool = field(default=False, init=False)

    async def a_is_set(self) -> bool:
        if self._canceled:
            return True
        now = time.monotonic()
        if now - self._last_check < self.poll_interval_sec:
            return False
        self._last_check = now
        try:
            async with self.uow_factory() as uow:
                status = await uow.session.scalar(
                    select(AgentRunRow.status).where(AgentRunRow.id == int(self.run_id)).limit(1)
                )
        except Exception:
            return False
        try:
            status_value = RunStatus(str(status or ""))
        except ValueError:
            return False
        self._canceled = status_value in TERMINAL_RUN_STATUSES
        return self._canceled

    def is_set(self) -> bool:
        if self._canceled:
            return True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.a_is_set())
        return False


__all__ = ["RunCancelToken"]
