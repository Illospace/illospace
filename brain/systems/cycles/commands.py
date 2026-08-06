"""Canonical Cycle mutation commands shared by API and agent tools."""
from __future__ import annotations

from datetime import datetime, timezone

from brain.platform.db.models.cycle import Cycle
from brain.systems.cycles.access import CycleActor
from brain.systems.cycles.common import (
    canonical_execution_mode,
    validate_cycle_timeout_seconds,
    validate_model_override,
    validate_nonempty_trimmed,
    validate_thinking_override,
)
from brain.systems.cycles.memory import (
    async_add_cycle_guidance,
    async_add_cycle_output_target,
    async_record_cycle_revision,
    async_remove_cycle_output_target,
)
from brain.systems.cycles.execution_policy_registry import (
    validate_cycle_execution_policy_key,
)
from brain.systems.cycles.output_targets import default_output_target_specs
from brain.systems.cycles.schedules import (
    build_one_time_schedule_expr,
    compute_next_run_at,
    validate_schedule_expr,
    validate_timezone_name,
)


class _UnsetCycleField:
    pass


UNSET_CYCLE_FIELD = _UnsetCycleField()


def _validated_max_concurrency(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_concurrency must be an integer >= 1")
    return value


async def async_create_cycle(
    session,
    *,
    actor: CycleActor,
    name: str,
    prompt: str,
    timezone_name: str,
    schedule_expr: str | None = None,
    run_at=None,
    enabled: bool = True,
    max_concurrency: int = 1,
    timeout_seconds: int | None = None,
    model_override: str | None = None,
    thinking_override: str | None = None,
    execution_policy_key: str | None = None,
    target_idea_id: str | None = None,
    guidance: str | None = None,
    rationale: str | None = None,
) -> Cycle:
    validated_execution_policy_key = validate_cycle_execution_policy_key(
        execution_policy_key
    )
    tz_name = validate_timezone_name(timezone_name)
    expr = _schedule_expr(schedule_expr=schedule_expr, run_at=run_at, timezone_name=tz_name)
    cycle = Cycle(
        user_id=actor.user_id,
        org_id=actor.org_id,
        creator_type=actor.source_type,
        creator_id=actor.revision_source_id,
        maintainer_type=actor.source_type,
        maintainer_id=actor.revision_source_id,
        name=validate_nonempty_trimmed(name, "name"),
        prompt=validate_nonempty_trimmed(prompt, "prompt"),
        schedule_expr=expr,
        timezone=tz_name,
        enabled=enabled,
        max_concurrency=_validated_max_concurrency(max_concurrency),
        timeout_seconds=validate_cycle_timeout_seconds(timeout_seconds),
        model_override=validate_model_override(model_override),
        thinking_override=validate_thinking_override(thinking_override),
        execution_policy_key=validated_execution_policy_key,
        execution_mode=canonical_execution_mode(),
        target_idea_id=target_idea_id,
        reopen_archived=True,
        next_run_at=compute_next_run_at(expr, tz_name),
    )
    session.add(cycle)
    await session.flush()
    revision = await async_record_cycle_revision(
        session,
        cycle,
        source_type=actor.source_type,
        source_id=actor.revision_source_id,
        rationale=rationale or "Initial Cycle definition.",
    )
    await _seed_default_output_targets(
        session,
        cycle,
        actor=actor,
        revision_id=revision.id,
    )
    if guidance:
        await async_add_cycle_guidance(
            session,
            cycle,
            guidance=guidance,
            source_type=actor.source_type,
            source_id=actor.revision_source_id,
            rationale=rationale,
            revision_id=revision.id,
        )
    return cycle


async def async_update_cycle(
    session,
    cycle: Cycle,
    *,
    actor: CycleActor,
    name: str | None = None,
    prompt: str | None = None,
    timezone_name: str | None = None,
    schedule_expr: str | None = None,
    run_at=UNSET_CYCLE_FIELD,
    enabled=UNSET_CYCLE_FIELD,
    max_concurrency=UNSET_CYCLE_FIELD,
    timeout_seconds=UNSET_CYCLE_FIELD,
    model_override=UNSET_CYCLE_FIELD,
    thinking_override=UNSET_CYCLE_FIELD,
    execution_policy_key=UNSET_CYCLE_FIELD,
    target_idea_id=UNSET_CYCLE_FIELD,
    guidance: str | None = None,
    rationale: str | None = None,
) -> Cycle:
    if _is_patch_field_set(execution_policy_key):
        next_execution_policy_key = validate_cycle_execution_policy_key(
            execution_policy_key
        )
    else:
        validate_cycle_execution_policy_key(
            getattr(cycle, "execution_policy_key", None)
        )
        next_execution_policy_key = UNSET_CYCLE_FIELD
    next_timezone = validate_timezone_name(timezone_name) if _has_patch_value(timezone_name) else cycle.timezone
    next_schedule_expr = cycle.schedule_expr
    next_model_override = (
        validate_model_override(model_override)
        if _is_patch_field_set(model_override)
        else UNSET_CYCLE_FIELD
    )
    next_thinking_override = (
        validate_thinking_override(thinking_override)
        if _is_patch_field_set(thinking_override)
        else UNSET_CYCLE_FIELD
    )
    next_timeout_seconds = (
        validate_cycle_timeout_seconds(timeout_seconds)
        if _is_patch_field_set(timeout_seconds)
        else UNSET_CYCLE_FIELD
    )
    if _has_patch_value(run_at):
        next_schedule_expr = build_one_time_schedule_expr(run_at, next_timezone)
    elif _has_patch_value(schedule_expr):
        next_schedule_expr = validate_schedule_expr(schedule_expr, next_timezone)

    if _has_patch_value(name):
        cycle.name = validate_nonempty_trimmed(name, "name")
    if _has_patch_value(prompt):
        cycle.prompt = validate_nonempty_trimmed(prompt, "prompt")
    cycle.timezone = next_timezone
    cycle.schedule_expr = next_schedule_expr
    if _is_patch_field_set(enabled) and enabled is not None:
        cycle.enabled = enabled
    if _is_patch_field_set(max_concurrency):
        cycle.max_concurrency = _validated_max_concurrency(max_concurrency)
    if _is_patch_field_set(next_timeout_seconds):
        cycle.timeout_seconds = next_timeout_seconds
    if _is_patch_field_set(next_model_override):
        cycle.model_override = next_model_override
    if _is_patch_field_set(next_thinking_override):
        cycle.thinking_override = next_thinking_override
    if _is_patch_field_set(next_execution_policy_key):
        cycle.execution_policy_key = next_execution_policy_key
    if _is_patch_field_set(target_idea_id):
        cycle.target_idea_id = target_idea_id

    cycle.execution_mode = canonical_execution_mode()
    cycle.reopen_archived = True
    cycle.next_run_at = compute_next_run_at(cycle.schedule_expr, cycle.timezone)
    cycle.updated_at = datetime.now(timezone.utc)

    revision = await async_record_cycle_revision(
        session,
        cycle,
        source_type=actor.source_type,
        source_id=actor.revision_source_id,
        rationale=rationale or "Cycle updated.",
    )
    if guidance:
        await async_add_cycle_guidance(
            session,
            cycle,
            guidance=guidance,
            source_type=actor.source_type,
            source_id=actor.revision_source_id,
            rationale=rationale,
            revision_id=revision.id,
        )
    return cycle


async def async_delete_cycle(session, cycle: Cycle) -> Cycle:
    cycle.enabled = False
    cycle.deleted_at = datetime.now(timezone.utc)
    await session.flush()
    return cycle


async def async_add_guidance_to_cycle(
    session,
    cycle: Cycle,
    *,
    actor: CycleActor,
    guidance: str,
    rationale: str | None = None,
):
    revision = await async_record_cycle_revision(
        session,
        cycle,
        source_type=actor.source_type,
        source_id=actor.revision_source_id,
        rationale=rationale or "Cycle guidance added.",
    )
    return await async_add_cycle_guidance(
        session,
        cycle,
        guidance=guidance,
        source_type=actor.source_type,
        source_id=actor.revision_source_id,
        rationale=rationale,
        revision_id=revision.id,
    )


async def async_add_output_target_to_cycle(
    session,
    cycle: Cycle,
    *,
    actor: CycleActor,
    target_type: str,
    target_id: str | None = None,
    label: str | None = None,
    config: dict | None = None,
    rationale: str | None = None,
):
    revision = await async_record_cycle_revision(
        session,
        cycle,
        source_type=actor.source_type,
        source_id=actor.revision_source_id,
        rationale=rationale or "Cycle output target added.",
    )
    return await async_add_cycle_output_target(
        session,
        cycle,
        target_type=target_type,
        target_id=target_id,
        label=label,
        config=config,
        source_type=actor.source_type,
        source_id=actor.revision_source_id,
        rationale=rationale,
        revision_id=revision.id,
    )


async def async_remove_output_target_from_cycle(
    session,
    cycle: Cycle,
    *,
    actor: CycleActor,
    target_id: int,
    rationale: str | None = None,
):
    revision = await async_record_cycle_revision(
        session,
        cycle,
        source_type=actor.source_type,
        source_id=actor.revision_source_id,
        rationale=rationale or "Cycle output target removed.",
    )
    return await async_remove_cycle_output_target(
        session,
        cycle,
        target_id=target_id,
        source_type=actor.source_type,
        source_id=actor.revision_source_id,
        rationale=rationale,
        revision_id=revision.id,
    )


def cycle_change_event(cycle: Cycle) -> dict:
    return {
        "org_id": cycle.org_id,
        "user_id": cycle.user_id,
        "cycle_id": cycle.id,
        "target_idea_id": cycle.target_idea_id,
    }


def _schedule_expr(*, schedule_expr: str | None, run_at, timezone_name: str) -> str:
    if run_at is not None:
        return build_one_time_schedule_expr(run_at, timezone_name)
    if schedule_expr:
        return validate_schedule_expr(schedule_expr, timezone_name)
    raise ValueError("schedule_expr or run_at is required")


def _is_patch_field_set(value) -> bool:
    return value is not UNSET_CYCLE_FIELD


def _has_patch_value(value) -> bool:
    return _is_patch_field_set(value) and value is not None


async def _seed_default_output_targets(
    session,
    cycle: Cycle,
    *,
    actor: CycleActor,
    revision_id: int,
) -> None:
    for spec in default_output_target_specs(
        cycle,
        source_type=actor.source_type,
        ledger_rationale="Every Cycle persists durable memory in its ledger.",
        thread_rationale="Initial display thread for Cycle output.",
    ):
        await async_add_cycle_output_target(
            session,
            cycle,
            target_type=spec.target_type,
            target_id=spec.target_id,
            label=spec.label,
            config=spec.config,
            source_type=actor.source_type,
            source_id=actor.revision_source_id,
            rationale=spec.rationale,
            revision_id=revision_id,
        )
