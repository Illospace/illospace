"""Canonical Cycle mutation commands shared by API and agent tools."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from brain.platform.db.models.cycle import Cycle, CycleGuidance
from brain.systems.cycles.access import CycleActor
from brain.systems.cycles.behavior_policy import (
    CyclePolicyApplied,
    CyclePolicyPatch,
    UNSET_CYCLE_FIELD,
    async_apply_cycle_policy_change,
    async_preview_cycle_policy_change,
)
from brain.systems.cycles.common import (
    ILLO_LANE_EXECUTOR_BINDING,
    canonical_execution_mode,
    validate_cycle_timeout_seconds,
    validate_executor_binding,
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
from brain.systems.cycles.skill_refs import (
    validate_cycle_prompt,
    validate_cycle_skill_ids,
)


def _validated_max_concurrency(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_concurrency must be an integer >= 1")
    return value


async def async_create_cycle(
    session,
    *,
    actor: CycleActor,
    name: str,
    prompt: str | None,
    timezone_name: str,
    schedule_expr: str | None = None,
    run_at=None,
    enabled: bool = True,
    max_concurrency: int = 1,
    timeout_seconds: int | None = None,
    model_override: str | None = None,
    thinking_override: str | None = None,
    execution_policy_key: str | None = None,
    executor_binding: str = ILLO_LANE_EXECUTOR_BINDING,
    skill_ids: list[int] | None = None,
    target_idea_id: str | None = None,
    guidance: str | None = None,
    rationale: str | None = None,
) -> Cycle:
    monitoring_started_at = datetime.now(timezone.utc)
    validated_skill_ids = validate_cycle_skill_ids(skill_ids)
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
        prompt=validate_cycle_prompt(prompt, skill_ids=validated_skill_ids),
        schedule_expr=expr,
        timezone=tz_name,
        enabled=enabled,
        max_concurrency=_validated_max_concurrency(max_concurrency),
        timeout_seconds=validate_cycle_timeout_seconds(timeout_seconds),
        model_override=validate_model_override(model_override),
        thinking_override=validate_thinking_override(thinking_override),
        execution_policy_key=validated_execution_policy_key,
        execution_mode=canonical_execution_mode(),
        executor_binding=validate_executor_binding(executor_binding),
        skill_ids=validated_skill_ids,
        receipt_monitoring_started_at=monitoring_started_at,
        target_idea_id=target_idea_id,
        reopen_archived=True,
        next_run_at=compute_next_run_at(
            expr,
            tz_name,
            from_dt=monitoring_started_at,
        ),
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
    executor_binding=UNSET_CYCLE_FIELD,
    skill_ids=UNSET_CYCLE_FIELD,
    target_idea_id=UNSET_CYCLE_FIELD,
    guidance: str | None = None,
    rationale: str | None = None,
) -> Cycle:
    # Validate a stored key before any query or mutation. This preserves the
    # existing fail-fast repair signal for legacy rows with invalid keys.
    if _is_patch_field_set(execution_policy_key):
        validate_cycle_execution_policy_key(
            execution_policy_key
        )
    else:
        validate_cycle_execution_policy_key(
            getattr(cycle, "execution_policy_key", None)
        )
    patch = CyclePolicyPatch(
        name=_policy_field(name, ignore_none=True),
        prompt=_policy_field(prompt, ignore_none=True),
        timezone=_policy_field(timezone_name, ignore_none=True),
        schedule_expr=_policy_field(schedule_expr, ignore_none=True),
        run_at=_policy_field(run_at, ignore_none=True),
        enabled=_policy_field(enabled, ignore_none=True),
        max_concurrency=_policy_field(max_concurrency),
        timeout_seconds=_policy_field(timeout_seconds),
        model_override=_policy_field(model_override),
        thinking_override=_policy_field(thinking_override),
        execution_policy_key=_policy_field(execution_policy_key),
        executor_binding=_policy_field(executor_binding),
        skill_ids=_policy_field(skill_ids),
        target_idea_id=_policy_field(target_idea_id),
        guidance_additions=(
            [guidance] if guidance else UNSET_CYCLE_FIELD
        ),
    )
    preview = await async_preview_cycle_policy_change(
        session,
        actor=actor,
        cycle_id=cycle.id,
        proposal=patch,
    )
    result = await async_apply_cycle_policy_change(
        session,
        actor=actor,
        cycle_id=cycle.id,
        proposal=patch,
        expected_version=preview.before.version,
        preview_digest=preview.preview_digest,
        rationale=rationale or "Cycle updated.",
        source_reference=_command_source_reference(actor),
    )
    if not isinstance(result, CyclePolicyApplied):
        raise ValueError("Cycle policy changed while the update was being applied")
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
    clean_guidance = validate_nonempty_trimmed(guidance, "guidance")
    patch = CyclePolicyPatch(guidance_additions=[clean_guidance])
    preview = await async_preview_cycle_policy_change(
        session,
        actor=actor,
        cycle_id=cycle.id,
        proposal=patch,
    )
    result = await async_apply_cycle_policy_change(
        session,
        actor=actor,
        cycle_id=cycle.id,
        proposal=patch,
        expected_version=preview.before.version,
        preview_digest=preview.preview_digest,
        rationale=rationale or "Cycle guidance added.",
        source_reference=_command_source_reference(actor),
    )
    if not isinstance(result, CyclePolicyApplied):
        raise ValueError("Cycle policy changed while guidance was being added")
    rows = await session.scalars(
        select(CycleGuidance).where(
            CycleGuidance.cycle_id == cycle.id,
            CycleGuidance.revision_id == result.revision.id,
            CycleGuidance.guidance == clean_guidance,
            CycleGuidance.is_active.is_(True),
        )
    )
    return rows.first()


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


def _policy_field(value, *, ignore_none: bool = False):
    if value is UNSET_CYCLE_FIELD or (ignore_none and value is None):
        return UNSET_CYCLE_FIELD
    return value


def _command_source_reference(actor: CycleActor) -> str:
    return f"{actor.source_type}:{actor.revision_source_id}"


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
