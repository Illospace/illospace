"""Cycle CRUD, scheduling, and execution helpers."""
from __future__ import annotations

import logging

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import and_, or_, select

from brain.app.api.routers.cortex._helpers import _parse_message_type
from brain.systems.runs.cortex import RunAdmissionRequest, admit_run
from brain.systems.cortex.events import publish
from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.run import AgentRun
from brain.platform.db.models.idea import Idea, IdeaStateLog, IdeaThread
from brain.platform.db.models.org import User
from brain.platform.db.repositories.unit_of_work import UnitOfWork, run_sync_with_unit_of_work

logger = logging.getLogger("cycles")

REUSABLE_THREAD_EXECUTION_MODE = "reuse_same_idea"
VALID_EXECUTION_MODES = {"new_idea_per_run", REUSABLE_THREAD_EXECUTION_MODE}
VALID_THINKING_OVERRIDES = {"none", "low", "medium", "high", "xhigh"}
ACTIVE_RUN_STATUSES = {"queued", "running", "pending_approval"}
TERMINAL_RUN_STATUSES = {"completed", "failed", "skipped"}
ONE_TIME_SCHEDULE_PREFIX = "at:"


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


def serialize_cycle(cycle: Cycle) -> dict:
    created_at = _required_datetime(cycle.created_at, cycle.updated_at)
    updated_at = _required_datetime(cycle.updated_at, created_at)
    return {
        "id": cycle.id,
        "user_id": str(cycle.user_id),
        "org_id": _string_or_none(cycle.org_id),
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
        "scheduled_for": run.scheduled_for,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "status": run.status,
        "error": run.error,
        "skip_reason": run.skip_reason,
        "idea_id": _string_or_none(run.idea_id),
        "run_id": run.run_id,
        "prompt_snapshot": run.prompt_snapshot,
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


def _idea_has_active_run(session, idea_id: str) -> bool:
    stmt = (
        select(AgentRun.id)
        .where(
            AgentRun.thread_id == idea_id,
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .limit(1)
    )
    return session.execute(stmt).first() is not None


def _cycle_target_idea_scope_condition(cycle: Cycle):
    if cycle.org_id:
        org_user_ids = select(User.id).where(User.org_id == cycle.org_id)
        return or_(
            Idea.org_id == cycle.org_id,
            and_(Idea.org_id.is_(None), Idea.user_id.in_(org_user_ids)),
        )
    return Idea.user_id == cycle.user_id


def _admit_cycle_run(
    session,
    *,
    idea_id: str,
    message: str,
    priority: int,
    user_id: str | None,
    metadata: dict | None,
    cycle_run_id: int,
) -> int | None:
    result = admit_run(
        RunAdmissionRequest(
            idea_id=idea_id,
            event="thread_reply",
            message=message,
            priority=priority,
            user_id=user_id,
            metadata=metadata,
            source="cycle",
            producer="cycle",
            idempotency_key=f"cycle_run:{cycle_run_id}",
        ),
        session=session,
    )
    return result.run_id if result.ok else None


def _append_cycle_thread_message(
    session,
    idea: Idea,
    cycle: Cycle,
    cycle_run: CycleRun,
    owner: User | None,
) -> tuple[dict, dict | None]:
    current_status = idea.status
    metadata = {
        "source": "cycle",
        "cycle_id": cycle.id,
        "cycle_run_id": cycle_run.id,
    }
    thread_msg = IdeaThread(
        idea_id=idea.id,
        role="user",
        content=cycle.prompt,
        attachments=[],
        metadata_=metadata,
        user_id=cycle.user_id,
        message_type=_parse_message_type(cycle.prompt, "user"),
    )
    session.add(thread_msg)
    session.flush()

    new_status = None
    if current_status in ("needs_input", "unread_reply", "emerged"):
        new_status = "active"

    status_payload = None
    if new_status and new_status != current_status:
        idea.status = new_status
        idea.updated_at = datetime.now(timezone.utc)
        session.add(
            IdeaStateLog(
                idea_id=idea.id,
                from_state=current_status,
                to_state=new_status,
                trigger="auto_cycle_message",
            )
        )
        status_payload = {
            "idea_id": idea.id,
            "old_status": current_status,
            "new_status": new_status,
        }

    message_payload = {
        "id": thread_msg.id,
        "idea_id": idea.id,
        "role": thread_msg.role,
        "content": thread_msg.content,
        "attachments": [],
        "metadata": thread_msg.metadata_,
        "user_id": cycle.user_id,
        "message_type": thread_msg.message_type,
        "created_at": thread_msg.created_at.isoformat() if thread_msg.created_at else None,
    }
    if owner:
        message_payload["user_name"] = owner.name
        message_payload["user_color"] = owner.color
    return message_payload, status_payload


def _finalize_cycle_run(
    run: CycleRun,
    cycle: Cycle,
    *,
    status: str,
    error: str | None = None,
    skip_reason: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    run.status = status
    run.completed_at = now
    run.error = error
    run.skip_reason = skip_reason
    cycle.last_run_at = now
    cycle.last_status = status
    cycle.last_error = error


def create_cycle_run_record(cycle_id: int, *, scheduled_for: datetime, prompt_snapshot: str) -> int:
    with UnitOfWork() as uow:
        run = CycleRun(
            cycle_id=cycle_id,
            scheduled_for=scheduled_for,
            prompt_snapshot=prompt_snapshot,
            status="queued",
        )
        uow.session.add(run)
        uow.session.flush()
        return run.id


def _load_cycle_prompt(cycle_id: int) -> str:
    with UnitOfWork() as uow:
        cycle = uow.session.get(Cycle, cycle_id)
        if not cycle or cycle.deleted_at is not None:
            raise ValueError("Cycle not found")
        return cycle.prompt


def run_cycle_now(cycle_id: int) -> dict:
    run_id = create_cycle_run_record(
        cycle_id,
        scheduled_for=datetime.now(timezone.utc),
        prompt_snapshot=_load_cycle_prompt(cycle_id),
    )
    execute_cycle_run(run_id)
    with UnitOfWork() as uow:
        run = uow.session.get(CycleRun, run_id)
        return serialize_cycle_run(run)


async def async_run_cycle_now(cycle_id: int) -> dict:
    return await run_sync_with_unit_of_work(run_cycle_now, cycle_id)


def schedule_due_cycles_once(*, limit: int = 10) -> list[int]:
    claimed_run_ids: list[int] = []
    now = datetime.now(timezone.utc)

    with UnitOfWork() as uow:
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
        cycles = uow.session.scalars(stmt).all()
        for cycle in cycles:
            scheduled_for = cycle.next_run_at or now
            run = CycleRun(
                cycle_id=cycle.id,
                scheduled_for=scheduled_for,
                prompt_snapshot=cycle.prompt,
                status="queued",
            )
            uow.session.add(run)
            uow.session.flush()
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
        execute_cycle_run(run_id)

    return claimed_run_ids


def execute_cycle_run(run_id: int) -> None:
    message_payload = None
    status_payload = None
    should_publish_idea = False
    idea_snapshot = None
    idea_id = None
    run_message = None
    run_metadata = None
    cycle_name = ""
    cycle_user_id = None
    run_id = None

    with UnitOfWork() as uow:
        run = uow.session.scalars(
            select(CycleRun).where(CycleRun.id == run_id).with_for_update()
        ).first()
        if not run or run.status in TERMINAL_RUN_STATUSES:
            return
        cycle = uow.session.scalars(
            select(Cycle).where(Cycle.id == run.cycle_id).with_for_update()
        ).first()
        if not cycle or cycle.deleted_at is not None:
            if cycle:
                _finalize_cycle_run(
                    run,
                    cycle,
                    status="failed",
                    error="Cycle deleted before run started",
                )
            else:
                run.status = "failed"
                run.error = "Cycle deleted before run started"
                run.completed_at = datetime.now(timezone.utc)
            return

        cycle_name = cycle.name
        cycle_user_id = cycle.user_id
        owner = uow.session.get(User, cycle.user_id)
        cycle.execution_mode = REUSABLE_THREAD_EXECUTION_MODE
        cycle.reopen_archived = True

        idea = None
        if cycle.target_idea_id:
            idea = uow.session.scalars(
                select(Idea)
                .where(
                    Idea.id == cycle.target_idea_id,
                    _cycle_target_idea_scope_condition(cycle),
                )
                .with_for_update()
            ).first()
        if idea and idea.archived_at is not None:
            old_status = idea.status
            idea.archived_at = None
            idea.status = "needs_input"
            idea.updated_at = datetime.now(timezone.utc)
            uow.session.add(
                IdeaStateLog(
                    idea_id=idea.id,
                    from_state=old_status,
                    to_state="needs_input",
                    trigger="cycle_reopen",
                )
            )
            should_publish_idea = True
        if idea and _idea_has_active_run(uow.session, idea.id):
            run.idea_id = idea.id
            _finalize_cycle_run(
                run,
                cycle,
                status="skipped",
                skip_reason="idea_busy",
            )
            return
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
            uow.session.flush()
            cycle.target_idea_id = idea.id
            should_publish_idea = True

        run.idea_id = idea.id
        run.started_at = datetime.now(timezone.utc)
        run.status = "running"
        idea_id = idea.id
        run_metadata = {
            "source": "cycle",
            "cycle_id": cycle.id,
            "cycle_run_id": run.id,
            "model_override": cycle.model_override,
            "thinking_override": cycle.thinking_override,
        }
        run_message = f"[Idea: \"{idea.title}\" | {idea.id}]\n\n{cycle.prompt[:2000]}"
        run_id = _admit_cycle_run(
            uow.session,
            idea_id=idea.id,
            message=run_message,
            priority=1,
            user_id=cycle_user_id,
            metadata=run_metadata,
            cycle_run_id=run.id,
        )
        if run_id is None:
            _finalize_cycle_run(
                run,
                cycle,
                status="skipped",
                skip_reason="idea_busy",
            )
            return
        message_payload, status_payload = _append_cycle_thread_message(
            uow.session,
            idea,
            cycle,
            run,
            owner,
        )
        idea_snapshot = _serialize_idea(idea)
        run.run_id = run_id
        cycle.last_status = "running"
        cycle.last_error = None
    if should_publish_idea and idea_snapshot:
        publish("idea_upserted", {"idea": idea_snapshot})
    if message_payload:
        publish("thread_message", {"idea_id": idea_id, "message": message_payload})
    if status_payload:
        publish("status_change", status_payload)
    if run_id is not None:
        logger.info(
            "Enqueued cycle run #%s for idea %s... (cycle=%s)",
            run_id,
            _short_identifier(idea_id),
            cycle_name,
        )


def finalize_cycle_run_from_run(
    run_id: int,
    *,
    status: str,
    error: str | None = None,
) -> None:
    if status not in {"completed", "failed"}:
        return
    with UnitOfWork() as uow:
        run = uow.session.get(AgentRun, run_id)
        metadata = run.metadata_ if run else None
        if not isinstance(metadata, dict) or metadata.get("source") != "cycle":
            return
        cycle_run_id = metadata.get("cycle_run_id")
        if not cycle_run_id:
            return
        run = uow.session.get(CycleRun, int(cycle_run_id))
        cycle = uow.session.get(Cycle, run.cycle_id) if run else None
        if not run or not cycle or run.status in TERMINAL_RUN_STATUSES:
            return
        _finalize_cycle_run(
            run,
            cycle,
            status=status,
            error=error if status == "failed" else None,
        )
