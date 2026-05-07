"""Cycles orchestration tool handlers."""

from __future__ import annotations

from brain.systems.runs.tool_catalog.handlers.common import *

def _handle_manage_cycle(
    action: str,
    id: int | None = None,
    name: str | None = None,
    prompt: str | None = None,
    schedule_expr: str | None = None,
    timezone: str | None = None,
    enabled: bool | None = None,
    model_override: str | None = None,
    thinking_override: str | None = None,
    execution_mode: str | None = None,
    target_idea_id: str | None = None,
    reopen_archived: bool | None = None,
) -> str:
    from sqlalchemy import and_, or_, select

    from brain.systems.cycles.service import (
        compute_next_run_at,
        cycle_defaults,
        REUSABLE_THREAD_EXECUTION_MODE,
        run_cycle_now,
        serialize_cycle,
        validate_nonempty_trimmed,
        validate_execution_mode,
        validate_schedule_expr,
        validate_thinking_override,
        validate_timezone_name,
    )
    from brain.platform.db.models.cycle import Cycle
    from brain.platform.db.models.idea import Idea
    from brain.platform.db.models.org import User
    from brain.platform.db.repositories.unit_of_work import UnitOfWork
    from datetime import timezone as dt_timezone

    user_id = getattr(_agent_context, "user_id", None)
    org_id = getattr(_agent_context, "org_id", None)
    if not user_id:
        return json.dumps({"error": "manage_cycle requires a user-scoped cortex run"})

    def _cycle_scope():
        scope = [Cycle.deleted_at.is_(None)]
        if org_id:
            org_user_ids = select(User.id).where(User.org_id == org_id)
            scope.append(
                or_(
                    Cycle.org_id == org_id,
                    and_(Cycle.org_id.is_(None), Cycle.user_id.in_(org_user_ids)),
                )
            )
        else:
            scope.append(Cycle.user_id == user_id)
        return scope

    def _idea_scope(idea_id: str):
        scope = [Idea.id == idea_id]
        if org_id:
            org_user_ids = select(User.id).where(User.org_id == org_id)
            scope.append(
                or_(
                    Idea.org_id == org_id,
                    and_(Idea.org_id.is_(None), Idea.user_id.in_(org_user_ids)),
                )
            )
        else:
            scope.append(Idea.user_id == user_id)
        return scope

    try:
        if action == "list":
            with UnitOfWork() as uow:
                stmt = (
                    select(Cycle)
                    .where(*_cycle_scope())
                    .order_by(Cycle.created_at.desc())
                )
                cycles = [serialize_cycle(cycle) for cycle in uow.session.scalars(stmt).all()]
            return json.dumps({"cycles": cycles}, default=str)
        elif action == "create":
            if not name or not prompt or not schedule_expr or not timezone:
                return json.dumps({"error": "create requires: name, prompt, schedule_expr, timezone"})
            expr = validate_schedule_expr(schedule_expr)
            tz_name = validate_timezone_name(timezone)
            mode = validate_execution_mode(execution_mode)
            thinking = validate_thinking_override(thinking_override)
            normalized_name = validate_nonempty_trimmed(name, "name")
            normalized_prompt = validate_nonempty_trimmed(prompt, "prompt")
            with UnitOfWork() as uow:
                if target_idea_id:
                    stmt = select(Idea.id).where(*_idea_scope(target_idea_id))
                    if not uow.session.execute(stmt).first():
                        return json.dumps({"error": "target_idea_id must belong to the current workspace"})
                cycle = Cycle(
                    user_id=user_id,
                    org_id=org_id,
                    name=normalized_name,
                    prompt=normalized_prompt,
                    schedule_expr=expr,
                    timezone=tz_name,
                    enabled=True if enabled is None else enabled,
                    model_override=(model_override or "").strip() or None,
                    thinking_override=thinking,
                    execution_mode=REUSABLE_THREAD_EXECUTION_MODE,
                    target_idea_id=target_idea_id,
                    reopen_archived=cycle_defaults(
                        execution_mode=mode,
                        reopen_archived=reopen_archived,
                    ),
                    next_run_at=compute_next_run_at(expr, tz_name),
                )
                uow.session.add(cycle)
                uow.session.flush()
                payload = serialize_cycle(cycle)
            return json.dumps({"created": payload}, default=str)
        elif action == "update":
            if not id:
                return json.dumps({"error": "update requires: id"})
            with UnitOfWork() as uow:
                stmt = select(Cycle).where(
                    Cycle.id == id,
                    *_cycle_scope(),
                )
                cycle = uow.session.scalars(stmt).first()
                if not cycle:
                    return json.dumps({"error": f"Cycle {id} not found"})
                if target_idea_id:
                    idea_stmt = select(Idea.id).where(*_idea_scope(target_idea_id))
                    if not uow.session.execute(idea_stmt).first():
                        return json.dumps({"error": "target_idea_id must belong to the current workspace"})
                if name is not None:
                    cycle.name = validate_nonempty_trimmed(name, "name")
                if prompt is not None:
                    cycle.prompt = validate_nonempty_trimmed(prompt, "prompt")
                if schedule_expr is not None:
                    cycle.schedule_expr = validate_schedule_expr(schedule_expr)
                if timezone is not None:
                    cycle.timezone = validate_timezone_name(timezone)
                if enabled is not None:
                    cycle.enabled = enabled
                if model_override is not None:
                    cycle.model_override = (model_override or "").strip() or None
                if thinking_override is not None:
                    cycle.thinking_override = validate_thinking_override(thinking_override)
                if execution_mode is not None:
                    cycle.execution_mode = validate_execution_mode(execution_mode)
                if target_idea_id is not None:
                    cycle.target_idea_id = target_idea_id
                if reopen_archived is not None:
                    cycle.reopen_archived = reopen_archived
                elif execution_mode is not None:
                    cycle.reopen_archived = cycle_defaults(
                        execution_mode=cycle.execution_mode,
                        reopen_archived=None,
                    )
                cycle.execution_mode = REUSABLE_THREAD_EXECUTION_MODE
                cycle.reopen_archived = True
                cycle.updated_at = datetime.now(dt_timezone.utc)
                cycle.next_run_at = compute_next_run_at(cycle.schedule_expr, cycle.timezone)
                payload = serialize_cycle(cycle)
            return json.dumps({"updated": payload}, default=str)
        elif action == "delete":
            if not id:
                return json.dumps({"error": "delete requires: id"})
            with UnitOfWork() as uow:
                stmt = select(Cycle).where(
                    Cycle.id == id,
                    *_cycle_scope(),
                )
                cycle = uow.session.scalars(stmt).first()
                if not cycle:
                    return json.dumps({"error": f"Cycle {id} not found"})
                cycle.enabled = False
                cycle.deleted_at = datetime.now(dt_timezone.utc)
            return json.dumps({"deleted": {"id": id}})
        elif action == "run":
            if not id:
                return json.dumps({"error": "run requires: id"})
            with UnitOfWork() as uow:
                stmt = select(Cycle.id).where(
                    Cycle.id == id,
                    *_cycle_scope(),
                )
                if not uow.session.execute(stmt).first():
                    return json.dumps({"error": f"Cycle {id} not found"})
            return json.dumps({"run": run_cycle_now(id)}, default=str)
        else:
            return json.dumps({"error": f"Unknown action: {action}"})
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.exception(f"manage_cycle failed: {e}")
        if _is_missing_cycle_schema_error(e):
            return json.dumps(_cycle_schema_missing_payload(e))
        return json.dumps({"error": str(e)})

__all__ = [name for name in globals() if not name.startswith("__")]
