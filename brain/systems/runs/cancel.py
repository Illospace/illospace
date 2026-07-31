"""Cancellation operations for active AgentRun execution."""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.contracts.statuses import OPEN_RUN_STATUS_VALUES
from brain.systems.runs.cycle_settlement import async_finalize_cycle_run_if_needed
from brain.systems.runs.events import run_event
from brain.systems.runs.status import RunStatus, TERMINAL_RUN_STATUSES
from brain.systems.runs.store import AsyncAgentRunStore
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.repositories.unit_of_work import UnitOfWork


async def async_cancel_open_runs_for_thread(
    session: AsyncSession,
    thread_id: str,
    *,
    reason: str = "canceled_for_thread",
) -> int:
    """Cancel every open run on a thread within the caller's transaction."""

    result = await session.scalars(
        select(AgentRunRow).where(
            AgentRunRow.thread_id == thread_id,
            AgentRunRow.status.in_(OPEN_RUN_STATUS_VALUES),
        )
    )
    store = AsyncAgentRunStore(session)
    count = 0
    for row in result.all():
        await store.append_event(
            run_event(
                int(row.id),
                "run.canceled",
                {"reason": reason},
                root_run_id=row.root_run_id,
            )
        )
        canceled = await store.set_status(
            row.id,
            RunStatus.CANCELED,
            reason=reason,
        )
        if canceled.status == RunStatus.CANCELED:
            await async_finalize_cycle_run_if_needed(int(row.id), status="canceled")
        count += 1
    return count


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
        return self._canceled


__all__ = ["RunCancelToken", "async_cancel_open_runs_for_thread"]
