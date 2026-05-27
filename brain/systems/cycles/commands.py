"""Canonical Cycle mutation commands shared by API and agent tools."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from brain.platform.db.models.cycle import Cycle
from brain.systems.cycles.common import (
    canonical_execution_mode,
    validate_nonempty_trimmed,
    validate_thinking_override,
)
from brain.systems.cycles.memory import (
    async_add_cycle_guidance,
    async_add_cycle_output_target,
    async_record_cycle_revision,
    async_remove_cycle_output_target,
)
from brain.systems.cycles.schedules import (
    build_one_time_schedule_expr,
    compute_next_run_at,
    validate_schedule_expr,
    validate_timezone_name,
)


@dataclass(frozen=True)
class CycleMutationActor:
    user_id: str
    org_id: str | None = None
    principal_type: str = "user"
    source_id: str | None = None

    @property
    def source_type(self) -> str:
        return self.principal_type or "user"

    @property
    def revision_source_id(self) -> str:
        return str(self.source_id or self.user_id)


async def async_create_cycle(
    session,
    *,
    actor: CycleMutationActor,
    name: str,
    prompt: str,
    timezone_name: str,
    schedule_expr: str | None = None,
    run_at=None,
    enabled: bool = True,
    model_override: str | None = None,
    thinking_override: str | None = None,
    target_idea_id: str | None = None,
    guidance: str | None = None,
    rationale: str | None = None,
) -> Cycle:
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
        model_override=(model_override or "").strip() or None,
        thinking_override=validate_thinking_override(thinking_override),
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
    actor: CycleMutationActor,
    name: str | None = None,
    prompt: str | None = None,
    timezone_name: str | None = None,
    schedule_expr: str | None = None,
    run_at=None,
    enabled: bool | None = None,
    model_override: str | None = None,
    model_override_provided: bool = False,
    thinking_override: str | None = None,
    thinking_override_provided: bool = False,
    target_idea_id: str | None = None,
    target_idea_provided: bool = False,
    guidance: str | None = None,
    rationale: str | None = None,
) -> Cycle:
    next_timezone = validate_timezone_name(timezone_name) if timezone_name is not None else cycle.timezone
    next_schedule_expr = cycle.schedule_expr
    if run_at is not None:
        next_schedule_expr = build_one_time_schedule_expr(run_at, next_timezone)
    elif schedule_expr is not None:
        next_schedule_expr = validate_schedule_expr(schedule_expr, next_timezone)

    if name is not None:
        cycle.name = validate_nonempty_trimmed(name, "name")
    if prompt is not None:
        cycle.prompt = validate_nonempty_trimmed(prompt, "prompt")
    cycle.timezone = next_timezone
    cycle.schedule_expr = next_schedule_expr
    if enabled is not None:
        cycle.enabled = enabled
    if model_override_provided:
        cycle.model_override = (model_override or "").strip() or None
    if thinking_override_provided:
        cycle.thinking_override = validate_thinking_override(thinking_override)
    if target_idea_provided:
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
    actor: CycleMutationActor,
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
    actor: CycleMutationActor,
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
    actor: CycleMutationActor,
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


async def _seed_default_output_targets(
    session,
    cycle: Cycle,
    *,
    actor: CycleMutationActor,
    revision_id: int,
) -> None:
    await async_add_cycle_output_target(
        session,
        cycle,
        target_type="cycle_ledger",
        target_id=str(cycle.id),
        label="Cycle ledger",
        source_type=actor.source_type,
        source_id=actor.revision_source_id,
        rationale="Every Cycle persists durable memory in its ledger.",
        revision_id=revision_id,
    )
    if cycle.target_idea_id:
        await async_add_cycle_output_target(
            session,
            cycle,
            target_type="thread",
            target_id=str(cycle.target_idea_id),
            label="Cycle thread",
            source_type=actor.source_type,
            source_id=actor.revision_source_id,
            rationale="Initial display thread for Cycle output.",
            revision_id=revision_id,
        )
