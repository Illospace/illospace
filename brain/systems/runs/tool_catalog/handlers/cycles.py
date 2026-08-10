"""Cycles orchestration tool handlers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.idea import Idea
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cycles.access import (
    CycleActor,
    cycle_scope_conditions,
    target_idea_scope_conditions,
)
from brain.systems.cycles.common import (
    AGENT_TRIGGERED_CYCLE_ORIGIN,
    OFF_SLOT_MATERIAL_ALERT_RUN_KIND,
    validate_cycle_timeout_seconds,
)
from brain.systems.cycles.contracts import normalize_cycle_run_kind
from brain.systems.cycles.commands import (
    UNSET_CYCLE_FIELD,
    async_add_guidance_to_cycle,
    async_add_output_target_to_cycle,
    async_create_cycle,
    async_delete_cycle,
    async_remove_output_target_from_cycle,
    async_update_cycle,
    cycle_change_event,
)
from brain.systems.cycles.events import publish_cycle_change
from brain.systems.cycles.serializers import (
    serialize_cycle,
    serialize_cycle_guidance,
    serialize_cycle_output_target,
)
from brain.systems.cycles.service import async_run_cycle_now
from brain.systems.runs.token_usage import (
    async_summarize_run_trees_usage,
    merge_usage_totals,
    usage_totals_payload,
)
from brain.systems.runs.tool_catalog.handlers.common import *


@dataclass(frozen=True)
class ManageCycleArgs:
    action: str
    id: int | None = None
    name: str | None = None
    prompt: str | None = None
    schedule_expr: str | None = None
    run_at: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    timeout_seconds: Any = UNSET_CYCLE_FIELD
    model_override: str | None = None
    thinking_override: str | None = None
    execution_policy_key: Any = UNSET_CYCLE_FIELD
    target_idea_id: str | None = None
    guidance: str | None = None
    rationale: str | None = None
    run_kind: str | None = None
    output_target_type: str | None = None
    output_target_id: str | None = None
    output_target_label: str | None = None
    output_target_config: dict | None = None
    days: int | None = None
    run_limit: int | None = None


@dataclass(frozen=True)
class ManageCycleContext:
    args: ManageCycleArgs
    actor: CycleActor


CycleAction = Callable[[ManageCycleContext], Awaitable[dict[str, Any]]]


async def _handle_manage_cycle(
    action: str,
    operation: str | None = None,
    id: int | None = None,
    name: str | None = None,
    prompt: str | None = None,
    schedule_expr: str | None = None,
    run_at: str | None = None,
    timezone: str | None = None,
    enabled: bool | None = None,
    timeout_seconds=UNSET_CYCLE_FIELD,
    model_override: str | None = None,
    thinking_override: str | None = None,
    execution_policy_key=UNSET_CYCLE_FIELD,
    target_idea_id: str | None = None,
    guidance: str | None = None,
    rationale: str | None = None,
    run_kind: str | None = None,
    output_target_type: str | None = None,
    output_target_id: str | None = None,
    output_target_label: str | None = None,
    output_target_config: dict | None = None,
    days: int | None = None,
    run_limit: int | None = None,
):
    return await _handle_manage_cycle_async(
        action=action,
        operation=operation,
        id=id,
        name=name,
        prompt=prompt,
        schedule_expr=schedule_expr,
        run_at=run_at,
        timezone=timezone,
        enabled=enabled,
        timeout_seconds=timeout_seconds,
        model_override=model_override,
        thinking_override=thinking_override,
        execution_policy_key=execution_policy_key,
        target_idea_id=target_idea_id,
        guidance=guidance,
        rationale=rationale,
        run_kind=run_kind,
        output_target_type=output_target_type,
        output_target_id=output_target_id,
        output_target_label=output_target_label,
        output_target_config=output_target_config,
        days=days,
        run_limit=run_limit,
    )


async def _handle_manage_cycle_async(
    action: str,
    operation: str | None = None,
    id: int | None = None,
    name: str | None = None,
    prompt: str | None = None,
    schedule_expr: str | None = None,
    run_at: str | None = None,
    timezone: str | None = None,
    enabled: bool | None = None,
    timeout_seconds=UNSET_CYCLE_FIELD,
    model_override: str | None = None,
    thinking_override: str | None = None,
    execution_policy_key=UNSET_CYCLE_FIELD,
    target_idea_id: str | None = None,
    guidance: str | None = None,
    rationale: str | None = None,
    run_kind: str | None = None,
    output_target_type: str | None = None,
    output_target_id: str | None = None,
    output_target_label: str | None = None,
    output_target_config: dict | None = None,
    days: int | None = None,
    run_limit: int | None = None,
) -> str:
    normalized_action = str(action or "").strip().lower()
    if normalized_action in {"help", "schema"}:
        return _manage_tool_guide("manage_cycle", operation)

    handler = CYCLE_ACTIONS.get(normalized_action)
    if handler is None:
        return json.dumps({"error": f"Unknown action: {normalized_action}"})

    user_id = getattr(_agent_context, "user_id", None)
    if not user_id:
        return json.dumps({"error": "manage_cycle requires user context"})

    args = ManageCycleArgs(
        action=normalized_action,
        id=id,
        name=name,
        prompt=prompt,
        schedule_expr=schedule_expr,
        run_at=run_at,
        timezone=timezone,
        enabled=enabled,
        timeout_seconds=timeout_seconds,
        model_override=model_override,
        thinking_override=thinking_override,
        execution_policy_key=execution_policy_key,
        target_idea_id=target_idea_id,
        guidance=guidance,
        rationale=rationale,
        run_kind=run_kind,
        output_target_type=output_target_type,
        output_target_id=output_target_id,
        output_target_label=output_target_label,
        output_target_config=output_target_config,
        days=days,
        run_limit=run_limit,
    )
    context = ManageCycleContext(args=args, actor=_tool_actor(str(user_id)))

    try:
        payload = await handler(context)
        return json.dumps(payload, default=str)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.exception("manage_cycle failed: %s", exc)
        if _is_missing_cycle_schema_error(exc):
            return json.dumps(_cycle_schema_missing_payload(exc))
        return json.dumps({"error": str(exc)})


async def _action_list(ctx: ManageCycleContext) -> dict[str, Any]:
    async with UnitOfWork() as uow:
        result = await uow.session.scalars(
            select(Cycle)
            .where(*cycle_scope_conditions(ctx.actor))
            .order_by(Cycle.created_at.desc())
        )
        return {"cycles": [serialize_cycle(cycle) for cycle in result.all()]}


def _window_value(value: int | None, *, field: str, maximum: int) -> int | None:
    if value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if normalized < 1 or normalized > maximum:
        raise ValueError(f"{field} must be between 1 and {maximum}")
    return normalized


async def _action_usage_summary(ctx: ManageCycleContext) -> dict[str, Any]:
    args = ctx.args
    days = _window_value(args.days, field="days", maximum=3650)
    run_limit = _window_value(args.run_limit, field="run_limit", maximum=500)
    if days is None and run_limit is None:
        days = 30
    effective_limit = run_limit or 500
    since = datetime.now(timezone.utc) - timedelta(days=days) if days is not None else None

    async with UnitOfWork() as uow:
        stmt = (
            select(CycleRun, Cycle)
            .join(Cycle, Cycle.id == CycleRun.cycle_id)
            .where(
                *cycle_scope_conditions(ctx.actor),
                CycleRun.run_id.isnot(None),
            )
            .order_by(CycleRun.scheduled_for.desc(), CycleRun.id.desc())
            .limit(effective_limit)
        )
        if args.id is not None:
            stmt = stmt.where(Cycle.id == args.id)
        if since is not None:
            stmt = stmt.where(CycleRun.scheduled_for >= since)
        selected_rows = list((await uow.session.execute(stmt)).all())
        run_ids = [int(cycle_run.run_id) for cycle_run, _cycle in selected_rows]
        usage_by_run = await async_summarize_run_trees_usage(uow.session, run_ids)

    cycles: dict[int, dict[str, Any]] = {}
    totals = usage_totals_payload(include_runs=True)
    for cycle_run, cycle in selected_rows:
        cycle_usage = cycles.setdefault(
            int(cycle.id),
            {
                "cycle_id": int(cycle.id),
                "name": cycle.name,
                **usage_totals_payload(include_runs=True),
            },
        )
        usage = usage_by_run.get(int(cycle_run.run_id), {})
        merge_usage_totals(cycle_usage, usage, runs=1)
        merge_usage_totals(totals, usage, runs=1)

    return {
        "usage_summary": {
            "window": {
                "days": days,
                "run_limit": effective_limit,
                "since": since.isoformat() if since is not None else None,
                "selected_runs": len(selected_rows),
                "truncated": len(selected_rows) == effective_limit,
            },
            "totals": totals,
            "cycles": sorted(
                cycles.values(),
                key=lambda item: (-item["estimated_cost"], -item["tokens_total"], item["cycle_id"]),
            ),
        }
    }


async def _action_create(ctx: ManageCycleContext) -> dict[str, Any]:
    args = ctx.args
    timeout_seconds = (
        None
        if args.timeout_seconds is UNSET_CYCLE_FIELD
        else validate_cycle_timeout_seconds(args.timeout_seconds)
    )
    async with UnitOfWork() as uow:
        await _validate_target_idea(uow.session, args.target_idea_id, ctx.actor)
        cycle = await async_create_cycle(
            uow.session,
            actor=ctx.actor,
            name=_optional_text(args.name),
            prompt=_optional_text(args.prompt),
            timezone_name=_optional_text(args.timezone),
            schedule_expr=_optional_text(args.schedule_expr),
            run_at=_optional_text(args.run_at),
            enabled=True if args.enabled is None else args.enabled,
            timeout_seconds=timeout_seconds,
            model_override=args.model_override,
            thinking_override=args.thinking_override,
            execution_policy_key=(
                None
                if args.execution_policy_key is UNSET_CYCLE_FIELD
                else _optional_text(args.execution_policy_key)
            ),
            target_idea_id=_optional_text(args.target_idea_id),
            guidance=_optional_text(args.guidance),
            rationale=_optional_text(args.rationale),
        )
        payload = serialize_cycle(cycle)
    publish_cycle_change(action="create", **_event_from_payload(payload))
    return {"created": payload}


async def _action_update(ctx: ManageCycleContext) -> dict[str, Any]:
    args = ctx.args
    timeout_seconds = args.timeout_seconds
    if timeout_seconds is not UNSET_CYCLE_FIELD:
        timeout_seconds = validate_cycle_timeout_seconds(timeout_seconds)
    async with UnitOfWork() as uow:
        cycle = await _load_cycle(uow.session, ctx.actor, args.id)
        update_target_idea_id = _optional_text(args.target_idea_id)
        if args.target_idea_id is not None:
            await _validate_target_idea(uow.session, update_target_idea_id, ctx.actor)
        await async_update_cycle(
            uow.session,
            cycle,
            actor=ctx.actor,
            name=_patch_text(args.name),
            prompt=_patch_text(args.prompt),
            timezone_name=_patch_text(args.timezone),
            schedule_expr=_patch_text(args.schedule_expr),
            run_at=_patch_text(args.run_at),
            enabled=_patch_value(args.enabled),
            timeout_seconds=timeout_seconds,
            model_override=_patch_value(args.model_override),
            thinking_override=_patch_text(args.thinking_override),
            execution_policy_key=args.execution_policy_key,
            target_idea_id=_patch_text(args.target_idea_id),
            guidance=_optional_text(args.guidance),
            rationale=_optional_text(args.rationale),
        )
        payload = serialize_cycle(cycle)
    return {"updated": payload}


async def _action_delete(ctx: ManageCycleContext) -> dict[str, Any]:
    async with UnitOfWork() as uow:
        cycle = await _load_cycle(uow.session, ctx.actor, ctx.args.id)
        await async_delete_cycle(uow.session, cycle)
        event = cycle_change_event(cycle)
    publish_cycle_change(action="delete", **event)
    return {"deleted": {"id": ctx.args.id}}


async def _action_run(ctx: ManageCycleContext) -> dict[str, Any]:
    run_kind = normalize_cycle_run_kind(
        ctx.args.run_kind or OFF_SLOT_MATERIAL_ALERT_RUN_KIND
    )
    async with UnitOfWork() as uow:
        cycle = await _load_cycle(uow.session, ctx.actor, ctx.args.id)
        event = cycle_change_event(cycle)
    payload = await async_run_cycle_now(
        ctx.args.id,
        run_kind=run_kind,
        launch_context={
            "origin": AGENT_TRIGGERED_CYCLE_ORIGIN,
            "source": "manage_cycle",
            "actor_type": ctx.actor.principal_type,
            "actor_id": ctx.actor.source_id,
            "thread_id": getattr(_agent_context, "idea_id", None),
            "rationale": _optional_text(ctx.args.rationale),
        },
    )
    publish_cycle_change(action="run", **event)
    return {"run": payload}


async def _action_add_guidance(ctx: ManageCycleContext) -> dict[str, Any]:
    async with UnitOfWork() as uow:
        cycle = await _load_cycle(uow.session, ctx.actor, ctx.args.id)
        row = await async_add_guidance_to_cycle(
            uow.session,
            cycle,
            actor=ctx.actor,
            guidance=_optional_text(ctx.args.guidance),
            rationale=_optional_text(ctx.args.rationale),
        )
        return {"guidance": serialize_cycle_guidance(row)}


async def _action_add_output_target(ctx: ManageCycleContext) -> dict[str, Any]:
    async with UnitOfWork() as uow:
        cycle = await _load_cycle(uow.session, ctx.actor, ctx.args.id)
        row = await async_add_output_target_to_cycle(
            uow.session,
            cycle,
            actor=ctx.actor,
            target_type=_optional_text(ctx.args.output_target_type),
            target_id=_optional_text(ctx.args.output_target_id),
            label=_optional_text(ctx.args.output_target_label),
            config=ctx.args.output_target_config,
            rationale=_optional_text(ctx.args.rationale),
        )
        return {"output_target": serialize_cycle_output_target(row)}


async def _action_remove_output_target(ctx: ManageCycleContext) -> dict[str, Any]:
    output_target_id = _int_required(ctx.args.output_target_id, "output_target_id")
    async with UnitOfWork() as uow:
        cycle = await _load_cycle(uow.session, ctx.actor, ctx.args.id)
        row = await async_remove_output_target_from_cycle(
            uow.session,
            cycle,
            actor=ctx.actor,
            target_id=output_target_id,
            rationale=_optional_text(ctx.args.rationale),
        )
        if row is None:
            raise ValueError(f"Cycle output target {output_target_id} not found")
        return {"output_target": serialize_cycle_output_target(row)}


CYCLE_ACTIONS: dict[str, CycleAction] = {
    "list": _action_list,
    "usage_summary": _action_usage_summary,
    "create": _action_create,
    "update": _action_update,
    "delete": _action_delete,
    "run": _action_run,
    "add_guidance": _action_add_guidance,
    "add_output_target": _action_add_output_target,
    "remove_output_target": _action_remove_output_target,
}


def _tool_actor(user_id: str) -> CycleActor:
    org_id = getattr(_agent_context, "org_id", None)
    run_id = getattr(_agent_context, "run_id", None)
    if run_id is None:
        run = getattr(_agent_context, "run", None)
        run_id = getattr(run, "run_id", None)
    return CycleActor(
        user_id=user_id,
        org_id=str(org_id) if org_id else None,
        principal_type="agent",
        source_id=str(run_id or user_id),
    )


async def _load_cycle(session, actor: CycleActor, cycle_id: int | None) -> Cycle:
    if not cycle_id:
        raise ValueError("id is required")
    result = await session.scalars(
        select(Cycle).where(Cycle.id == cycle_id, *cycle_scope_conditions(actor))
    )
    cycle = result.first()
    if not cycle:
        raise ValueError(f"Cycle {cycle_id} not found")
    return cycle


async def _validate_target_idea(session, idea_id: str | None, actor: CycleActor) -> None:
    if not idea_id:
        return
    result = await session.execute(select(Idea.id).where(*target_idea_scope_conditions(idea_id, actor)))
    if not result.first():
        raise ValueError("target_idea_id must belong to the current workspace")


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _patch_value(value):
    return UNSET_CYCLE_FIELD if value is None else value


def _patch_text(value: str | None):
    return UNSET_CYCLE_FIELD if value is None else _optional_text(value)


def _int_required(value: str | None, field_name: str) -> int:
    text = _optional_text(value)
    if text is None:
        raise ValueError(f"{field_name} is required")
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be numeric") from exc


def _event_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "org_id": payload.get("org_id"),
        "user_id": payload.get("user_id"),
        "cycle_id": payload.get("id"),
        "target_idea_id": payload.get("target_idea_id"),
    }


__all__ = [name for name in globals() if not name.startswith("__")]
