"""Queue health helpers for the standalone AgentRun worker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from brain.contracts.statuses import PROCESSING_RUN_STATUS_VALUES
from brain.platform.db.models.agent_run import AgentRunRow
from brain.systems.runs.status import RunStatus

UnitOfWork = None

_DEFAULT_RUNNER_CONCURRENCY = 4
_MAX_RUNNER_CONCURRENCY = 32
_DEFAULT_QUEUED_WATCHDOG_AFTER_SEC = 15.0


def _unit_of_work_factory():
    global UnitOfWork
    if UnitOfWork is None:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork as _UnitOfWork

        UnitOfWork = _UnitOfWork
    return UnitOfWork


def _coerce_concurrency(value: Any, *, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return max(1, min(_MAX_RUNNER_CONCURRENCY, int(value)))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, *, default: float, minimum: float) -> float:
    try:
        if value is None or value == "":
            return default
        return max(float(minimum), float(value))
    except (TypeError, ValueError):
        return default


def runner_concurrency() -> int:
    return _coerce_concurrency(
        os.getenv("ILLO_AGENT_RUNNER_CONCURRENCY"),
        default=_DEFAULT_RUNNER_CONCURRENCY,
    ) or _DEFAULT_RUNNER_CONCURRENCY


def queued_watchdog_after_seconds() -> float:
    return _coerce_float(
        os.getenv("ILLO_AGENT_RUN_QUEUED_WATCHDOG_SECONDS"),
        default=_DEFAULT_QUEUED_WATCHDOG_AFTER_SEC,
        minimum=1.0,
    )


async def queued_backlog_snapshot_async() -> tuple[int, datetime | None, int]:
    async with _unit_of_work_factory()() as uow:
        queued_result = await uow.session.execute(
            select(func.count(AgentRunRow.id), func.min(AgentRunRow.created_at)).where(
                AgentRunRow.status == RunStatus.QUEUED.value
            )
        )
        queued_count, oldest_created_at = queued_result.one()
        active_count = await uow.session.scalar(
            select(func.count(AgentRunRow.id)).where(
                AgentRunRow.status.in_(PROCESSING_RUN_STATUS_VALUES)
            )
        )
    return int(queued_count or 0), _normalize_datetime(oldest_created_at), int(active_count or 0)


def _normalize_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def queued_backlog_health_snapshot_async() -> dict[str, Any]:
    queued_count, oldest_queued_at, active_count = await queued_backlog_snapshot_async()
    configured_concurrency = runner_concurrency()
    watchdog_after_seconds = queued_watchdog_after_seconds()
    oldest_queued_age_seconds = None
    if oldest_queued_at is not None:
        oldest_queued_age_seconds = max(
            0.0,
            (datetime.now(timezone.utc) - oldest_queued_at).total_seconds(),
        )
    stale_queued_backlog = (
        queued_count > 0
        and oldest_queued_age_seconds is not None
        and oldest_queued_age_seconds >= watchdog_after_seconds
        and active_count < configured_concurrency
    )
    return {
        "queued": queued_count,
        "active_runs": active_count,
        "configured_concurrency": configured_concurrency,
        "oldest_queued_at": oldest_queued_at.isoformat() if oldest_queued_at else None,
        "oldest_queued_age_seconds": int(oldest_queued_age_seconds)
        if oldest_queued_age_seconds is not None
        else None,
        "watchdog_after_seconds": int(watchdog_after_seconds),
        "stale_queued_backlog": stale_queued_backlog,
    }


@dataclass
class QueueStallMonitor:
    check_interval_seconds: float
    stall_grace_seconds: float
    last_checked_at: float = 0.0
    stale_since: float | None = None

    def should_check(self, *, now: float) -> bool:
        return now - self.last_checked_at >= self.check_interval_seconds

    def observe(self, queue_health: dict[str, Any], *, now: float) -> int | None:
        self.last_checked_at = now
        if not queue_health.get("stale_queued_backlog"):
            self.stale_since = None
            return None
        if self.stale_since is None:
            self.stale_since = now
        stale_for_seconds = now - self.stale_since
        if stale_for_seconds < self.stall_grace_seconds:
            return None
        return int(stale_for_seconds)


__all__ = [
    "QueueStallMonitor",
    "queued_backlog_health_snapshot_async",
    "queued_backlog_snapshot_async",
    "queued_watchdog_after_seconds",
    "runner_concurrency",
]
