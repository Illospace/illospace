"""Rich read model for an effective Cycle behavior policy."""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime

from sqlalchemy import select

from brain.platform.db.models.cycle import (
    BehaviorChangeAudit,
    CycleOutputTarget,
    CycleRevision,
)
from brain.systems.cycles.access import CycleActor
from brain.systems.cycles.behavior_policy import (
    BehaviorChangeRecord,
    CyclePolicySnapshot,
    EffectiveCyclePolicy,
    _audit_target_conditions,
    _aware_utc,
    _effective_policy,
    _load_scoped_cycle,
    _record,
)

__all__ = [
    "CyclePolicyFieldSource",
    "EffectiveCyclePolicyReadModel",
    "async_read_effective_cycle_policy",
]


@dataclass(frozen=True)
class CyclePolicyFieldSource:
    field_name: str
    version: int
    cycle_revision_id: int | None
    actor_type: str | None
    actor_id: str | None
    source_reference: str | None
    rationale: str | None
    changed_at: datetime | None
    change_id: int | None


@dataclass(frozen=True)
class EffectiveCyclePolicyReadModel(EffectiveCyclePolicy):
    """Effective policy with all metadata required by read adapters."""

    output_targets: tuple[CycleOutputTarget, ...]
    source_revision: CycleRevision | None
    latest_change: BehaviorChangeRecord | None
    field_sources: tuple[CyclePolicyFieldSource, ...]


async def async_read_effective_cycle_policy(
    session,
    *,
    actor: CycleActor,
    cycle_id: int,
) -> EffectiveCyclePolicyReadModel:
    cycle = await _load_scoped_cycle(session, actor=actor, cycle_id=cycle_id)
    effective = await _effective_policy(session, cycle)
    source_revision = await session.scalar(
        select(CycleRevision)
        .where(CycleRevision.cycle_id == cycle.id)
        .order_by(CycleRevision.revision_number.desc(), CycleRevision.id.desc())
        .limit(1)
    )
    output_targets = tuple(
        (
            await session.scalars(
                select(CycleOutputTarget)
                .where(
                    CycleOutputTarget.cycle_id == cycle.id,
                    CycleOutputTarget.is_active.is_(True),
                )
                .order_by(
                    CycleOutputTarget.created_at.asc(),
                    CycleOutputTarget.id.asc(),
                )
            )
        ).all()
    )
    change_rows = list(
        (
            await session.scalars(
                select(BehaviorChangeAudit)
                .where(*_audit_target_conditions(cycle))
                .order_by(
                    BehaviorChangeAudit.version.desc(),
                    BehaviorChangeAudit.id.desc(),
                )
            )
        ).all()
    )
    latest_change = (
        _record(change_rows[0], current_snapshot=effective.snapshot)
        if change_rows
        else None
    )
    if change_rows:
        first_policy_revision_number = (
            select(CycleRevision.revision_number)
            .where(CycleRevision.id == change_rows[-1].cycle_revision_id)
            .scalar_subquery()
        )
        baseline_revision = await session.scalar(
            select(CycleRevision)
            .where(
                CycleRevision.cycle_id == cycle.id,
                CycleRevision.revision_number < first_policy_revision_number,
            )
            .order_by(
                CycleRevision.revision_number.desc(),
                CycleRevision.id.desc(),
            )
            .limit(1)
        )
    else:
        baseline_revision = source_revision
    field_sources = _field_sources(
        snapshot=effective.snapshot,
        baseline_revision=baseline_revision,
        change_rows=change_rows,
    )
    return EffectiveCyclePolicyReadModel(
        workspace_id=effective.workspace_id,
        policy_kind=effective.policy_kind,
        target_type=effective.target_type,
        target_id=effective.target_id,
        version=effective.version,
        revision_id=(
            source_revision.id
            if source_revision is not None
            else effective.revision_id
        ),
        snapshot=effective.snapshot,
        output_targets=output_targets,
        source_revision=source_revision,
        latest_change=latest_change,
        field_sources=field_sources,
    )


def _field_sources(
    *,
    snapshot: CyclePolicySnapshot,
    baseline_revision: CycleRevision | None,
    change_rows: list[BehaviorChangeAudit],
) -> tuple[CyclePolicyFieldSource, ...]:
    latest_by_field: dict[str, BehaviorChangeAudit] = {}
    for row in change_rows:
        for field_name in row.changed_fields or []:
            latest_by_field.setdefault(str(field_name), row)

    sources = []
    for snapshot_field in fields(snapshot):
        row = latest_by_field.get(snapshot_field.name)
        if row is not None:
            sources.append(
                CyclePolicyFieldSource(
                    field_name=snapshot_field.name,
                    version=row.version,
                    cycle_revision_id=row.cycle_revision_id,
                    actor_type=row.actor_type,
                    actor_id=row.actor_id,
                    source_reference=row.source_reference,
                    rationale=row.rationale,
                    changed_at=_aware_utc(row.applied_at),
                    change_id=row.id,
                )
            )
            continue
        sources.append(
            CyclePolicyFieldSource(
                field_name=snapshot_field.name,
                version=0,
                cycle_revision_id=(
                    baseline_revision.id
                    if baseline_revision is not None
                    else None
                ),
                actor_type=(
                    baseline_revision.source_type
                    if baseline_revision is not None
                    else None
                ),
                actor_id=(
                    str(baseline_revision.source_id)
                    if baseline_revision is not None
                    and baseline_revision.source_id is not None
                    else None
                ),
                source_reference=(
                    f"cycle_revision:{baseline_revision.id}"
                    if baseline_revision is not None
                    else None
                ),
                rationale=(
                    baseline_revision.rationale
                    if baseline_revision is not None
                    else None
                ),
                changed_at=(
                    _aware_utc(baseline_revision.created_at)
                    if baseline_revision is not None
                    else None
                ),
                change_id=None,
            )
        )
    return tuple(sources)
