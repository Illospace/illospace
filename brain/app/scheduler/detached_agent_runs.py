"""Detached AgentRun scheduler state transitions and reconciliation."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.contracts.scheduler_handoff import (
    HANDOFF_STATUS_DISPATCHED,
    DetachedAgentRunHandoff,
)
from brain.kernel.common.time import ensure_utc
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.scheduler import (
    SchedulerJob,
    SchedulerRun,
    SchedulerRunStep,
)
from brain.app.scheduler.runtime import (
    RUN_STATUS_EXECUTING,
    RUN_STATUS_SETTLED_FAILURE,
    RUN_STATUS_SETTLED_SUCCESS,
    async_finish_run,
    async_release_lease,
    trace_id_for_run_id,
)
from brain.app.scheduler.scheduler_failure_guard import (
    async_reset_scheduler_job_failure_guard,
)
from brain.systems.runs.status import (
    TERMINAL_RUN_STATUSES as TERMINAL_AGENT_RUN_STATUSES,
    RunStatus as AgentRunStatus,
    coerce_run_status as coerce_agent_run_status,
)

RetryableFailureSummary = Callable[
    ...,
    tuple[str, dict[str, Any]],
]
ApplyFailureGuard = Callable[..., Awaitable[None]]


def _agent_run_summary(
    agent_run_id: int,
    *,
    status: str,
    reconciled_at: datetime | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "run_id": agent_run_id,
        "status": status,
    }
    if reconciled_at is not None:
        summary["reconciled_at"] = reconciled_at.isoformat()
    return summary


async def async_mark_detached_run_dispatched(
    session: AsyncSession,
    run: SchedulerRun,
    step: SchedulerRunStep,
    *,
    handoff: DetachedAgentRunHandoff,
    step_result: dict[str, Any],
    step_results: list[dict[str, Any]],
    now: datetime,
) -> SchedulerRun:
    """Persist the non-terminal scheduler state after AgentRun dispatch."""
    agent_run_id = handoff.agent_run_id
    agent_summary = _agent_run_summary(
        agent_run_id,
        status=HANDOFF_STATUS_DISPATCHED,
    )

    step.status = RUN_STATUS_EXECUTING
    step.finished_at = None
    step.result_summary = {
        "results": list(step_result.get("results") or []),
        "agent_run": agent_summary,
    }
    step.error_text = None
    step.agent_run_id = agent_run_id
    step.trace_id = trace_id_for_run_id(agent_run_id)

    run.agent_run_id = agent_run_id
    run.trace_id = trace_id_for_run_id(agent_run_id)
    run.status = RUN_STATUS_EXECUTING
    run.result_summary = {
        "steps": step_results,
        "agent_run": agent_summary,
    }
    run.error_text = None
    run.finished_at = None
    if run.lease_id:
        await async_release_lease(
            session,
            run.lease_id,
            reason="agent_run_dispatched",
            now=now,
        )
    await session.flush()
    return run


def _agent_run_finished_at(agent_run: AgentRunRow, *, now: datetime) -> datetime:
    for attr_name in ("completed_at", "failed_at", "canceled_at"):
        value = getattr(agent_run, attr_name, None)
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return ensure_utc(value)
    return now


def _terminal_scheduler_status(agent_status: AgentRunStatus) -> str:
    if agent_status == AgentRunStatus.COMPLETED:
        return RUN_STATUS_SETTLED_SUCCESS
    return RUN_STATUS_SETTLED_FAILURE


async def async_reconcile_detached_runs(
    session: AsyncSession,
    *,
    retryable_failure_summary: RetryableFailureSummary,
    apply_failure_guard: ApplyFailureGuard,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Settle detached scheduler runs from their linked AgentRun outcomes."""
    clock = ensure_utc(now)
    runs = (
        await session.scalars(
            select(SchedulerRun)
            .where(
                SchedulerRun.status == RUN_STATUS_EXECUTING,
                SchedulerRun.agent_run_id.is_not(None),
            )
            .order_by(SchedulerRun.id.asc())
        )
    ).all()
    reconciled: list[dict[str, Any]] = []
    for run in runs:
        agent_run_id = int(run.agent_run_id)
        agent_run = await session.get(AgentRunRow, agent_run_id)
        if agent_run is None:
            continue
        agent_status = coerce_agent_run_status(agent_run.status)
        if agent_status not in TERMINAL_AGENT_RUN_STATUSES:
            continue

        terminal_status = _terminal_scheduler_status(agent_status)
        job = await session.get(SchedulerJob, run.job_id)
        error_text = None
        if terminal_status == RUN_STATUS_SETTLED_FAILURE:
            error_text = (
                f"Agent run {agent_run_id} ended with status {agent_status.value}"
            )
        agent_summary = _agent_run_summary(
            agent_run_id,
            status=agent_status.value,
            reconciled_at=clock,
        )
        summary = {
            **(run.result_summary or {}),
            "agent_run": agent_summary,
        }
        scheduler_status = terminal_status
        if terminal_status == RUN_STATUS_SETTLED_FAILURE and job is not None:
            scheduler_status, summary = retryable_failure_summary(
                job,
                run,
                base_summary=summary,
                now=clock,
            )

        finished_at = _agent_run_finished_at(agent_run, now=clock)
        step = await session.scalar(
            select(SchedulerRunStep)
            .where(
                SchedulerRunStep.run_id == run.id,
                SchedulerRunStep.agent_run_id == agent_run_id,
            )
            .order_by(SchedulerRunStep.sequence_no.desc())
            .limit(1)
        )
        if step is not None:
            step.status = (
                RUN_STATUS_SETTLED_SUCCESS
                if terminal_status == RUN_STATUS_SETTLED_SUCCESS
                else scheduler_status
            )
            step.finished_at = finished_at
            step.result_summary = {
                **(step.result_summary or {}),
                "agent_run": agent_summary,
            }
            step.error_text = error_text
            step.agent_run_id = agent_run_id
            step.trace_id = trace_id_for_run_id(agent_run_id)
            await session.flush()

        await async_finish_run(
            session,
            run,
            status=scheduler_status,
            result_summary=summary,
            error_text=error_text,
            now=finished_at,
        )
        if job is not None:
            if scheduler_status == RUN_STATUS_SETTLED_SUCCESS:
                await async_reset_scheduler_job_failure_guard(
                    session,
                    job,
                    now=clock,
                )
            else:
                await apply_failure_guard(
                    session,
                    job,
                    run,
                    failure_key="detached_agent_run",
                    error_text=error_text or "Detached agent run failed",
                    now=clock,
                )
        reconciled.append(
            {
                "run_id": run.id,
                "agent_run_id": agent_run_id,
                "agent_run_status": agent_status.value,
                "status": scheduler_status,
            }
        )
    return reconciled
