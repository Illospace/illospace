"""Cycle CRUD, scheduling, and execution helpers."""
from __future__ import annotations

import logging
import asyncio

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import and_, func, or_, select

from brain.systems.cortex.events import publish
from brain.systems.cortex.thought_lifecycle import (
    ThoughtStatusCommand,
    ThreadMessageCommand,
    post_thread_message,
    transition_thought_status,
)
from brain.systems.cycles.status import CYCLE_RUN_ACTIVE_STATUSES, CYCLE_RUN_TERMINAL_STATUSES
from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work
from brain.platform.db.models.cycle import (
    Cycle,
    CycleGuidance,
    CycleOutputTarget,
    CycleRevision,
    CycleRun,
    CycleRunEvaluation,
)
from brain.platform.db.models.run import AgentRun
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.org import User
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger("cycles")

REUSABLE_THREAD_EXECUTION_MODE = "reuse_same_idea"
VALID_EXECUTION_MODES = {"new_idea_per_run", REUSABLE_THREAD_EXECUTION_MODE}
VALID_THINKING_OVERRIDES = {"none", "low", "medium", "high", "xhigh"}
ACTIVE_RUN_STATUSES = CYCLE_RUN_ACTIVE_STATUSES
TERMINAL_RUN_STATUSES = CYCLE_RUN_TERMINAL_STATUSES
ONE_TIME_SCHEDULE_PREFIX = "at:"
CYCLE_LAUNCH_ENVELOPE_VERSION = 1
DEFAULT_STALE_CYCLE_RUN_SECONDS = 60
DEFAULT_CYCLE_RUN_CATCHUP_WINDOW_SECONDS = 24 * 60 * 60
CYCLE_LEDGER_OUTPUT_TARGET_TYPE = "cycle_ledger"
THREAD_OUTPUT_TARGET_TYPE = "thread"
RUN_STATUS_TO_CYCLE_RUN_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "error": "failed",
    "canceled": "failed",
    "cancelled": "failed",
    "expired": "failed",
    "blocked": "failed",
}


def validate_nonempty_trimmed(value: str, field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def is_one_time_schedule_expr(expr: str | None) -> bool:
    return str(expr or "").strip().lower().startswith(ONE_TIME_SCHEDULE_PREFIX)


def _parse_one_time_run_at(expr: str, timezone_name: str) -> datetime:
    value = str(expr or "").strip()
    if not is_one_time_schedule_expr(value):
        raise ValueError("schedule_expr must start with at:")
    raw_at = value[len(ONE_TIME_SCHEDULE_PREFIX):].strip()
    if not raw_at:
        raise ValueError("one-time schedule requires an ISO timestamp")
    try:
        run_at = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("one-time schedule requires an ISO timestamp") from exc
    tz = ZoneInfo(validate_timezone_name(timezone_name))
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=tz)
    return run_at.astimezone(timezone.utc)


def build_one_time_schedule_expr(run_at: str | datetime, timezone_name: str) -> str:
    tz = ZoneInfo(validate_timezone_name(timezone_name))
    if isinstance(run_at, datetime):
        parsed = run_at
    else:
        raw_at = str(run_at or "").strip()
        if not raw_at:
            raise ValueError("run_at is required")
        try:
            parsed = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("run_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    local_run_at = parsed.astimezone(tz).replace(second=0, microsecond=0)
    return f"{ONE_TIME_SCHEDULE_PREFIX}{local_run_at.isoformat()}"


def validate_schedule_expr(expr: str, timezone_name: str | None = None) -> str:
    value = (expr or "").strip()
    if is_one_time_schedule_expr(value):
        tz_name = validate_timezone_name(timezone_name or "UTC")
        return build_one_time_schedule_expr(
            _parse_one_time_run_at(value, tz_name),
            tz_name,
        )
    if len(value.split()) != 5 or not croniter.is_valid(value):
        raise ValueError("schedule_expr must be a valid 5-field cron expression")
    return value


def validate_timezone_name(name: str) -> str:
    value = (name or "").strip()
    if not value:
        raise ValueError("timezone is required")
    try:
        ZoneInfo(value)
    except Exception as exc:
        raise ValueError(f"Unknown timezone: {value}") from exc
    return value


def validate_execution_mode(mode: str | None) -> str:
    value = (mode or REUSABLE_THREAD_EXECUTION_MODE).strip()
    if value not in VALID_EXECUTION_MODES:
        raise ValueError(
            f"execution_mode must be one of: {', '.join(sorted(VALID_EXECUTION_MODES))}"
        )
    return REUSABLE_THREAD_EXECUTION_MODE


def validate_thinking_override(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized not in VALID_THINKING_OVERRIDES:
        raise ValueError(
            f"thinking_override must be one of: {', '.join(sorted(VALID_THINKING_OVERRIDES))}"
        )
    return normalized


def compute_next_run_at(
    schedule_expr: str,
    timezone_name: str,
    *,
    from_dt: datetime | None = None,
) -> datetime | None:
    if is_one_time_schedule_expr(schedule_expr):
        run_at = _parse_one_time_run_at(schedule_expr, timezone_name)
        return None if from_dt is not None and run_at <= from_dt else run_at

    tz = ZoneInfo(validate_timezone_name(timezone_name))
    baseline = from_dt or datetime.now(timezone.utc)
    local_baseline = baseline.astimezone(tz)
    iterator = croniter(validate_schedule_expr(schedule_expr), local_baseline)
    next_local = iterator.get_next(datetime)
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)
    return next_local.astimezone(timezone.utc)


def humanize_schedule(schedule_expr: str, timezone_name: str) -> str:
    if is_one_time_schedule_expr(schedule_expr):
        tz_name = validate_timezone_name(timezone_name)
        local_run_at = _parse_one_time_run_at(schedule_expr, tz_name).astimezone(ZoneInfo(tz_name))
        hour = local_run_at.strftime("%I").lstrip("0") or "0"
        return (
            f"Once at {local_run_at.strftime('%b')} {local_run_at.day}, "
            f"{local_run_at.year} {hour}:{local_run_at.strftime('%M %p')} ({tz_name})"
        )

    expr = validate_schedule_expr(schedule_expr)
    minute, hour, dom, month, dow = expr.split()
    tz = validate_timezone_name(timezone_name)

    if minute == "*" and hour == "*":
        return f"Every minute ({tz})"
    if minute == "0" and hour == "*":
        return f"Every hour ({tz})"
    if minute.isdigit() and hour.isdigit() and dom == "*" and month == "*" and dow == "*":
        dt = datetime(2000, 1, 1, int(hour), int(minute))
        return f"Every day at {dt.strftime('%-I:%M %p')} ({tz})"
    weekday_names = {
        "0": "Sundays",
        "1": "Mondays",
        "2": "Tuesdays",
        "3": "Wednesdays",
        "4": "Thursdays",
        "5": "Fridays",
        "6": "Saturdays",
        "7": "Sundays",
    }
    if minute.isdigit() and hour.isdigit() and dom == "*" and month == "*" and dow in weekday_names:
        dt = datetime(2000, 1, 1, int(hour), int(minute))
        return f"{weekday_names[dow]} at {dt.strftime('%-I:%M %p')} ({tz})"
    if (
        minute.isdigit()
        and hour.isdigit()
        and dom == "*"
        and month == "*"
        and dow in {"1", "2", "3", "4", "5"}
    ):
        dt = datetime(2000, 1, 1, int(hour), int(minute))
        return f"Weekdays at {dt.strftime('%-I:%M %p')} ({tz})"
    return f"{expr} ({tz})"


def _safe_humanize_schedule(schedule_expr: str, timezone_name: str) -> str:
    try:
        return humanize_schedule(schedule_expr, timezone_name)
    except ValueError:
        return f"{schedule_expr} ({timezone_name})"


def cycle_defaults(*, execution_mode: str, reopen_archived: bool | None) -> bool:
    return True


def _string_or_none(value) -> str | None:
    if value is None:
        return None
    return str(value)


def _short_identifier(value, *, length: int = 8) -> str:
    text = _string_or_none(value) or ""
    return text[:length]


def _required_datetime(*values) -> datetime:
    for value in values:
        if value is not None:
            return value
    return datetime.now(timezone.utc)


def _json_list(value) -> list:
    return value if isinstance(value, list) else []


def _json_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _actor_type(value: str | None) -> str:
    return (value or "system").strip() or "system"


def _actor_id(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _creator_payload(cycle: Cycle) -> dict:
    creator_type = getattr(cycle, "creator_type", None) or "user"
    creator_id = getattr(cycle, "creator_id", None) or _string_or_none(cycle.user_id)
    maintainer_type = getattr(cycle, "maintainer_type", None) or creator_type
    maintainer_id = getattr(cycle, "maintainer_id", None) or creator_id
    return {
        "creator_type": creator_type,
        "creator_id": _string_or_none(creator_id),
        "maintainer_type": maintainer_type,
        "maintainer_id": _string_or_none(maintainer_id),
    }


def serialize_cycle_revision(revision: CycleRevision | None) -> dict | None:
    if revision is None:
        return None
    return {
        "id": revision.id,
        "cycle_id": revision.cycle_id,
        "revision_number": revision.revision_number,
        "source_type": revision.source_type,
        "source_id": _string_or_none(revision.source_id),
        "rationale": revision.rationale,
        "name": revision.name,
        "prompt": revision.prompt,
        "schedule_expr": revision.schedule_expr,
        "timezone": revision.timezone,
        "enabled": revision.enabled,
        "model_override": revision.model_override,
        "thinking_override": revision.thinking_override,
        "target_idea_id": _string_or_none(revision.target_idea_id),
        "context_policy": _json_dict(revision.context_policy),
        "created_at": _required_datetime(revision.created_at),
    }


def serialize_cycle_guidance(guidance: CycleGuidance) -> dict:
    return {
        "id": guidance.id,
        "cycle_id": guidance.cycle_id,
        "revision_id": guidance.revision_id,
        "source_type": guidance.source_type,
        "source_id": _string_or_none(guidance.source_id),
        "guidance": guidance.guidance,
        "rationale": guidance.rationale,
        "is_active": guidance.is_active,
        "created_at": _required_datetime(guidance.created_at),
    }


def serialize_cycle_output_target(target: CycleOutputTarget) -> dict:
    return {
        "id": target.id,
        "cycle_id": target.cycle_id,
        "revision_id": target.revision_id,
        "target_type": target.target_type,
        "target_id": _string_or_none(target.target_id),
        "label": target.label,
        "config": _json_dict(target.config),
        "source_type": target.source_type,
        "source_id": _string_or_none(target.source_id),
        "rationale": target.rationale,
        "is_active": target.is_active,
        "created_at": _required_datetime(target.created_at, target.updated_at),
        "updated_at": _required_datetime(target.updated_at, target.created_at),
    }


def serialize_cycle_run_evaluation(evaluation: CycleRunEvaluation) -> dict:
    return {
        "id": evaluation.id,
        "cycle_id": evaluation.cycle_id,
        "cycle_run_id": evaluation.cycle_run_id,
        "evaluator_type": evaluation.evaluator_type,
        "evaluator_id": _string_or_none(evaluation.evaluator_id),
        "summary": evaluation.summary,
        "score": evaluation.score,
        "details": _json_dict(evaluation.details),
        "created_at": _required_datetime(evaluation.created_at),
    }


def serialize_cycle(cycle: Cycle) -> dict:
    created_at = _required_datetime(cycle.created_at, cycle.updated_at)
    updated_at = _required_datetime(cycle.updated_at, created_at)
    creator = _creator_payload(cycle)
    return {
        "id": cycle.id,
        "user_id": str(cycle.user_id),
        "org_id": _string_or_none(cycle.org_id),
        "workspace_id": _string_or_none(cycle.org_id),
        **creator,
        "name": cycle.name,
        "prompt": cycle.prompt,
        "schedule_expr": cycle.schedule_expr,
        "schedule_human": _safe_humanize_schedule(cycle.schedule_expr, cycle.timezone),
        "timezone": cycle.timezone,
        "enabled": cycle.enabled,
        "model_override": cycle.model_override,
        "thinking_override": cycle.thinking_override,
        "execution_mode": REUSABLE_THREAD_EXECUTION_MODE,
        "target_idea_id": _string_or_none(cycle.target_idea_id),
        "reopen_archived": True,
        "next_run_at": cycle.next_run_at,
        "last_run_at": cycle.last_run_at,
        "last_status": cycle.last_status,
        "last_error": cycle.last_error,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def serialize_cycle_run(run: CycleRun) -> dict:
    return {
        "id": run.id,
        "cycle_id": run.cycle_id,
        "revision_id": getattr(run, "revision_id", None),
        "scheduled_for": run.scheduled_for,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "status": run.status,
        "error": run.error,
        "skip_reason": run.skip_reason,
        "idea_id": _string_or_none(run.idea_id),
        "run_id": run.run_id,
        "prompt_snapshot": run.prompt_snapshot,
        "guidance_snapshot": _json_list(getattr(run, "guidance_snapshot", None)),
        "output_targets_snapshot": _json_list(getattr(run, "output_targets_snapshot", None)),
        "context_snapshot": _json_dict(getattr(run, "context_snapshot", None)),
        "self_review_summary": getattr(run, "self_review_summary", None),
        "created_at": _required_datetime(run.created_at, run.scheduled_for),
    }


def _serialize_idea(idea: Idea) -> dict:
    return {
        "id": idea.id,
        "title": idea.title,
        "display_title": idea.display_title,
        "description": idea.description,
        "status": idea.status,
        "origin": idea.origin,
        "origin_ref": idea.origin_ref,
        "salience_score": idea.salience_score,
        "position_x": idea.position_x,
        "position_y": idea.position_y,
        "created_at": idea.created_at.isoformat() if idea.created_at else None,
        "updated_at": idea.updated_at.isoformat() if idea.updated_at else None,
        "user_id": idea.user_id,
        "archived_at": idea.archived_at.isoformat() if idea.archived_at else None,
        "active_agents": idea.active_agents,
        "attachments": idea.attachments or [],
    }


def _cycle_idea_title(cycle: Cycle, scheduled_for: datetime, *, per_run: bool) -> str:
    if not per_run:
        return cycle.name
    local_time = scheduled_for.astimezone(ZoneInfo(cycle.timezone))
    return f"{cycle.name} - {local_time.strftime('%b %d %I:%M %p')}"


async def _async_idea_has_active_run(session, idea_id: str) -> bool:
    stmt = (
        select(AgentRun.id)
        .where(
            AgentRun.thread_id == idea_id,
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.first() is not None


def _cycle_target_idea_scope_condition(cycle: Cycle):
    if cycle.org_id:
        org_user_ids = select(User.id).where(User.org_id == cycle.org_id)
        return or_(
            Idea.org_id == cycle.org_id,
            and_(Idea.org_id.is_(None), Idea.user_id.in_(org_user_ids)),
        )
    return Idea.user_id == cycle.user_id


async def async_record_cycle_revision(
    session,
    cycle: Cycle,
    *,
    source_type: str = "system",
    source_id: str | None = None,
    rationale: str | None = None,
) -> CycleRevision:
    result = await session.execute(
        select(func.coalesce(func.max(CycleRevision.revision_number), 0)).where(
            CycleRevision.cycle_id == cycle.id
        )
    )
    current_revision = int(result.scalar_one() or 0)
    revision = CycleRevision(
        cycle_id=cycle.id,
        revision_number=current_revision + 1,
        source_type=_actor_type(source_type),
        source_id=_actor_id(source_id),
        rationale=(rationale or "").strip() or None,
        name=cycle.name,
        prompt=cycle.prompt,
        schedule_expr=cycle.schedule_expr,
        timezone=cycle.timezone,
        enabled=cycle.enabled,
        model_override=cycle.model_override,
        thinking_override=cycle.thinking_override,
        target_idea_id=cycle.target_idea_id,
        context_policy={
            "workspace_id": _string_or_none(cycle.org_id),
            "owner_user_id": _string_or_none(cycle.user_id),
            **_creator_payload(cycle),
        },
    )
    session.add(revision)
    await session.flush()
    return revision


async def async_add_cycle_guidance(
    session,
    cycle: Cycle,
    *,
    guidance: str,
    source_type: str = "user",
    source_id: str | None = None,
    rationale: str | None = None,
    revision_id: int | None = None,
) -> CycleGuidance:
    guidance_text = validate_nonempty_trimmed(guidance, "guidance")
    row = CycleGuidance(
        cycle_id=cycle.id,
        revision_id=revision_id,
        source_type=_actor_type(source_type),
        source_id=_actor_id(source_id),
        guidance=guidance_text,
        rationale=(rationale or "").strip() or None,
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return row


async def async_add_cycle_output_target(
    session,
    cycle: Cycle,
    *,
    target_type: str,
    target_id: str | None = None,
    label: str | None = None,
    config: dict | None = None,
    source_type: str = "user",
    source_id: str | None = None,
    rationale: str | None = None,
    revision_id: int | None = None,
) -> CycleOutputTarget:
    target_type_text = validate_nonempty_trimmed(target_type, "target_type")
    row = CycleOutputTarget(
        cycle_id=cycle.id,
        revision_id=revision_id,
        target_type=target_type_text,
        target_id=(str(target_id).strip() if target_id is not None else None) or None,
        label=(label or "").strip() or None,
        config=_json_dict(config),
        source_type=_actor_type(source_type),
        source_id=_actor_id(source_id),
        rationale=(rationale or "").strip() or None,
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return row


async def async_remove_cycle_output_target(
    session,
    cycle: Cycle,
    *,
    target_id: int,
    source_type: str = "user",
    source_id: str | None = None,
    rationale: str | None = None,
    revision_id: int | None = None,
) -> CycleOutputTarget | None:
    row = await session.get(CycleOutputTarget, target_id)
    if row is None or row.cycle_id != cycle.id:
        return None
    row.is_active = False
    row.revision_id = revision_id
    row.source_type = _actor_type(source_type)
    row.source_id = _actor_id(source_id)
    row.rationale = (rationale or "").strip() or row.rationale
    await session.flush()
    return row


async def _async_latest_cycle_revision(session, cycle_id: int) -> CycleRevision | None:
    result = await session.scalars(
        select(CycleRevision)
        .where(CycleRevision.cycle_id == cycle_id)
        .order_by(CycleRevision.revision_number.desc(), CycleRevision.id.desc())
        .limit(1)
    )
    return result.first()


async def _async_active_cycle_guidance(session, cycle_id: int) -> list[CycleGuidance]:
    result = await session.scalars(
        select(CycleGuidance)
        .where(CycleGuidance.cycle_id == cycle_id, CycleGuidance.is_active.is_(True))
        .order_by(CycleGuidance.created_at.asc(), CycleGuidance.id.asc())
        .limit(25)
    )
    return list(result.all())


async def _async_active_cycle_output_targets(session, cycle_id: int) -> list[CycleOutputTarget]:
    result = await session.scalars(
        select(CycleOutputTarget)
        .where(CycleOutputTarget.cycle_id == cycle_id, CycleOutputTarget.is_active.is_(True))
        .order_by(CycleOutputTarget.created_at.asc(), CycleOutputTarget.id.asc())
        .limit(25)
    )
    return list(result.all())


async def _async_prepare_cycle_run_memory_snapshot(session, cycle: Cycle, run: CycleRun) -> None:
    try:
        revision = await _async_latest_cycle_revision(session, cycle.id)
        guidance_rows = await _async_active_cycle_guidance(session, cycle.id)
        target_rows = await _async_active_cycle_output_targets(session, cycle.id)
    except (AttributeError, IndexError, NotImplementedError):
        revision = None
        guidance_rows = []
        target_rows = []

    if revision is not None:
        run.revision_id = revision.id

    output_targets = [serialize_cycle_output_target(target) for target in target_rows]
    if not any(target.get("target_type") == CYCLE_LEDGER_OUTPUT_TARGET_TYPE for target in output_targets):
        output_targets.insert(
            0,
            {
                "target_type": CYCLE_LEDGER_OUTPUT_TARGET_TYPE,
                "target_id": str(cycle.id),
                "label": "Cycle ledger",
                "config": {},
                "source_type": "system",
                "rationale": "Implicit durable Cycle memory target.",
                "is_active": True,
            },
        )
    if cycle.target_idea_id and not any(
        target.get("target_type") == THREAD_OUTPUT_TARGET_TYPE
        and target.get("target_id") == str(cycle.target_idea_id)
        for target in output_targets
    ):
        output_targets.append(
            {
                "target_type": THREAD_OUTPUT_TARGET_TYPE,
                "target_id": str(cycle.target_idea_id),
                "label": "Cycle thread",
                "config": {},
                "source_type": "system",
                "rationale": "Implicit display thread target.",
                "is_active": True,
            }
        )

    run.guidance_snapshot = [serialize_cycle_guidance(row) for row in guidance_rows]
    run.output_targets_snapshot = output_targets
    run.context_snapshot = {
        "revision": serialize_cycle_revision(revision),
        "workspace_id": _string_or_none(cycle.org_id),
        "owner_user_id": _string_or_none(cycle.user_id),
        **_creator_payload(cycle),
    }


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


def _cycle_launch_envelope(cycle: Cycle, run: CycleRun) -> dict:
    """Return the minimal scheduler semantics around an otherwise free-form prompt."""

    return {
        "version": CYCLE_LAUNCH_ENVELOPE_VERSION,
        "origin": "scheduled_cycle",
        "cycle_id": cycle.id,
        "cycle_run_id": run.id,
        "cycle_revision_id": getattr(run, "revision_id", None),
        "cycle_name": cycle.name,
        "scheduled_for": run.scheduled_for.isoformat() if run.scheduled_for else None,
        "launch_mode": "background_cycle_run",
        "active_instruction_source": "cycle.prompt",
        "prior_thread_role": "context_only",
        "lifecycle_owner": "cycle_run",
        "thread_visibility": "output_target",
        "cycle_memory_role": "source_of_truth",
    }


def _cycle_run_metadata(cycle: Cycle, run: CycleRun) -> dict:
    envelope = _cycle_launch_envelope(cycle, run)
    return {
        "source": "cycle",
        "origin": "cycle",
        "cycle_id": cycle.id,
        "cycle_run_id": run.id,
        "model_override": cycle.model_override,
        "thinking_override": cycle.thinking_override,
        "launch_envelope": envelope,
        "cycle_memory": {
            "guidance": _json_list(getattr(run, "guidance_snapshot", None)),
            "output_targets": _json_list(getattr(run, "output_targets_snapshot", None)),
            "context": _json_dict(getattr(run, "context_snapshot", None)),
        },
        "contract": {
            "kind": "autonomous_cycle_run",
            "active_instruction_source": "cycle.prompt",
            "lifecycle_owner": "cycle_run",
        },
        "context_policy": {
            "current_instruction_role": "scheduled_prompt",
            "prior_thread_role": "context_only",
        },
    }


def _cycle_run_message(idea: Idea, cycle: Cycle, run: CycleRun) -> str:
    envelope = _cycle_launch_envelope(cycle, run)
    header = (
        f"[Idea: \"{idea.title}\" | {idea.id}]\n\n"
        "## Scheduled Prompt Launch\n"
        f"- Origin: {envelope['origin']}\n"
        f"- Cycle ID: {cycle.id}\n"
        f"- Cycle run ID: {run.id}\n"
        "- The Cycle mission below is the current instruction.\n"
        "- Thread messages are output/context surfaces, not durable Cycle memory.\n"
        "- Use Cycle memory, revisions, guidance, output targets, and the workspace state as source of truth.\n"
        "- You may create, update, delete, or run Cycles when that is the right workspace action; include rationale.\n"
        "- If an output target is unavailable, repair or replace it when possible instead of treating it as a blocker.\n"
        "- End with a short self-review summary suitable for the Cycle ledger and visible outputs.\n\n"
        "## Cycle Memory Snapshot\n"
        f"{_json_dict(getattr(run, 'context_snapshot', None))}\n\n"
        "## Guidance Snapshot\n"
        f"{_json_list(getattr(run, 'guidance_snapshot', None))}\n\n"
        "## Output Targets\n"
        f"{_json_list(getattr(run, 'output_targets_snapshot', None))}\n\n"
        "## Cycle Mission\n"
    )
    return f"{header}{cycle.prompt[:2000]}"


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


def _finalize_cycle_run(
    run: CycleRun,
    cycle: Cycle,
    *,
    status: str,
    error: str | None = None,
    skip_reason: str | None = None,
    session=None,
) -> None:
    now = datetime.now(timezone.utc)
    run.status = status
    run.completed_at = now
    run.error = error
    run.skip_reason = skip_reason
    cycle.last_run_at = now
    cycle.last_status = status
    cycle.last_error = error
    _record_cycle_run_evaluation(
        session,
        run,
        cycle,
        status=status,
        error=error,
        skip_reason=skip_reason,
    )


def _finalize_stale_cycle_run(
    run: CycleRun,
    cycle: Cycle | None,
    *,
    status: str,
    error: str | None = None,
    skip_reason: str | None = None,
    session=None,
) -> None:
    now = datetime.now(timezone.utc)
    run.status = status
    run.completed_at = now
    run.error = error
    run.skip_reason = skip_reason
    if cycle is not None:
        _record_cycle_run_evaluation(
            session,
            run,
            cycle,
            status=status,
            error=error,
            skip_reason=skip_reason,
            evaluator_type="recovery",
        )
    if cycle is None:
        return
    cycle_last_run_at = _aware_utc(cycle.last_run_at)
    scheduled_for = _aware_utc(run.scheduled_for)
    if cycle_last_run_at is None or (
        scheduled_for is not None and scheduled_for >= cycle_last_run_at
    ):
        cycle.last_run_at = now
        cycle.last_status = status
        cycle.last_error = error


def _cycle_run_evaluation_summary(
    *,
    status: str,
    error: str | None = None,
    skip_reason: str | None = None,
) -> str:
    if status == "completed":
        return "Cycle run completed and was recorded in the Cycle ledger."
    if status == "failed":
        detail = error or "unknown failure"
        return f"Cycle run failed and was recorded in the Cycle ledger: {detail}"
    if status == "skipped":
        detail = skip_reason or "unknown skip reason"
        return f"Cycle run was skipped and recorded in the Cycle ledger: {detail}"
    return f"Cycle run reached status {status} and was recorded in the Cycle ledger."


def _record_cycle_run_evaluation(
    session,
    run: CycleRun,
    cycle: Cycle,
    *,
    status: str,
    error: str | None = None,
    skip_reason: str | None = None,
    evaluator_type: str = "system",
    evaluator_id: str | None = None,
) -> None:
    summary = _cycle_run_evaluation_summary(
        status=status,
        error=error,
        skip_reason=skip_reason,
    )
    run.self_review_summary = summary
    if session is None or not hasattr(session, "add") or run.id is None:
        return
    session.add(
        CycleRunEvaluation(
            cycle_id=cycle.id,
            cycle_run_id=run.id,
            evaluator_type=evaluator_type,
            evaluator_id=_actor_id(evaluator_id),
            summary=summary,
            score=1 if status == "completed" else 0 if status == "failed" else None,
            details={
                "status": status,
                "error": error,
                "skip_reason": skip_reason,
                "agent_run_id": run.run_id,
                "idea_id": _string_or_none(run.idea_id),
            },
        )
    )


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
                    _finalize_stale_cycle_run(
                        run,
                        cycle,
                        status="skipped",
                        skip_reason="missed_catchup_window",
                        session=uow.session,
                    )
                elif cycle is None or cycle.deleted_at is not None:
                    _finalize_stale_cycle_run(
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
                _finalize_stale_cycle_run(
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
                _finalize_cycle_run(
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

        idea = None
        if cycle.target_idea_id:
            result = await uow.session.scalars(
                select(Idea)
                .where(
                    Idea.id == cycle.target_idea_id,
                    _cycle_target_idea_scope_condition(cycle),
                )
                .with_for_update()
            )
            idea = result.first()
        if idea and idea.archived_at is not None:
            await transition_thought_status(
                uow.session,
                idea=idea,
                command=ThoughtStatusCommand(
                    to_status="needs_input",
                    trigger="cycle_reopen",
                    actor={
                        "user_id": str(cycle.user_id) if cycle.user_id else None,
                        "org_id": str(cycle.org_id) if cycle.org_id else None,
                    },
                ),
            )
            should_publish_idea = True
        if idea and await _async_idea_has_active_run(uow.session, idea.id):
            idea = Idea(
                title=_cycle_idea_title(cycle, run.scheduled_for, per_run=True),
                description=cycle.prompt[:2000],
                status="emerged",
                origin="cycle_run",
                origin_ref=f"cycle:{cycle.id}:run:{run.id}",
                user_id=cycle.user_id,
                org_id=cycle.org_id,
            )
            uow.session.add(idea)
            await uow.session.flush()
            should_publish_idea = True
        if idea is None:
            idea = Idea(
                title=_cycle_idea_title(cycle, run.scheduled_for, per_run=False),
                description=cycle.prompt[:2000],
                status="emerged",
                origin="cycle",
                origin_ref=f"cycle:{cycle.id}",
                user_id=cycle.user_id,
                org_id=cycle.org_id,
            )
            uow.session.add(idea)
            await uow.session.flush()
            cycle.target_idea_id = idea.id
            should_publish_idea = True

        run.idea_id = idea.id
        await _async_prepare_cycle_run_memory_snapshot(uow.session, cycle, run)
        output_targets = _json_list(getattr(run, "output_targets_snapshot", None))
        if not any(
            target.get("target_type") == THREAD_OUTPUT_TARGET_TYPE
            and target.get("target_id") == str(idea.id)
            for target in output_targets
            if isinstance(target, dict)
        ):
            output_targets.append(
                {
                    "target_type": THREAD_OUTPUT_TARGET_TYPE,
                    "target_id": str(idea.id),
                    "label": "Cycle run execution thread",
                    "config": {"ephemeral": cycle.target_idea_id != idea.id},
                    "source_type": "system",
                    "rationale": "Execution output surface for this CycleRun.",
                    "is_active": True,
                }
            )
            run.output_targets_snapshot = output_targets
        run.started_at = datetime.now(timezone.utc)
        run.status = "running"
        idea_id = idea.id
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
            _finalize_cycle_run(
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
        idea_snapshot = _serialize_idea(idea)
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
            _short_identifier(idea_id),
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
        run = await uow.session.get(AgentRun, run_id)
        metadata = run.metadata_ if run else None
        if not isinstance(metadata, dict) or metadata.get("source") != "cycle":
            return
        cycle_run_id = metadata.get("cycle_run_id")
        if not cycle_run_id:
            return
        run = await uow.session.get(CycleRun, int(cycle_run_id))
        cycle = await uow.session.get(Cycle, run.cycle_id) if run else None
        if not run or not cycle or run.status in TERMINAL_RUN_STATUSES:
            return
        _finalize_cycle_run(
            run,
            cycle,
            status=status,
            error=error if status == "failed" else None,
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
