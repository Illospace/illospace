"""Queue health helpers for the standalone AgentRun worker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import DateTime, case, cast, func, or_, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from brain.contracts.statuses import PROCESSING_RUN_STATUS_VALUES
from brain.platform.db.models.agent_run import AgentRunRow
from brain.systems.runs.status import RunStatus

UnitOfWork = None

_DEFAULT_RUNNER_CONCURRENCY = 4
_MAX_RUNNER_CONCURRENCY = 32
_DEFAULT_QUEUED_WATCHDOG_AFTER_SEC = 15.0


@dataclass(frozen=True, slots=True)
class QueuedBacklogSnapshot:
    queued: int
    oldest_queued_at: datetime | None
    recent_active_runs: int


@dataclass(frozen=True, slots=True)
class QueueHealthPolicy:
    watchdog_after_seconds: float
    configured_concurrency: int


@dataclass(frozen=True, slots=True)
class QueueHealth:
    queued: int
    recent_active_runs: int
    configured_concurrency: int
    oldest_queued_at: datetime | None
    oldest_queued_age_seconds: int | None
    watchdog_after_seconds: float
    queue_moving_at_capacity: bool
    stale_queued_backlog: bool


def _unit_of_work_factory():
    global UnitOfWork
    if UnitOfWork is None:
        from brain.platform.db.repositories.unit_of_work import (
            UnitOfWork as _UnitOfWork,
        )

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
    return (
        _coerce_concurrency(
            os.getenv("ILLO_AGENT_RUNNER_CONCURRENCY"),
            default=_DEFAULT_RUNNER_CONCURRENCY,
        )
        or _DEFAULT_RUNNER_CONCURRENCY
    )


def queued_watchdog_after_seconds() -> float:
    return _coerce_float(
        os.getenv("ILLO_AGENT_RUN_QUEUED_WATCHDOG_SECONDS"),
        default=_DEFAULT_QUEUED_WATCHDOG_AFTER_SEC,
        minimum=1.0,
    )


def _resolved_stale_after_seconds(value: Any = None) -> float:
    default = queued_watchdog_after_seconds()
    return _coerce_float(value, default=default, minimum=1.0)


def _queued_since_expression(*, dialect_name: str):
    """Return the timestamp for the current stay in the queue.

    Previously processed rows use the timestamp recorded by
    ``interrupt_and_requeue`` in ``metadata.interruption.interrupted_at``.
    Unprocessed rows, and historical requeues without that record, fall back to
    ``created_at``. The fallback can overstate queue tenure for a requeued row
    whose mutable interruption metadata is missing, but unrelated writes cannot
    reset tenure through ``updated_at``.
    """

    was_previously_processed = or_(
        AgentRunRow.started_at.is_not(None),
        AgentRunRow.execution_attempt > 0,
    )
    interrupted_at_text = AgentRunRow.metadata_["interruption"][
        "interrupted_at"
    ].as_string()
    if dialect_name == "sqlite":
        interrupted_at = type_coerce(
            func.datetime(interrupted_at_text),
            DateTime(timezone=True),
        )
    else:
        interrupted_at = cast(interrupted_at_text, DateTime(timezone=True))
    return case(
        (
            was_previously_processed,
            func.coalesce(interrupted_at, AgentRunRow.created_at),
        ),
        else_=AgentRunRow.created_at,
    )


async def queued_backlog_snapshot_async(
    *,
    stale_after_seconds: float | None = None,
    session: AsyncSession | None = None,
    now: datetime | None = None,
) -> QueuedBacklogSnapshot:
    """Return queued count, oldest queued-since time, and recent active claims.

    The optional threshold defaults to the existing in-worker watchdog setting,
    keeping no-argument callers on the current ~15 second behavior. Processing
    rows only count toward capacity when their durable liveness was refreshed
    inside that same window; stale rows cannot make a dead queue look saturated.
    """

    threshold_seconds = _resolved_stale_after_seconds(stale_after_seconds)
    captured_at = now or datetime.now(timezone.utc)
    active_cutoff = captured_at - timedelta(seconds=threshold_seconds)
    if session is not None:
        return await _queued_backlog_snapshot_from_session(
            session,
            active_cutoff=active_cutoff,
        )
    async with _unit_of_work_factory()() as uow:
        return await _queued_backlog_snapshot_from_session(
            uow.session,
            active_cutoff=active_cutoff,
        )


async def _queued_backlog_snapshot_from_session(
    session: AsyncSession,
    *,
    active_cutoff: datetime,
) -> QueuedBacklogSnapshot:
    dialect_name = session.get_bind().dialect.name
    queued_result = await session.execute(
        select(
            func.count(AgentRunRow.id),
            func.min(_queued_since_expression(dialect_name=dialect_name)),
        ).where(AgentRunRow.status == RunStatus.QUEUED.value)
    )
    queued_count, oldest_queued_at = queued_result.one()
    active_count = await session.scalar(
        select(func.count(AgentRunRow.id)).where(
            AgentRunRow.status.in_(PROCESSING_RUN_STATUS_VALUES),
            func.coalesce(
                AgentRunRow.updated_at,
                AgentRunRow.started_at,
                AgentRunRow.created_at,
            )
            >= active_cutoff,
        )
    )
    return QueuedBacklogSnapshot(
        queued=int(queued_count or 0),
        oldest_queued_at=_normalize_datetime(oldest_queued_at),
        recent_active_runs=int(active_count or 0),
    )


def _normalize_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def evaluate_queue_health(
    snapshot: QueuedBacklogSnapshot,
    policy: QueueHealthPolicy,
    *,
    now: datetime,
) -> QueueHealth:
    oldest_queued_age_seconds = None
    if snapshot.oldest_queued_at is not None:
        oldest_queued_age_seconds = max(
            0.0,
            (now - snapshot.oldest_queued_at).total_seconds(),
        )
    queue_past_threshold = (
        snapshot.queued > 0
        and oldest_queued_age_seconds is not None
        and oldest_queued_age_seconds >= policy.watchdog_after_seconds
    )
    # Recency is resolved before capacity: only claims with current liveness
    # count as occupied slots. Saturation can excuse an old queue only when all
    # configured slots are demonstrably still moving.
    queue_moving_at_capacity = (
        snapshot.recent_active_runs >= policy.configured_concurrency
    )
    return QueueHealth(
        queued=snapshot.queued,
        recent_active_runs=snapshot.recent_active_runs,
        configured_concurrency=policy.configured_concurrency,
        oldest_queued_at=snapshot.oldest_queued_at,
        oldest_queued_age_seconds=(
            int(oldest_queued_age_seconds)
            if oldest_queued_age_seconds is not None
            else None
        ),
        watchdog_after_seconds=policy.watchdog_after_seconds,
        queue_moving_at_capacity=queue_moving_at_capacity,
        stale_queued_backlog=queue_past_threshold and not queue_moving_at_capacity,
    )


async def queued_backlog_health_snapshot_async(
    *,
    stale_after_seconds: float | None = None,
    configured_concurrency: int | None = None,
    session: AsyncSession | None = None,
    now: datetime | None = None,
) -> QueueHealth:
    watchdog_after_seconds = _resolved_stale_after_seconds(stale_after_seconds)
    snapshot_kwargs: dict[str, Any] = {
        "stale_after_seconds": watchdog_after_seconds,
    }
    if session is not None:
        snapshot_kwargs["session"] = session
    if now is not None:
        snapshot_kwargs["now"] = now
    snapshot = await queued_backlog_snapshot_async(**snapshot_kwargs)
    if configured_concurrency is None:
        configured_concurrency = runner_concurrency()
    else:
        configured_concurrency = (
            _coerce_concurrency(
                configured_concurrency,
                default=_DEFAULT_RUNNER_CONCURRENCY,
            )
            or _DEFAULT_RUNNER_CONCURRENCY
        )
    return evaluate_queue_health(
        snapshot,
        QueueHealthPolicy(
            watchdog_after_seconds=watchdog_after_seconds,
            configured_concurrency=configured_concurrency,
        ),
        now=now or datetime.now(timezone.utc),
    )


@dataclass
class QueueStallMonitor:
    check_interval_seconds: float
    stall_grace_seconds: float
    last_checked_at: float = 0.0
    stale_since: float | None = None

    def should_check(self, *, now: float) -> bool:
        return now - self.last_checked_at >= self.check_interval_seconds

    def observe(self, queue_health: QueueHealth, *, now: float) -> int | None:
        self.last_checked_at = now
        if not queue_health.stale_queued_backlog:
            self.stale_since = None
            return None
        if self.stale_since is None:
            self.stale_since = now
        stale_for_seconds = now - self.stale_since
        if stale_for_seconds < self.stall_grace_seconds:
            return None
        return int(stale_for_seconds)


__all__ = [
    "QueueHealth",
    "QueueHealthPolicy",
    "QueueStallMonitor",
    "QueuedBacklogSnapshot",
    "evaluate_queue_health",
    "queued_backlog_health_snapshot_async",
    "queued_backlog_snapshot_async",
    "queued_watchdog_after_seconds",
    "runner_concurrency",
]
