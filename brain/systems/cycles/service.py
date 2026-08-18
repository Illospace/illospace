"""Cycle CRUD, scheduling, and execution helpers."""
from __future__ import annotations

import logging
import asyncio

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from brain.contracts.statuses import TERMINAL_RUN_STATUS_VALUES
from brain.kernel.common.time import assume_utc_optional
from brain.platform.integrations.provider_auth_preflight import (
    ProviderAuthBlockedPreflightResult,
)
from brain.platform.events import publish
from brain.systems.cortex.thought_lifecycle import ThreadMessageCommand, post_thread_message
from brain.systems.cycles.status import CYCLE_RUN_ACTIVE_STATUSES, CYCLE_RUN_TERMINAL_STATUSES
from brain.systems.cycles.admission import (
    CycleAdmissionAdmitted,
    CycleAdmissionAuthBlocked,
    CycleAdmissionFinalized,
    CycleAdmissionPromotionConfigurationError,
    CycleAdmissionPromotionIdle,
    CycleAdmissionQuotaBlocked,
    CycleAdmissionQuotaDeferred,
    CycleAdmissionRejected,
    CycleProviderRoute,
    async_prepare_cycle_run_admission,
)
from brain.systems.cycles.quota_preflight import (
    async_append_cycle_quota_notice,
)
from brain.systems.cycles.common import (
    MANUAL_CYCLE_ORIGIN,
    ILLO_LANE_EXECUTOR_BINDING,
    PERSONAL_AGENT_EXECUTOR_BINDING,
    MAX_CYCLE_TIMEOUT_SECONDS,
    MIN_CYCLE_TIMEOUT_SECONDS,
    REUSABLE_THREAD_EXECUTION_MODE,
    SCHEDULED_CYCLE_ORIGIN,
    SCHEDULED_DIGEST_RUN_KIND,
    THREAD_OUTPUT_TARGET_TYPE,
    canonical_execution_mode,
    cycle_executor_binding,
    cycle_run_launch_context,
    due_illo_lane_cycle_clause,
    json_dict,
    short_identifier,
    validate_nonempty_trimmed,
    validate_thinking_override,
)
from brain.systems.cycles.contracts import normalize_cycle_run_kind
from brain.systems.cycles.contract_gate import (
    async_prepare_cycle_run_visible_finalization,
    cycle_finalization_status_from_verdict,
)
from brain.systems.cycles.cycle_verdict_ledger import (
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
from brain.platform.db.models.agent_run import AgentRunEventRow
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.org import User
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger("cycles")

ACTIVE_RUN_STATUSES = CYCLE_RUN_ACTIVE_STATUSES
TERMINAL_RUN_STATUSES = CYCLE_RUN_TERMINAL_STATUSES
DEFAULT_STALE_CYCLE_RUN_SECONDS = 60
DEFAULT_CYCLE_RUN_CATCHUP_WINDOW_SECONDS = 24 * 60 * 60
_UWEAR_COORDINATOR_CYCLE_NAME = "Uwear Ticket Coordinator Check-ins"
# User cancellations currently count toward the repeated-failure guard. The open
# alternative is "skipped" with skip_reason="canceled", which the guard ignores.
CANCELED_RUN_CYCLE_DISPOSITION = "failed"
RUN_STATUS_TO_CYCLE_RUN_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "error": "failed",
    "canceled": CANCELED_RUN_CYCLE_DISPOSITION,
    "cancelled": CANCELED_RUN_CYCLE_DISPOSITION,
    "expired": "failed",
    "blocked": "failed",
}

__all__ = [
    "CANCELED_RUN_CYCLE_DISPOSITION",
    "REUSABLE_THREAD_EXECUTION_MODE",
    "async_add_cycle_guidance",
    "async_add_cycle_output_target",
    "async_claim_cycle_run",
    "async_execute_cycle_run",
    "async_finalize_cycle_run_from_run",
    "async_recover_stale_cycle_runs_once",
    "async_record_cycle_revision",
    "async_advance_cycle_schedule_past_gap",
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
    route: CycleProviderRoute,
    metadata: dict | None,
    cycle_run_id: int,
    deadline_at: datetime | None = None,
) -> int | None:
    result = await admit_work(
        session,
        WorkIntakeEvent(
            source="cycle",
            event_type="cycle.due_run",
            org_id=str((metadata or {}).get("org_id") or ""),
            actor={"id": route.user_id, "org_id": (metadata or {}).get("org_id")},
            target={"kind": "cortex_idea", "idea_id": idea_id},
            payload={
                "message": message,
                "metadata": dict(metadata or {}),
                "model_policy": route.work_intake_model_policy,
                **({"deadline_at": deadline_at} if deadline_at is not None else {}),
            },
            policy={
                "priority": priority,
                "producer": "cycle",
                "idempotency_key": f"cycle_run:{cycle_run_id}",
                "run_event": "thread_reply",
            },
        ),
    )
    return result.run_id if result.ok else None


def _cycle_run_deadline_at(
    cycle: Cycle,
    *,
    now: datetime | None = None,
) -> datetime | None:
    raw_timeout = cycle.timeout_seconds
    if raw_timeout is None:
        return None
    timeout_seconds = min(
        max(int(raw_timeout), MIN_CYCLE_TIMEOUT_SECONDS),
        MAX_CYCLE_TIMEOUT_SECONDS,
    )
    if timeout_seconds != raw_timeout:
        logger.warning(
            "Clamping out-of-range Cycle timeout_seconds at run admission",
            extra={
                "cycle_id": cycle.id,
                "stored_timeout_seconds": raw_timeout,
                "effective_timeout_seconds": timeout_seconds,
            },
        )
    # This operational policy is read live from the Cycle row at admission.
    # CycleRevision deliberately snapshots mission content, not run deadlines.
    admitted_at = now or datetime.now(timezone.utc)
    return admitted_at + timedelta(seconds=timeout_seconds)


async def _async_attach_open_ask_stragglers(
    session,
    cycle: Cycle,
    run: CycleRun,
) -> list[dict]:
    """Put overdue human asks directly into the next coordinator digest."""

    launch_context = cycle_run_launch_context(run)
    if (
        cycle.name != _UWEAR_COORDINATOR_CYCLE_NAME
        or launch_context.get("origin") != SCHEDULED_CYCLE_ORIGIN
        or launch_context.get("run_kind") != SCHEDULED_DIGEST_RUN_KIND
    ):
        return []
    from brain.systems.runs.open_ask_digest import list_open_ask_stragglers
    from brain.systems.runs.run_deferrals import expire_stale_run_deferrals

    try:
        async with session.begin_nested():
            await expire_stale_run_deferrals(
                session,
                org_id=str(cycle.org_id),
                now=run.scheduled_for,
            )
        stragglers = await list_open_ask_stragglers(
            session,
            org_id=str(cycle.org_id),
            now=run.scheduled_for,
        )
    except Exception as exc:  # noqa: BLE001 - digest still launches with a loud evidence gap
        logger.exception("coordinator open-ask ledger read failed safely")
        context_snapshot = dict(run.context_snapshot or {})
        context_snapshot["open_ask_ledger_error"] = str(exc)
        run.context_snapshot = context_snapshot
        return []
    context_snapshot = dict(run.context_snapshot or {})
    context_snapshot["open_ask_stragglers"] = stragglers
    run.context_snapshot = context_snapshot
    return stragglers


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
    preflight: ProviderAuthBlockedPreflightResult,
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


def _cycle_admission_notice_error(notice):
    return notice.visible_message


def _cycle_admission_without_error(_notice):
    return None


def _cycle_admission_notice_appender(notice_kind):
    return {
        "auth": _async_append_cycle_auth_blocked_thread_message,
        "quota": async_append_cycle_quota_notice,
    }[notice_kind]


_CYCLE_ADMISSION_REJECTION_SETTLEMENTS = {
    CycleAdmissionAuthBlocked: (
        "auth_blocked",
        None,
        _cycle_admission_notice_error,
        "auth",
    ),
    CycleAdmissionQuotaBlocked: (
        "quota_blocked",
        None,
        _cycle_admission_notice_error,
        "quota",
    ),
    CycleAdmissionQuotaDeferred: (
        "skipped",
        "quota_soft_limit",
        _cycle_admission_without_error,
        "quota",
    ),
}

_CYCLE_ADMISSION_FINALIZATION_SETTLEMENTS = {
    CycleAdmissionPromotionIdle: (
        "skipped",
        None,
        "promotion_readiness_idle",
    ),
    CycleAdmissionPromotionConfigurationError: (
        "failed",
        "promotion_readiness_policy_configuration_error",
        None,
    ),
}


async def async_run_cycle_now(
    cycle_id: int,
    *,
    run_kind: str,
    launch_context: dict | None = None,
) -> dict:
    scheduled_for = datetime.now(timezone.utc)
    clean_run_kind = normalize_cycle_run_kind(run_kind)
    launch = {
        "origin": MANUAL_CYCLE_ORIGIN,
        "source": "cycle.run_now",
        **json_dict(launch_context),
        "run_kind": clean_run_kind,
    }
    async with UnitOfWork() as uow:
        cycle = await uow.session.get(Cycle, cycle_id)
        if not cycle or cycle.deleted_at is not None:
            raise ValueError("Cycle not found")
        run = CycleRun(
            cycle_id=cycle_id,
            scheduled_for=scheduled_for,
            prompt_snapshot=cycle.prompt,
            status="queued",
            context_snapshot={"launch_context": launch},
        )
        uow.session.add(run)
        await uow.session.flush()
        run_id = run.id

    await async_execute_cycle_run(run_id)

    async with UnitOfWork() as uow:
        run = await uow.session.get(CycleRun, run_id)
    return serialize_cycle_run(run)


def _agent_run_terminal_cycle_status(agent_run: AgentRun | None) -> str | None:
    if agent_run is None:
        return None
    return RUN_STATUS_TO_CYCLE_RUN_STATUS.get(str(agent_run.status or "").strip().lower())


def _cycle_max_concurrency(cycle: Cycle) -> int:
    return max(int(cycle.max_concurrency or 1), 1)


async def _async_active_cycle_run_count(
    session,
    cycle_id: int,
    *,
    excluding_run_id: int | None = None,
) -> int:
    filters = [
        CycleRun.cycle_id == cycle_id,
        CycleRun.status.in_(CYCLE_RUN_ACTIVE_STATUSES),
    ]
    if excluding_run_id is not None:
        filters.append(CycleRun.id != excluding_run_id)
    return int(
        await session.scalar(
            select(func.count())
            .select_from(CycleRun)
            .where(*filters)
        )
        or 0
    )


def _record_capacity_disposition(
    run: CycleRun,
    *,
    active_run_count: int,
    max_concurrency: int,
) -> None:
    context_snapshot = dict(run.context_snapshot or {})
    context_snapshot["disposition"] = {
        "reason": "previous_run_active",
        "active_run_count": active_run_count,
        "max_concurrency": max_concurrency,
    }
    run.context_snapshot = context_snapshot


def _personal_cycle_run_is_scheduler_owned(cycle: Cycle, run: CycleRun) -> bool:
    return (
        cycle_executor_binding(cycle) == PERSONAL_AGENT_EXECUTOR_BINDING
        and cycle_run_launch_context(run).get("origin") == SCHEDULED_CYCLE_ORIGIN
    )


async def async_claim_cycle_run(session, run_id: int) -> tuple[CycleRun, Cycle] | None:
    """Atomically claim one queued run under its owning Cycle's capacity lock."""
    result = await session.scalars(
        select(CycleRun).where(CycleRun.id == run_id)
    )
    candidate = result.first()
    if not candidate or candidate.status != "queued":
        return None

    # Both locking reads need populate_existing, for the same reason the wake
    # primitive does: the unlocked read above cached this run in the identity
    # map, and the ORM would hand that copy back rather than the row it just
    # locked. The Cycle lock guarantees a second executor arrives only after the
    # first committed — so without the refresh it reads "queued", claims a run
    # that is already running, and executes it twice.
    result = await session.scalars(
        select(Cycle)
        .where(Cycle.id == candidate.cycle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    cycle = result.first()
    result = await session.scalars(
        select(CycleRun)
        .where(
            CycleRun.id == run_id,
            CycleRun.cycle_id == candidate.cycle_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    run = result.first()
    if not run or run.status != "queued":
        return None

    if not cycle or cycle.deleted_at is not None:
        if cycle:
            await _finalize_cycle_run(
                run,
                cycle,
                status="failed",
                error="Cycle deleted before run started",
                session=session,
            )
        else:
            run.status = "failed"
            run.error = "Cycle deleted before run started"
            run.completed_at = datetime.now(timezone.utc)
        await session.flush()
        return None

    if _personal_cycle_run_is_scheduler_owned(cycle, run):
        await _finalize_cycle_run(
            run,
            cycle,
            status="skipped",
            skip_reason="personal_agent_executor",
            session=session,
        )
        await session.flush()
        return None

    active_run_count = await _async_active_cycle_run_count(
        session,
        cycle.id,
        excluding_run_id=run.id,
    )
    max_concurrency = _cycle_max_concurrency(cycle)
    if active_run_count >= max_concurrency:
        _record_capacity_disposition(
            run,
            active_run_count=active_run_count,
            max_concurrency=max_concurrency,
        )
        await _finalize_cycle_run(
            run,
            cycle,
            status="skipped",
            skip_reason="previous_run_active",
            session=session,
        )
        await session.flush()
        return None

    run.status = "running"
    await session.flush()
    return run, cycle


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
                scheduled_for = assume_utc_optional(run.scheduled_for)
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
                elif _personal_cycle_run_is_scheduler_owned(cycle, run):
                    await _finalize_stale_cycle_run(
                        run,
                        cycle,
                        status="skipped",
                        skip_reason="personal_agent_executor",
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


def _advance_cycle_schedule(
    cycle: Cycle,
    *,
    from_dt: datetime,
) -> datetime | None:
    """Move one Cycle onto its next schedule slot."""

    scheduled_for = assume_utc_optional(from_dt)
    if scheduled_for is None:
        raise ValueError("from_dt is required")
    next_run_at = assume_utc_optional(
        compute_next_run_at(
            cycle.schedule_expr,
            cycle.timezone,
            from_dt=scheduled_for,
        )
    )
    if next_run_at is not None and next_run_at <= scheduled_for:
        raise RuntimeError("schedule did not advance")
    cycle.next_run_at = next_run_at
    if is_one_time_schedule_expr(cycle.schedule_expr) and next_run_at is None:
        cycle.enabled = False
    return next_run_at


async def _async_materialize_due_cycle_runs_once(
    *,
    limit: int,
    now: datetime,
) -> list[int]:
    executable_run_ids: list[int] = []
    async with UnitOfWork() as uow:
        stmt = (
            select(Cycle)
            .where(due_illo_lane_cycle_clause(now, inclusive=True))
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
                context_snapshot={
                    "launch_context": {
                        "origin": SCHEDULED_CYCLE_ORIGIN,
                        "source": "cycle_scheduler",
                        "run_kind": SCHEDULED_DIGEST_RUN_KIND,
                    }
                },
            )
            uow.session.add(run)
            await uow.session.flush()
            active_run_count = await _async_active_cycle_run_count(
                uow.session,
                cycle.id,
                excluding_run_id=run.id,
            )
            max_concurrency = _cycle_max_concurrency(cycle)
            if active_run_count >= max_concurrency:
                _record_capacity_disposition(
                    run,
                    active_run_count=active_run_count,
                    max_concurrency=max_concurrency,
                )
                await _finalize_cycle_run(
                    run,
                    cycle,
                    status="skipped",
                    skip_reason="previous_run_active",
                    session=uow.session,
                )
            else:
                await _async_prepare_cycle_run_memory_snapshot(
                    uow.session,
                    cycle,
                    run,
                )
                executable_run_ids.append(run.id)
            _advance_cycle_schedule(cycle, from_dt=scheduled_for)

    return executable_run_ids


async def async_advance_cycle_schedule_past_gap(
    session,
    *,
    gap_start: datetime,
    now: datetime,
    max_slots_per_cycle: int = 1000,
) -> dict[str, object]:
    """Advance Cycle schedules past a cold-start gap without replaying slots.

    The cold-start owner supplies the one authoritative gap. This Cycle-owned
    operation lists the suppressed slots for the catch-up notice, then
    advances ``next_run_at`` beyond the gap without creating ``CycleRun`` rows.
    """

    gap_start = assume_utc_optional(gap_start)
    now = assume_utc_optional(now)
    if gap_start is None or now is None:
        raise ValueError("gap_start and now are required")
    if gap_start > now:
        raise ValueError("gap_start must not be after now")
    # Cron schedules are minute-granular. A slot in the current minute is the
    # normal slot cadence should resume with, not a missed digest to suppress.
    catch_up_before = now.replace(second=0, microsecond=0)

    cycles = (
        await session.scalars(
            select(Cycle)
            .where(due_illo_lane_cycle_clause(catch_up_before, inclusive=False))
            .order_by(Cycle.next_run_at.asc(), Cycle.id.asc())
            .with_for_update()
        )
    ).all()
    missed_slots: list[dict[str, object]] = []
    advanced = 0
    errors: list[str] = []
    limit = max(1, int(max_slots_per_cycle))

    for cycle in cycles:
        scheduled_for = assume_utc_optional(cycle.next_run_at)
        original_next_run_at = cycle.next_run_at
        original_enabled = cycle.enabled
        seen = 0
        try:
            while scheduled_for is not None and scheduled_for < catch_up_before:
                if seen >= limit:
                    raise RuntimeError(
                        f"more than {limit} overdue slots; refusing an unbounded advance"
                    )
                if scheduled_for >= gap_start:
                    missed_slots.append(
                        {
                            "cycle_id": cycle.id,
                            "cycle_name": cycle.name,
                            "scheduled_for": scheduled_for.isoformat(),
                            "timezone": cycle.timezone,
                        }
                    )
                seen += 1
                scheduled_for = _advance_cycle_schedule(
                    cycle,
                    from_dt=scheduled_for,
                )
        except Exception as exc:  # noqa: BLE001 - isolate one malformed Cycle
            cycle.next_run_at = original_next_run_at
            cycle.enabled = original_enabled
            errors.append(f"cycle:{cycle.id}:{exc}")
            continue

        advanced += 1

    await session.flush()
    return {
        "cycles_examined": len(cycles),
        "cycles_advanced": advanced,
        "missed_slots": missed_slots,
        "missed_slot_count": len(missed_slots),
        "errors": errors,
    }


async def async_wake_cycle_now(*, name: str, org_id: str | None = None) -> str:
    """Pull an enabled cycle's next run forward to now, without flooding.

    Event sources (e.g. the staging-promotion detector) use this instead of a
    native event->cycle binding: the cycle scheduler claims the due slot on its
    next tick, fires exactly one run, and recomputes ``next_run_at`` onto the
    future cron grid. A wake therefore never duplicates a scheduled slot — the
    recompute lands on the next genuine cron boundary, which later fires as
    the normal backstop run (cycles are contracted to exit cheaply when their
    inputs are unchanged). Returns a disposition string rather than raising so
    callers can report the outcome without owning cycle semantics: ``woken`` |
    ``already_pending`` | ``run_in_flight`` | ``not_found`` | ``ambiguous``.
    Cycle names are not globally unique; pass ``org_id`` to scope the lookup,
    and an ambiguous match refuses to wake anything rather than guessing.
    """
    async with UnitOfWork() as uow:
        filters = [
            Cycle.name == name,
            Cycle.enabled.is_(True),
            Cycle.deleted_at.is_(None),
            Cycle.executor_binding == ILLO_LANE_EXECUTOR_BINDING,
        ]
        if org_id is not None:
            filters.append(Cycle.org_id == org_id)
        matches = (
            await uow.session.scalars(
                select(Cycle).where(*filters).order_by(Cycle.id.asc()).limit(2)
            )
        ).all()
        if not matches:
            return "not_found"
        if len(matches) > 1:
            logger.warning("Cycle wake for %r is ambiguous; refusing to wake", name)
            return "ambiguous"
        # Re-apply the selector under the lock, not just the id: the caller
        # asked for a name, and the row can be renamed, disabled, deleted or
        # moved between orgs while this wake waits its turn. Postgres
        # re-evaluates the predicate against the row it waited for, so a row
        # that stopped matching drops out here instead of being woken anyway.
        # populate_existing is what makes the lock mean anything at all: the
        # lookup above already put this row in the identity map, and without it
        # the ORM hands back that pre-lock copy, so every guard below would be
        # decided on state a competing writer has since replaced.
        cycle = (
            await uow.session.scalars(
                select(Cycle)
                .where(Cycle.id == matches[0].id, *filters)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).first()
        if cycle is None:
            return "not_found"
        # Read the clock after the lock, not before: a caller that waited out a
        # competing wake must judge the slot as of when it holds the row, or it
        # re-stamps a slot that is already due.
        now = datetime.now(timezone.utc)
        pending_at = assume_utc_optional(cycle.next_run_at)
        if pending_at is not None and pending_at <= now:
            return "already_pending"
        active_run_count = await _async_active_cycle_run_count(uow.session, cycle.id)
        if active_run_count > 0:
            return "run_in_flight"
        cycle.next_run_at = now
        logger.info("Cycle %s (%s) woken by event source", cycle.id, cycle.name)
    return "woken"


async def async_schedule_due_cycles_once(*, limit: int = 10) -> list[int]:
    now = datetime.now(timezone.utc)
    await async_recover_stale_cycle_runs_once(limit=limit)
    executable_run_ids = await _async_materialize_due_cycle_runs_once(
        limit=limit,
        now=now,
    )

    for run_id in executable_run_ids:
        await async_execute_cycle_run(run_id)

    return executable_run_ids


async def _async_settle_rejected_cycle_run(
    session,
    *,
    admission: CycleAdmissionRejected,
    idea: Idea,
    cycle: Cycle,
    run: CycleRun,
) -> tuple[dict | None, dict | None, dict]:
    status, skip_reason, notice_error, notice_kind = (
        _CYCLE_ADMISSION_REJECTION_SETTLEMENTS[type(admission)]
    )
    await _finalize_cycle_run(
        run,
        cycle,
        status=status,
        error=notice_error(admission.notice),
        skip_reason=skip_reason,
        session=session,
    )
    append_notice = _cycle_admission_notice_appender(notice_kind)
    message_payload, status_payload = await append_notice(
        session,
        idea,
        cycle,
        run,
        admission.notice,
    )
    return message_payload, status_payload, serialize_execution_idea(idea)


async def _async_start_admitted_cycle_run(
    session,
    *,
    admission: CycleAdmissionAdmitted,
    idea: Idea,
    cycle: Cycle,
    run: CycleRun,
    owner: User | None,
) -> tuple[int, dict, dict | None, dict] | None:
    run.started_at = datetime.now(timezone.utc)
    run_metadata = _cycle_run_metadata(cycle, run)
    run_metadata.update(admission.metadata_patch)
    run_message = _cycle_run_message(idea, cycle, run)
    agent_run_id = await _async_admit_cycle_run(
        session,
        idea_id=idea.id,
        message=run_message,
        priority=1,
        route=admission.route,
        metadata=run_metadata,
        cycle_run_id=run.id,
        deadline_at=_cycle_run_deadline_at(cycle),
    )
    if agent_run_id is None:
        await _finalize_cycle_run(
            run,
            cycle,
            status="failed",
            error="Cycle work admission failed before an agent run was created",
            session=session,
        )
        return None

    message_payload, status_payload = await _async_append_cycle_thread_message(
        session,
        idea,
        cycle,
        run,
        owner,
    )
    idea_snapshot = serialize_execution_idea(idea)
    run.run_id = agent_run_id
    cycle.last_status = "running"
    cycle.last_error = None
    return agent_run_id, message_payload, status_payload, idea_snapshot


async def async_execute_cycle_run(run_id: int) -> None:
    message_payload = None
    status_payload = None
    should_publish_idea = False
    idea_snapshot = None
    idea_id = None
    cycle_name = ""
    agent_run_id = None

    async with UnitOfWork() as uow:
        claim = await async_claim_cycle_run(uow.session, run_id)
        if claim is None:
            return
        run, cycle = claim

        cycle_name = cycle.name
        owner = await uow.session.get(User, cycle.user_id)
        cycle.execution_mode = REUSABLE_THREAD_EXECUTION_MODE
        cycle.reopen_archived = True

        from brain.systems.tracker_maintenance import (
            maybe_run_tracker_maintenance,
        )

        await maybe_run_tracker_maintenance(
            uow.session,
            cycle=cycle,
            run=run,
        )

        target = await async_resolve_cycle_execution_target(
            uow.session,
            cycle=cycle,
            run=run,
        )
        idea = target.idea
        should_publish_idea = target.should_publish_idea

        run.idea_id = idea.id
        await _async_prepare_cycle_run_memory_snapshot(uow.session, cycle, run)
        await _async_attach_open_ask_stragglers(uow.session, cycle, run)
        append_cycle_run_output_target_snapshot(
            run,
            target_type=THREAD_OUTPUT_TARGET_TYPE,
            target_id=str(idea.id),
            label="Cycle run execution thread",
            config={"ephemeral": target.output_target_ephemeral},
            rationale="Execution output surface for this CycleRun.",
        )
        idea_id = idea.id

        admission = await async_prepare_cycle_run_admission(
            uow.session,
            cycle=cycle,
            run=run,
        )
        if isinstance(admission, CycleAdmissionFinalized):
            status, error, skip_reason = (
                _CYCLE_ADMISSION_FINALIZATION_SETTLEMENTS[type(admission)]
            )
            if isinstance(admission, CycleAdmissionPromotionConfigurationError):
                policy_snapshot = json_dict(run.context_snapshot).get(
                    "execution_policy"
                )
                if isinstance(policy_snapshot, dict):
                    error = str(policy_snapshot.get("error") or "").strip() or error
            await _finalize_cycle_run(
                run,
                cycle,
                status=status,
                error=error,
                skip_reason=skip_reason,
                session=uow.session,
            )
            return
        if isinstance(admission, CycleAdmissionRejected):
            message_payload, status_payload, idea_snapshot = (
                await _async_settle_rejected_cycle_run(
                    uow.session,
                    admission=admission,
                    idea=idea,
                    cycle=cycle,
                    run=run,
                )
            )
        else:
            started = await _async_start_admitted_cycle_run(
                uow.session,
                admission=admission,
                idea=idea,
                cycle=cycle,
                run=run,
                owner=owner,
            )
            if started is None:
                return
            agent_run_id, message_payload, status_payload, idea_snapshot = started
    for should_publish, event, payload in (
        (
            should_publish_idea and idea_snapshot,
            "idea_upserted",
            {"idea": idea_snapshot},
        ),
        (
            message_payload,
            "thread_message",
            {"idea_id": idea_id, "message": message_payload},
        ),
        (status_payload, "status_change", status_payload),
    ):
        if should_publish:
            publish(event, payload)
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
    if status not in TERMINAL_RUN_STATUS_VALUES:
        return
    effective_status = RUN_STATUS_TO_CYCLE_RUN_STATUS[status]
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
        if status == "canceled":
            if effective_status == "failed" and not str(error or "").strip():
                error = "Agent run was canceled"
            await _finalize_cycle_run(
                run,
                cycle,
                status=effective_status,
                error=error if effective_status == "failed" else None,
                skip_reason="canceled" if effective_status == "skipped" else None,
                session=uow.session,
            )
            return
        verdict = persisted_cycle_contract_verdict(run)
        if effective_status == "completed" and verdict is None:
            verdict = await async_prepare_cycle_run_visible_finalization(uow.session, int(run_id))
        elif effective_status == "failed" and verdict is None:
            verdict = await async_prepare_cycle_run_visible_finalization(
                uow.session,
                int(run_id),
                provider_errors_only=True,
            )
        if effective_status == "failed" and not str(error or "").strip():
            failure_event = (
                await uow.session.scalars(
                    select(AgentRunEventRow)
                    .where(
                        AgentRunEventRow.run_id == int(run_id),
                        AgentRunEventRow.event_type.in_(("run.failed", "run.status_changed")),
                    )
                    .order_by(
                        (AgentRunEventRow.event_type == "run.failed").desc(),
                        AgentRunEventRow.sequence_no.desc(),
                        AgentRunEventRow.id.desc(),
                    )
                    .limit(1)
                )
            ).first()
            failure_payload = dict(getattr(failure_event, "payload", None) or {})
            error = str(
                failure_payload.get("error")
                or failure_payload.get("reason")
                or ""
            ).strip() or None
        final_status, final_error = cycle_finalization_status_from_verdict(
            effective_status,
            verdict=verdict,
            error=error if effective_status == "failed" else None,
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
