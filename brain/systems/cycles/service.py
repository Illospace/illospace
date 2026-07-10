"""Cycle CRUD, scheduling, and execution helpers."""
from __future__ import annotations

import logging
import asyncio

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from brain.systems.cortex.events import publish
from brain.systems.cortex.thought_lifecycle import ThreadMessageCommand, post_thread_message
from brain.systems.cycles.status import CYCLE_RUN_ACTIVE_STATUSES, CYCLE_RUN_TERMINAL_STATUSES
from brain.systems.cycles.auth_preflight import (
    CycleAuthPreflightResult,
    async_preflight_cycle_external_auth,
)
from brain.systems.cycles.common import (
    REUSABLE_THREAD_EXECUTION_MODE,
    THREAD_OUTPUT_TARGET_TYPE,
    canonical_execution_mode,
    short_identifier,
    validate_nonempty_trimmed,
    validate_thinking_override,
)
from brain.systems.cycles.contract_gate import (
    async_prepare_cycle_run_visible_finalization,
    cycle_finalization_status_from_verdict,
    persisted_cycle_contract_verdict,
)
from brain.systems.cycles.memory import (
    append_cycle_run_output_target_snapshot,
    async_add_cycle_guidance,
    async_add_cycle_output_target,
    async_prepare_cycle_run_memory_snapshot as _async_prepare_cycle_run_memory_snapshot,
    async_record_cycle_revision,
    async_remove_cycle_output_target,
    finalize_cycle_run as _finalize_cycle_run,
    finalize_stale_cycle_run as _finalize_stale_cycle_run,
)
from brain.systems.cycles.prompts import (
    cycle_launch_envelope as _cycle_launch_envelope,
    cycle_run_message as _cycle_run_message,
    cycle_run_metadata as _cycle_run_metadata,
)
from brain.systems.cycles.schedules import (
    build_one_time_schedule_expr,
    compute_next_run_at,
    humanize_schedule,
    is_one_time_schedule_expr,
    validate_schedule_expr,
    validate_timezone_name,
)
from brain.systems.cycles.serializers import (
    serialize_cycle,
    serialize_cycle_guidance,
    serialize_cycle_output_target,
    serialize_cycle_run,
)
from brain.systems.cycles.execution import (
    async_resolve_cycle_execution_target,
    serialize_execution_idea,
)
from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work
from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.run import AgentRun
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.org import User
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger("cycles")

ACTIVE_RUN_STATUSES = CYCLE_RUN_ACTIVE_STATUSES
TERMINAL_RUN_STATUSES = CYCLE_RUN_TERMINAL_STATUSES
DEFAULT_STALE_CYCLE_RUN_SECONDS = 60
DEFAULT_CYCLE_RUN_CATCHUP_WINDOW_SECONDS = 24 * 60 * 60
RUN_STATUS_TO_CYCLE_RUN_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "error": "failed",
    "canceled": "failed",
    "cancelled": "failed",
    "expired": "failed",
    "blocked": "failed",
}

__all__ = [
    "REUSABLE_THREAD_EXECUTION_MODE",
    "async_add_cycle_guidance",
    "async_add_cycle_output_target",
    "async_execute_cycle_run",
    "async_finalize_cycle_run_from_run",
    "async_recover_stale_cycle_runs_once",
    "async_record_cycle_revision",
    "async_remove_cycle_output_target",
    "async_run_cycle_now",
    "async_schedule_due_cycles_once",
    "build_one_time_schedule_expr",
    "canonical_execution_mode",
    "compute_next_run_at",
    "finalize_cycle_run_from_run",
    "humanize_schedule",
    "is_one_time_schedule_expr",
    "serialize_cycle",
    "serialize_cycle_guidance",
    "serialize_cycle_output_target",
    "serialize_cycle_run",
    "validate_nonempty_trimmed",
    "validate_schedule_expr",
    "validate_thinking_override",
    "validate_timezone_name",
]


async def _async_admit_cycle_run(
    session,
    *,
    idea_id: str,
    message: str,
    priority: int,
    user_id: str | None,
    metadata: dict | None,
    cycle_run_id: int,
) -> int | None:
    result = await admit_work(
        session,
        WorkIntakeEvent(
            source="cycle",
            event_type="cycle.due_run",
            org_id=str((metadata or {}).get("org_id") or ""),
            actor={"id": user_id, "org_id": (metadata or {}).get("org_id")},
            target={"kind": "cortex_idea", "idea_id": idea_id},
            payload={"message": message, "metadata": dict(metadata or {})},
            policy={
                "priority": priority,
                "producer": "cycle",
                "idempotency_key": f"cycle_run:{cycle_run_id}",
                "run_event": "thread_reply",
            },
        ),
    )
    return result.run_id if result.ok else None


async def _async_append_cycle_thread_message(
    session,
    idea: Idea,
    cycle: Cycle,
    cycle_run: CycleRun,
    owner: User | None,
) -> tuple[dict, dict | None]:
    metadata = {
        "source": "cycle",
        "cycle_id": cycle.id,
        "cycle_run_id": cycle_run.id,
        "launch_envelope": _cycle_launch_envelope(cycle, cycle_run),
    }
    result = await post_thread_message(
        session,
        idea=idea,
        command=ThreadMessageCommand(
            idea_id=str(idea.id),
            role="user",
            content=cycle.prompt,
            actor={
                "user_id": str(cycle.user_id) if cycle.user_id else None,
                "org_id": str(cycle.org_id) if cycle.org_id else None,
                "name": getattr(owner, "name", None),
                "color": getattr(owner, "color", None),
            },
            attachments=[],
            metadata=metadata,
        ),
        parse_message_type=lambda _content, _role: "trigger",
        lifecycle_trigger="auto_cycle_message",
    )
    return result.message_payload, result.status_change


async def _async_append_cycle_auth_blocked_thread_message(
    session,
    idea: Idea,
    cycle: Cycle,
    cycle_run: CycleRun,
    preflight: CycleAuthPreflightResult,
) -> tuple[dict, dict | None]:
    metadata = {
        "source": "cycle",
        "cycle_id": cycle.id,
        "cycle_run_id": cycle_run.id,
        "auth_preflight": preflight.to_dict(),
    }
    result = await post_thread_message(
        session,
        idea=idea,
        command=ThreadMessageCommand(
            idea_id=str(idea.id),
            role="illo",
            content=preflight.visible_message or "Scheduled Cycle auth blocked.",
            actor={
                "org_id": str(cycle.org_id) if cycle.org_id else None,
                "name": "Illo",
            },
            attachments=[],
            metadata=metadata,
        ),
        parse_message_type=lambda _content, _role: "agent_response",
        lifecycle_trigger="cycle_auth_preflight_blocked",
    )
    return result.message_payload, result.status_change


async def async_run_cycle_now(cycle_id: int) -> dict:
    scheduled_for = datetime.now(timezone.utc)
    async with UnitOfWork() as uow:
        cycle = await uow.session.get(Cycle, cycle_id)
        if not cycle or cycle.deleted_at is not None:
            raise ValueError("Cycle not found")
        run = CycleRun(
            cycle_id=cycle_id,
            scheduled_for=scheduled_for,
            prompt_snapshot=cycle.prompt,
            status="queued",
        )
        uow.session.add(run)
        await uow.session.flush()
        await _async_prepare_cycle_run_memory_snapshot(uow.session, cycle, run)
        run_id = run.id

    await async_execute_cycle_run(run_id)

    async with UnitOfWork() as uow:
        run = await uow.session.get(CycleRun, run_id)
    return serialize_cycle_run(run)


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _agent_run_terminal_cycle_status(agent_run: AgentRun | None) -> str | None:
    if agent_run is None:
        return None
    return RUN_STATUS_TO_CYCLE_RUN_STATUS.get(str(agent_run.status or "").strip().lower())


async def async_recover_stale_cycle_runs_once(
    *,
    limit: int = 10,
    stale_after_seconds: int = DEFAULT_STALE_CYCLE_RUN_SECONDS,
    catchup_window_seconds: int = DEFAULT_CYCLE_RUN_CATCHUP_WINDOW_SECONDS,
) -> list[int]:
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(seconds=max(0, int(stale_after_seconds)))
    catchup_cutoff = now - timedelta(seconds=max(0, int(catchup_window_seconds)))
    executable_run_ids: list[int] = []

    async with UnitOfWork() as uow:
        active_stmt = (
            select(CycleRun)
            .where(
                CycleRun.status.in_(ACTIVE_RUN_STATUSES),
                CycleRun.scheduled_for <= stale_cutoff,
            )
            .order_by(CycleRun.scheduled_for.asc(), CycleRun.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        active_runs = list((await uow.session.scalars(active_stmt)).all())
        for run in active_runs:
            cycle = await uow.session.get(Cycle, run.cycle_id)
            if run.status == "queued":
                scheduled_for = _aware_utc(run.scheduled_for)
                if scheduled_for is not None and scheduled_for < catchup_cutoff:
                    await _finalize_stale_cycle_run(
                        run,
                        cycle,
                        status="skipped",
                        skip_reason="missed_catchup_window",
                        session=uow.session,
                    )
                elif cycle is None or cycle.deleted_at is not None:
                    await _finalize_stale_cycle_run(
                        run,
                        cycle,
                        status="failed",
                        error="Cycle unavailable before stale queued run recovered",
                        session=uow.session,
                    )
                else:
                    executable_run_ids.append(run.id)
                continue

            agent_run = await uow.session.get(AgentRun, run.run_id) if run.run_id else None
            terminal_status = _agent_run_terminal_cycle_status(agent_run)
            if terminal_status is not None:
                await _finalize_stale_cycle_run(
                    run,
                    cycle,
                    status=terminal_status,
                    error=(
                        "Agent run ended without cycle-run finalization"
                        if terminal_status == "failed"
                        else None
                    ),
                    session=uow.session,
                )

    for run_id in executable_run_ids:
        await async_execute_cycle_run(run_id)

    return executable_run_ids


async def async_schedule_due_cycles_once(*, limit: int = 10) -> list[int]:
    claimed_run_ids: list[int] = []
    now = datetime.now(timezone.utc)

    await async_recover_stale_cycle_runs_once(limit=limit)

    async with UnitOfWork() as uow:
        stmt = (
            select(Cycle)
            .where(
                Cycle.deleted_at.is_(None),
                Cycle.enabled.is_(True),
                Cycle.next_run_at.is_not(None),
                Cycle.next_run_at <= now,
            )
            .order_by(Cycle.next_run_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        cycles = (await uow.session.scalars(stmt)).all()
        for cycle in cycles:
            scheduled_for = cycle.next_run_at or now
            run = CycleRun(
                cycle_id=cycle.id,
                scheduled_for=scheduled_for,
                prompt_snapshot=cycle.prompt,
                status="queued",
            )
            uow.session.add(run)
            await uow.session.flush()
            await _async_prepare_cycle_run_memory_snapshot(uow.session, cycle, run)
            next_run_at = compute_next_run_at(
                cycle.schedule_expr,
                cycle.timezone,
                from_dt=scheduled_for,
            )
            cycle.next_run_at = next_run_at
            if is_one_time_schedule_expr(cycle.schedule_expr) and next_run_at is None:
                cycle.enabled = False
            claimed_run_ids.append(run.id)

    for run_id in claimed_run_ids:
        await async_execute_cycle_run(run_id)

    return claimed_run_ids


async def async_execute_cycle_run(run_id: int) -> None:
    message_payload = None
    status_payload = None
    should_publish_idea = False
    idea_snapshot = None
    idea_id = None
    run_message = None
    run_metadata = None
    cycle_name = ""
    cycle_user_id = None
    agent_run_id = None

    async with UnitOfWork() as uow:
        result = await uow.session.scalars(
            select(CycleRun).where(CycleRun.id == run_id).with_for_update()
        )
        run = result.first()
        if not run or run.status != "queued":
            return
        result = await uow.session.scalars(
            select(Cycle).where(Cycle.id == run.cycle_id).with_for_update()
        )
        cycle = result.first()
        if not cycle or cycle.deleted_at is not None:
            if cycle:
                await _finalize_cycle_run(
                    run,
                    cycle,
                    status="failed",
                    error="Cycle deleted before run started",
                    session=uow.session,
                )
            else:
                run.status = "failed"
                run.error = "Cycle deleted before run started"
                run.completed_at = datetime.now(timezone.utc)
            return

        cycle_name = cycle.name
        cycle_user_id = cycle.user_id
        owner = await uow.session.get(User, cycle.user_id)
        cycle.execution_mode = REUSABLE_THREAD_EXECUTION_MODE
        cycle.reopen_archived = True

        target = await async_resolve_cycle_execution_target(
            uow.session,
            cycle=cycle,
            run=run,
        )
        idea = target.idea
        should_publish_idea = target.should_publish_idea

        run.idea_id = idea.id
        await _async_prepare_cycle_run_memory_snapshot(uow.session, cycle, run)
        append_cycle_run_output_target_snapshot(
            run,
            target_type=THREAD_OUTPUT_TARGET_TYPE,
            target_id=str(idea.id),
            label="Cycle run execution thread",
            config={"ephemeral": target.output_target_ephemeral},
            rationale="Execution output surface for this CycleRun.",
        )
        idea_id = idea.id

        preflight = await async_preflight_cycle_external_auth(uow.session, cycle=cycle)
        if preflight.status != "skipped":
            context_snapshot = dict(run.context_snapshot or {})
            context_snapshot["auth_preflight"] = preflight.to_dict()
            run.context_snapshot = context_snapshot

        if preflight.blocked:
            await _finalize_cycle_run(
                run,
                cycle,
                status="auth_blocked",
                error=preflight.visible_message,
                session=uow.session,
            )
            message_payload, status_payload = await _async_append_cycle_auth_blocked_thread_message(
                uow.session,
                idea,
                cycle,
                run,
                preflight,
            )
            idea_snapshot = serialize_execution_idea(idea)
        else:
            run.started_at = datetime.now(timezone.utc)
            run.status = "running"
            run_metadata = _cycle_run_metadata(cycle, run)
            run_message = _cycle_run_message(idea, cycle, run)
            agent_run_id = await _async_admit_cycle_run(
                uow.session,
                idea_id=idea.id,
                message=run_message,
                priority=1,
                user_id=cycle_user_id,
                metadata=run_metadata,
                cycle_run_id=run.id,
            )
            if agent_run_id is None:
                await _finalize_cycle_run(
                    run,
                    cycle,
                    status="failed",
                    error="Cycle work admission failed before an agent run was created",
                    session=uow.session,
                )
                return
            message_payload, status_payload = await _async_append_cycle_thread_message(
                uow.session,
                idea,
                cycle,
                run,
                owner,
            )
            idea_snapshot = serialize_execution_idea(idea)
            run.run_id = agent_run_id
            cycle.last_status = "running"
            cycle.last_error = None
    if should_publish_idea and idea_snapshot:
        publish("idea_upserted", {"idea": idea_snapshot})
    if message_payload:
        publish("thread_message", {"idea_id": idea_id, "message": message_payload})
    if status_payload:
        publish("status_change", status_payload)
    if agent_run_id is not None:
        logger.info(
            "Enqueued cycle run #%s for idea %s... (cycle=%s)",
            agent_run_id,
            short_identifier(idea_id),
            cycle_name,
        )


async def async_finalize_cycle_run_from_run(
    run_id: int,
    *,
    status: str,
    error: str | None = None,
) -> None:
    if status not in {"completed", "failed"}:
        return
    async with UnitOfWork() as uow:
        agent_run = await uow.session.get(AgentRun, run_id)
        metadata = agent_run.metadata_ if agent_run else None
        if not isinstance(metadata, dict) or metadata.get("source") != "cycle":
            return
        cycle_run_id = metadata.get("cycle_run_id")
        if not cycle_run_id:
            return
        run = await uow.session.get(CycleRun, int(cycle_run_id))
        cycle = await uow.session.get(Cycle, run.cycle_id) if run else None
        if not run or not cycle or run.status in TERMINAL_RUN_STATUSES:
            return
        verdict = persisted_cycle_contract_verdict(run)
        if status == "completed" and verdict is None:
            verdict = await async_prepare_cycle_run_visible_finalization(uow.session, int(run_id))
        elif status == "failed" and verdict is None:
            verdict = await async_prepare_cycle_run_visible_finalization(
                uow.session,
                int(run_id),
                provider_errors_only=True,
            )
        final_status, final_error = cycle_finalization_status_from_verdict(
            status,
            verdict=verdict,
            error=error if status == "failed" else None,
        )
        await _finalize_cycle_run(
            run,
            cycle,
            status=final_status,
            error=final_error,
            session=uow.session,
        )


def finalize_cycle_run_from_run(
    run_id: int,
    *,
    status: str,
    error: str | None = None,
) -> None:
    with asyncio.Runner() as runner:
        runner.run(async_finalize_cycle_run_from_run(run_id, status=status, error=error))
