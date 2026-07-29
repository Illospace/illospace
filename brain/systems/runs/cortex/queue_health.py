"""Queue health helpers for the standalone AgentRun worker."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import case, func, or_, select

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


def _resolved_stale_after_seconds(value: Any = None) -> float:
    default = queued_watchdog_after_seconds()
    return _coerce_float(value, default=default, minimum=1.0)


def _queued_since_expression():
    """Return the timestamp for the current stay in the queue.

    A valid interrupted requeue can only come from a processing state. Those
    rows have either started once or acquired an execution attempt, and the
    interruption transition atomically stamps ``updated_at`` to the same time
    recorded in ``metadata.interruption.interrupted_at``. Newly admitted rows
    have neither marker, so their queue age still begins at creation.
    """

    was_previously_processed = or_(
        AgentRunRow.started_at.is_not(None),
        AgentRunRow.execution_attempt > 0,
    )
    return case(
        (
            was_previously_processed,
            func.coalesce(AgentRunRow.updated_at, AgentRunRow.created_at),
        ),
        else_=AgentRunRow.created_at,
    )


async def queued_backlog_snapshot_async(
    *,
    stale_after_seconds: float | None = None,
) -> tuple[int, datetime | None, int]:
    """Return queued count, oldest queued-since time, and recent active claims.

    The optional threshold defaults to the existing in-worker watchdog setting,
    keeping no-argument callers on the current ~15 second behavior. Processing
    rows only count toward capacity when their durable liveness was refreshed
    inside that same window; stale rows cannot make a dead queue look saturated.
    """

    threshold_seconds = _resolved_stale_after_seconds(stale_after_seconds)
    active_cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=threshold_seconds
    )
    async with _unit_of_work_factory()() as uow:
        queued_result = await uow.session.execute(
            select(
                func.count(AgentRunRow.id),
                func.min(_queued_since_expression()),
            ).where(
                AgentRunRow.status == RunStatus.QUEUED.value
            )
        )
        queued_count, oldest_queued_at = queued_result.one()
        active_count = await uow.session.scalar(
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
    return (
        int(queued_count or 0),
        _normalize_datetime(oldest_queued_at),
        int(active_count or 0),
    )


def _normalize_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def queued_backlog_health_snapshot_async(
    *,
    stale_after_seconds: float | None = None,
) -> dict[str, Any]:
    watchdog_after_seconds = _resolved_stale_after_seconds(stale_after_seconds)
    if stale_after_seconds is None:
        # Keep the long-standing no-argument seam for the worker and tests that
        # replace its database snapshot.
        queued_count, oldest_queued_at, active_count = (
            await queued_backlog_snapshot_async()
        )
    else:
        queued_count, oldest_queued_at, active_count = (
            await queued_backlog_snapshot_async(
                stale_after_seconds=watchdog_after_seconds,
            )
        )
    configured_concurrency = runner_concurrency()
    oldest_queued_age_seconds = None
    if oldest_queued_at is not None:
        oldest_queued_age_seconds = max(
            0.0,
            (datetime.now(timezone.utc) - oldest_queued_at).total_seconds(),
        )
    queue_past_threshold = (
        queued_count > 0
        and oldest_queued_age_seconds is not None
        and oldest_queued_age_seconds >= watchdog_after_seconds
    )
    # Recency is resolved before capacity: only claims with current liveness
    # count as occupied slots. Saturation can excuse an old queue only when all
    # configured slots are demonstrably still moving.
    queue_moving_at_capacity = active_count >= configured_concurrency
    stale_queued_backlog = queue_past_threshold and not queue_moving_at_capacity
    return {
        "queued": queued_count,
        "active_runs": active_count,
        "recent_active_runs": active_count,
        "configured_concurrency": configured_concurrency,
        "oldest_queued_at": oldest_queued_at.isoformat() if oldest_queued_at else None,
        "oldest_queued_age_seconds": int(oldest_queued_age_seconds)
        if oldest_queued_age_seconds is not None
        else None,
        "watchdog_after_seconds": int(watchdog_after_seconds),
        "queue_moving_at_capacity": queue_moving_at_capacity,
        "stale_queued_backlog": stale_queued_backlog,
    }


def _queue_health_message(queue_health: dict[str, Any]) -> str:
    queued = int(queue_health.get("queued") or 0)
    active_runs = int(queue_health.get("recent_active_runs") or 0)
    concurrency = int(queue_health.get("configured_concurrency") or 0)
    threshold = int(queue_health.get("watchdog_after_seconds") or 0)
    age = queue_health.get("oldest_queued_age_seconds")
    age_label = f"{int(age)}s" if age is not None else "unknown"
    detail = (
        f"{queued} run(s) queued; oldest queued for {age_label} "
        f"(threshold {threshold}s); {active_runs}/{concurrency} runner slot(s) "
        "have recent claim activity"
    )
    if queue_health.get("stale_queued_backlog"):
        return (
            f"AgentRun queue starvation: {detail}. "
            "No worker is claiming the queued backlog."
        )
    return f"AgentRun queue healthy: {detail}."


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the shared AgentRun queue-starvation predicate.",
    )
    parser.add_argument(
        "--stale-after-seconds",
        type=float,
        default=None,
        help=(
            "Alarm after this many seconds without enough recent claims "
            "(default: ILLO_AGENT_RUN_QUEUED_WATCHDOG_SECONDS or 15)."
        ),
    )
    args = parser.parse_args(argv)
    try:
        queue_health = asyncio.run(
            queued_backlog_health_snapshot_async(
                stale_after_seconds=args.stale_after_seconds,
            )
        )
    except Exception as exc:
        print(
            "AgentRun queue health check failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    message = _queue_health_message(queue_health)
    if queue_health.get("stale_queued_backlog"):
        print(message, file=sys.stderr)
        return 1
    print(message)
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
