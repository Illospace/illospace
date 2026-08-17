"""Health snapshots for legacy cycle scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import assume_utc_optional
from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.systems.cycles.common import ILLO_LANE_EXECUTOR_BINDING
from brain.systems.cycles.status import CYCLE_RUN_ACTIVE_STATUS_VALUES


@dataclass(frozen=True)
class LegacyCycleBacklogSnapshot:
    status: str
    summary: str
    details: dict[str, Any]
    remediation: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cycle_row_payload(cycle: Cycle, *, now: datetime) -> dict[str, Any]:
    next_run_at = assume_utc_optional(cycle.next_run_at)
    overdue_seconds = int((now - next_run_at).total_seconds()) if next_run_at else None
    return {
        "id": cycle.id,
        "enabled": cycle.enabled,
        "next_run_at": cycle.next_run_at.isoformat() if cycle.next_run_at else None,
        "overdue_seconds": overdue_seconds,
        "last_run_at": cycle.last_run_at.isoformat() if cycle.last_run_at else None,
        "last_status": cycle.last_status,
        "last_error_present": bool(cycle.last_error),
    }


def _cycle_run_row_payload(run: CycleRun, *, now: datetime) -> dict[str, Any]:
    scheduled_for = assume_utc_optional(run.scheduled_for)
    stale_seconds = int((now - scheduled_for).total_seconds()) if scheduled_for else None
    return {
        "id": run.id,
        "cycle_id": run.cycle_id,
        "status": run.status,
        "scheduled_for": run.scheduled_for.isoformat() if run.scheduled_for else None,
        "stale_seconds": stale_seconds,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "skip_reason": run.skip_reason,
        "error_present": bool(run.error),
        "idea_id": str(run.idea_id) if run.idea_id is not None else None,
        "run_id": run.run_id,
    }


async def async_legacy_cycle_backlog_snapshot(
    session: AsyncSession,
    *,
    stale_after_minutes: int,
    sample_limit: int,
    now: datetime | None = None,
) -> LegacyCycleBacklogSnapshot:
    now = now or _utc_now()
    stale_cutoff = now - timedelta(minutes=stale_after_minutes)
    due_cycle_clause = and_(
        Cycle.deleted_at.is_(None),
        Cycle.enabled.is_(True),
        Cycle.executor_binding == ILLO_LANE_EXECUTOR_BINDING,
        Cycle.next_run_at.is_not(None),
        Cycle.next_run_at <= stale_cutoff,
    )
    due_cycle_count = await session.scalar(
        select(func.count()).select_from(Cycle).where(due_cycle_clause)
    ) or 0
    stale_due_cycles = list(
        (
            await session.scalars(
                select(Cycle)
                .where(due_cycle_clause)
                .order_by(Cycle.next_run_at.asc())
                .limit(sample_limit)
            )
        ).all()
    )

    active_run_clause = and_(
        CycleRun.status.in_(CYCLE_RUN_ACTIVE_STATUS_VALUES),
        CycleRun.scheduled_for <= stale_cutoff,
    )
    active_run_count = await session.scalar(
        select(func.count()).select_from(CycleRun).where(active_run_clause)
    ) or 0
    stale_active_runs = list(
        (
            await session.scalars(
                select(CycleRun)
                .where(active_run_clause)
                .order_by(CycleRun.scheduled_for.asc())
                .limit(sample_limit)
            )
        ).all()
    )

    reasons: list[str] = []
    if due_cycle_count:
        reasons.append(f"{int(due_cycle_count)} stale due cycle(s)")
    if active_run_count:
        reasons.append(f"{int(active_run_count)} stale active cycle run(s)")

    status = "degraded" if reasons else "ok"
    return LegacyCycleBacklogSnapshot(
        status=status,
        summary=", ".join(reasons) if reasons else "legacy cycle scheduler has no stale due work",
        details={
            "stale_after_minutes": stale_after_minutes,
            "active_cycle_run_statuses": list(CYCLE_RUN_ACTIVE_STATUS_VALUES),
            "stale_due_cycles_count": int(due_cycle_count),
            "stale_due_cycles": [_cycle_row_payload(row, now=now) for row in stale_due_cycles],
            "stale_active_cycle_runs_count": int(active_run_count),
            "stale_active_cycle_runs": [
                _cycle_run_row_payload(row, now=now) for row in stale_active_runs
            ],
        },
        remediation=(
            "Ensure the production worker starts the legacy cycle scheduler and inspect cycle execution logs."
            if status != "ok"
            else None
        ),
    )
