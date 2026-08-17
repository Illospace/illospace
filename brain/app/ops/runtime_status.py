"""One evidence-based snapshot for the operator system page."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.scheduler.cold_start import scheduler_liveness_checkpoint
from brain.app.scheduler.daemon import async_scheduler_health_snapshot
from brain.app.scheduler.stale_run_reaper import agent_run_maintenance_snapshot
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.cycle import Cycle
from brain.systems.cycles.common import ILLO_LANE_EXECUTOR_BINDING
from brain.systems.runs.cortex.queue_health import (
    QueueHealth,
    queued_backlog_health_snapshot_async,
)
from brain.systems.runs.cortex.worker_liveness import worker_liveness_checkpoint
from brain.systems.runs.token_usage import async_summarize_token_totals

_PROCESS_STARTED_AT = datetime.now(timezone.utc)
_ACTIVE_RUN_STATUSES = ("starting", "running", "paused", "verifying")
_STATE_WEIGHT = {"good": 0, "late": 1, "stalled": 2}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    normalized = _utc(value)
    return normalized.isoformat() if normalized is not None else None


def _age_seconds(now: datetime, value: datetime | None) -> int | None:
    normalized = _utc(value)
    if normalized is None:
        return None
    return max(0, int((now - normalized).total_seconds()))


def _worst_state(*states: str) -> str:
    return max(states, key=lambda state: _STATE_WEIGHT[state], default="good")


def _age_state(age_seconds: int | None, *, late_after: int, stalled_after: int) -> str:
    if age_seconds is None or age_seconds > stalled_after:
        return "stalled"
    if age_seconds > late_after:
        return "late"
    return "good"


def _worker_state(
    *,
    heartbeat_age_seconds: int | None,
    queue_health: QueueHealth,
) -> tuple[str, str]:
    heartbeat_state = _age_state(
        heartbeat_age_seconds,
        late_after=30,
        stalled_after=90,
    )
    if queue_health.stale_queued_backlog:
        return (
            "stalled",
            "Queued work is past the worker watchdog without current claim capacity.",
        )
    if heartbeat_state == "stalled":
        return "stalled", "No current worker heartbeat is recorded."
    if heartbeat_state == "late":
        return "late", "The worker heartbeat is late."
    if queue_health.queued:
        return (
            "good",
            "The worker heartbeat is current and queued work is inside the watchdog.",
        )
    return "good", "The worker heartbeat is current; no run is queued."


def _scheduler_state(
    *, tick_age_seconds: int | None, lag_seconds: int
) -> tuple[str, str]:
    tick_state = _age_state(tick_age_seconds, late_after=120, stalled_after=900)
    lag_state = (
        "stalled" if lag_seconds > 900 else "late" if lag_seconds > 0 else "good"
    )
    state = _worst_state(tick_state, lag_state)
    if tick_age_seconds is None:
        return "stalled", "No scheduler tick has been recorded."
    if state == "stalled":
        return (
            state,
            "The scheduler tick or oldest due job is more than 15 minutes late.",
        )
    if state == "late":
        return state, "The scheduler tick or at least one job is late."
    return state, "Scheduler ticks and due jobs are on time."


def _runs_state(
    *,
    stale_queued_backlog: bool,
    overdue_deadlines: int,
) -> tuple[str, str]:
    if overdue_deadlines:
        return "stalled", "At least one active run is past its deadline."
    if stale_queued_backlog:
        return (
            "stalled",
            "Queued work is past the worker watchdog without current claim capacity.",
        )
    return "good", "Run queue and active deadlines are within their limits."


def _cycles_state(max_overdue_seconds: int | None) -> tuple[str, str]:
    if max_overdue_seconds is None:
        return "good", "No enabled cycle is overdue."
    if max_overdue_seconds > 900:
        return "stalled", "The oldest enabled cycle is more than 15 minutes overdue."
    return "late", "At least one enabled cycle is due."


def _deploy_evidence() -> dict[str, Any]:
    raw_sha = os.getenv("ILLO_BUILD_COMMIT", "").strip()
    sha = raw_sha if raw_sha and raw_sha.lower() != "unknown" else None
    raw_built_at = os.getenv("ILLO_BUILD_TIME", "").strip()
    built_at: datetime | None = None
    if raw_built_at and raw_built_at.lower() != "unknown":
        try:
            built_at = datetime.fromisoformat(raw_built_at.replace("Z", "+00:00"))
        except ValueError:
            built_at = None
    raw_deployed_at = os.getenv("ILLO_DEPLOY_TIME", "").strip()
    deployed_at: datetime | None = None
    if raw_deployed_at and raw_deployed_at.lower() != "unknown":
        try:
            deployed_at = datetime.fromisoformat(raw_deployed_at.replace("Z", "+00:00"))
        except ValueError:
            deployed_at = None
    if not sha:
        state = "stalled"
        reason = "The running API has no build commit evidence."
    elif deployed_at is None:
        state = "late"
        reason = "The running API reports its build commit but not its deploy time."
    else:
        state = "good"
        reason = "The running API reports its build commit and deploy time."
    return {
        "state": state,
        "sha": sha,
        "deployed_at": _iso(deployed_at),
        "built_at": _iso(built_at),
        "process_started_at": _PROCESS_STARTED_AT.isoformat(),
        "reason": reason,
    }


async def async_runtime_status_snapshot(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return compact health rows backed by durable or process-local facts."""

    captured_at = _utc(now or datetime.now(timezone.utc))
    if captured_at is None:
        raise ValueError("now is required")

    last_claimed_at = await session.scalar(select(func.max(AgentRunRow.started_at)))
    worker_heartbeat_at = await worker_liveness_checkpoint(session)
    queue_health = await queued_backlog_health_snapshot_async(
        session=session,
        now=captured_at,
    )
    queued = queue_health.queued
    running = int(
        await session.scalar(
            select(func.count())
            .select_from(AgentRunRow)
            .where(AgentRunRow.status == "running")
        )
        or 0
    )
    oldest_running_at = await session.scalar(
        select(
            func.min(func.coalesce(AgentRunRow.started_at, AgentRunRow.created_at))
        ).where(AgentRunRow.status == "running")
    )
    overdue_deadlines = int(
        await session.scalar(
            select(func.count())
            .select_from(AgentRunRow)
            .where(
                AgentRunRow.status.in_(_ACTIVE_RUN_STATUSES),
                AgentRunRow.deadline_at.is_not(None),
                AgentRunRow.deadline_at < captured_at,
            )
        )
        or 0
    )

    oldest_queued_age = queue_health.oldest_queued_age_seconds
    worker_state, worker_reason = _worker_state(
        heartbeat_age_seconds=_age_seconds(captured_at, worker_heartbeat_at),
        queue_health=queue_health,
    )
    runs_state, runs_reason = _runs_state(
        stale_queued_backlog=queue_health.stale_queued_backlog,
        overdue_deadlines=overdue_deadlines,
    )

    scheduler_snapshot = await async_scheduler_health_snapshot(session, now=captured_at)
    last_tick_at = await scheduler_liveness_checkpoint(session)
    tick_age = _age_seconds(captured_at, last_tick_at)
    lag = (
        scheduler_snapshot.get("lag")
        if isinstance(scheduler_snapshot.get("lag"), dict)
        else {}
    )
    lag_seconds = max(0, int(lag.get("lag_seconds") or 0))
    lagging_jobs = (
        lag.get("lagging_jobs") if isinstance(lag.get("lagging_jobs"), list) else []
    )
    scheduler_state, scheduler_reason = _scheduler_state(
        tick_age_seconds=tick_age,
        lag_seconds=lag_seconds,
    )
    maintenance = agent_run_maintenance_snapshot(now=captured_at)

    enabled_cycle_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Cycle)
            .where(Cycle.enabled.is_(True), Cycle.deleted_at.is_(None))
        )
        or 0
    )
    last_cycle_at = await session.scalar(
        select(func.max(Cycle.last_run_at)).where(
            Cycle.enabled.is_(True), Cycle.deleted_at.is_(None)
        )
    )
    overdue_cycle_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Cycle)
            .where(
                Cycle.enabled.is_(True),
                Cycle.deleted_at.is_(None),
                Cycle.executor_binding == ILLO_LANE_EXECUTOR_BINDING,
                Cycle.next_run_at.is_not(None),
                Cycle.next_run_at < captured_at,
            )
        )
        or 0
    )
    oldest_overdue_cycle_at = await session.scalar(
        select(func.min(Cycle.next_run_at)).where(
            Cycle.enabled.is_(True),
            Cycle.deleted_at.is_(None),
            Cycle.executor_binding == ILLO_LANE_EXECUTOR_BINDING,
            Cycle.next_run_at.is_not(None),
            Cycle.next_run_at < captured_at,
        )
    )
    overdue_cycles = list(
        (
            await session.scalars(
                select(Cycle)
                .where(
                    Cycle.enabled.is_(True),
                    Cycle.deleted_at.is_(None),
                    Cycle.executor_binding == ILLO_LANE_EXECUTOR_BINDING,
                    Cycle.next_run_at.is_not(None),
                    Cycle.next_run_at < captured_at,
                )
                .order_by(Cycle.next_run_at.asc())
                .limit(20)
            )
        ).all()
    )
    cycle_rows = [
        {
            "id": int(cycle.id),
            "name": cycle.name,
            "next_run_at": _iso(cycle.next_run_at),
            "overdue_seconds": _age_seconds(captured_at, cycle.next_run_at) or 0,
            "last_run_at": _iso(cycle.last_run_at),
            "last_status": cycle.last_status,
        }
        for cycle in overdue_cycles
    ]
    max_cycle_overdue = _age_seconds(captured_at, oldest_overdue_cycle_at)
    cycles_state, cycles_reason = _cycles_state(max_cycle_overdue)

    today = captured_at.replace(hour=0, minute=0, second=0, microsecond=0)
    usage = await async_summarize_token_totals(session, since=today)
    spend_cost = round(float(usage.get("estimated_cost") or 0), 6)
    spend_tokens = int(usage.get("tokens_total") or 0)

    deploy = _deploy_evidence()
    rows = {
        "worker": {
            "state": worker_state,
            "heartbeat_at": _iso(worker_heartbeat_at),
            "heartbeat_age_seconds": _age_seconds(captured_at, worker_heartbeat_at),
            "last_claimed_at": _iso(last_claimed_at),
            "last_claim_age_seconds": _age_seconds(captured_at, last_claimed_at),
            "queued": queued,
            "oldest_queued_age_seconds": oldest_queued_age,
            "reason": worker_reason,
        },
        "scheduler": {
            "state": scheduler_state,
            "last_tick_at": _iso(last_tick_at),
            "tick_age_seconds": tick_age,
            "overdue_jobs": len(lagging_jobs),
            "max_lag_seconds": lag_seconds,
            "reason": scheduler_reason,
        },
        "maintenance": maintenance,
        "runs": {
            "state": runs_state,
            "queued": queued,
            "running": running,
            "oldest_queued_age_seconds": oldest_queued_age,
            "oldest_running_age_seconds": _age_seconds(captured_at, oldest_running_at),
            "overdue_deadlines": overdue_deadlines,
            "reason": runs_reason,
        },
        "cycles": {
            "state": cycles_state,
            "enabled": enabled_cycle_count,
            "overdue": overdue_cycle_count,
            "last_fired_at": _iso(last_cycle_at),
            "max_overdue_seconds": max_cycle_overdue,
            "items": cycle_rows,
            "items_truncated": overdue_cycle_count > len(cycle_rows),
            "reason": cycles_reason,
        },
        "spend": {
            "state": "good",
            "amount_usd": spend_cost,
            "tokens": spend_tokens,
            "runs": int(usage.get("runs") or 0),
            "since": today.isoformat(),
            "reason": "Today's recorded model usage is available.",
        },
        "deploy": deploy,
    }
    overall_state = _worst_state(*(row["state"] for row in rows.values()))
    return {
        "captured_at": captured_at.isoformat(),
        "overall": {
            "state": overall_state,
            "reason": "All evidence is current."
            if overall_state == "good"
            else "One or more runtime signals need attention.",
        },
        **rows,
    }
