"""Reviewed Cycle behavior-policy commands and their immutable audit envelope.

This module is the only write path for an existing Cycle's behavior fields and
active guidance set. API and agent adapters can differ, but both must preview
and apply through this contract.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import (
    Any,
    ClassVar,
    Final,
    Mapping,
    TypeAlias,
    TypeGuard,
    TypeVar,
    cast,
)

from sqlalchemy import func, select

from brain.kernel.common.serialization import jsonable
from brain.platform.db.models.cycle import (
    BehaviorChangeAudit,
    Cycle,
    CycleGuidance,
    CycleRevision,
)
from brain.platform.db.models.idea import Idea
from brain.systems.cycles.access import (
    CycleActor,
    cycle_scope_conditions,
    target_idea_scope_conditions,
)
from brain.systems.cycles.common import (
    canonical_execution_mode,
    validate_cycle_timeout_seconds,
    validate_model_override,
    validate_nonempty_trimmed,
    validate_thinking_override,
)
from brain.systems.cycles.events import publish_cycle_change_strict
from brain.systems.cycles.execution_policy_registry import (
    validate_cycle_execution_policy_key,
)
from brain.systems.cycles.memory import async_record_cycle_revision
from brain.systems.cycles.schedules import (
    build_one_time_schedule_expr,
    compute_next_run_at,
    validate_schedule_expr,
    validate_timezone_name,
)

__all__ = [
    "BehaviorChangeRecord",
    "CyclePolicyApplied",
    "CyclePolicyApplyResult",
    "CyclePolicyConflict",
    "CyclePolicyPatch",
    "CyclePolicyPreview",
    "CyclePolicySnapshot",
    "EffectiveCyclePolicy",
    "UNSET_CYCLE_FIELD",
    "async_apply_cycle_policy_change",
    "async_apply_cycle_policy_revert",
    "async_list_cycle_policy_history",
    "async_preview_cycle_policy_change",
    "async_preview_cycle_policy_revert",
    "async_read_effective_cycle_policy",
]

CYCLE_POLICY_KIND = "cycle"
CYCLE_TARGET_TYPE = "cycle"

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
CycleGuidanceValues: TypeAlias = list[str] | tuple[str, ...]
_PatchValueT = TypeVar("_PatchValueT")


class _UnsetCycleField:
    __slots__ = ()


UNSET_CYCLE_FIELD: Final = _UnsetCycleField()
_CyclePatchField: TypeAlias = _PatchValueT | _UnsetCycleField


@dataclass(frozen=True)
class CyclePolicySnapshot:
    """One validated Cycle policy, with versioned JSON persistence.

    Fields stay required so schema growth makes typed constructor sites fail
    until their live-Cycle mapping and normalization are updated here.
    """

    SNAPSHOT_VERSION: ClassVar[int] = 1

    name: str
    prompt: str
    schedule_expr: str
    timezone: str
    enabled: bool
    max_concurrency: int
    timeout_seconds: int | None
    retry_policy: dict[str, JsonValue]
    model_override: str | None
    thinking_override: str | None
    execution_policy_key: str | None
    target_idea_id: str | None
    guidance: list[str]

    @classmethod
    def from_cycle(
        cls,
        cycle: Cycle,
        guidance_rows: list[CycleGuidance],
    ) -> CyclePolicySnapshot:
        """Build the effective policy from the live Cycle read model."""

        return cls(
            name=cycle.name,
            prompt=cycle.prompt,
            schedule_expr=cycle.schedule_expr,
            timezone=cycle.timezone,
            enabled=bool(cycle.enabled),
            max_concurrency=max(int(cycle.max_concurrency or 1), 1),
            timeout_seconds=cycle.timeout_seconds,
            retry_policy=dict(cycle.retry_policy or {}),
            model_override=cycle.model_override,
            thinking_override=cycle.thinking_override,
            execution_policy_key=cycle.execution_policy_key,
            target_idea_id=(
                str(cycle.target_idea_id)
                if cycle.target_idea_id is not None
                else None
            ),
            guidance=[row.guidance for row in guidance_rows],
        ).validated()

    def apply_to(self, cycle: Cycle) -> None:
        """Apply this policy explicitly to its live Cycle fields."""

        cycle.name = self.name
        cycle.prompt = self.prompt
        cycle.schedule_expr = self.schedule_expr
        cycle.timezone = self.timezone
        cycle.enabled = self.enabled
        cycle.max_concurrency = self.max_concurrency
        cycle.timeout_seconds = self.timeout_seconds
        cycle.retry_policy = deepcopy(self.retry_policy)
        cycle.model_override = self.model_override
        cycle.thinking_override = self.thinking_override
        cycle.execution_policy_key = self.execution_policy_key
        cycle.target_idea_id = self.target_idea_id
        cycle.execution_mode = canonical_execution_mode()
        cycle.reopen_archived = True
        cycle.next_run_at = compute_next_run_at(
            cycle.schedule_expr,
            cycle.timezone,
        )
        cycle.updated_at = datetime.now(timezone.utc)

    def validated(self) -> CyclePolicySnapshot:
        """Return a normalized snapshot or raise for an invalid policy."""

        timezone_name = validate_timezone_name(self.timezone)
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if not isinstance(self.retry_policy, dict):
            raise ValueError("retry_policy must be an object")
        if not isinstance(self.guidance, (list, tuple)):
            raise ValueError("guidance must be a list of strings")
        retry_policy = cast(
            dict[str, JsonValue],
            jsonable(deepcopy(self.retry_policy)),
        )
        return CyclePolicySnapshot(
            name=validate_nonempty_trimmed(self.name, "name"),
            prompt=validate_nonempty_trimmed(self.prompt, "prompt"),
            schedule_expr=validate_schedule_expr(
                self.schedule_expr,
                timezone_name,
            ),
            timezone=timezone_name,
            enabled=self.enabled,
            max_concurrency=_validated_max_concurrency(self.max_concurrency),
            timeout_seconds=validate_cycle_timeout_seconds(self.timeout_seconds),
            retry_policy=retry_policy,
            model_override=validate_model_override(self.model_override),
            thinking_override=validate_thinking_override(self.thinking_override),
            execution_policy_key=validate_cycle_execution_policy_key(
                self.execution_policy_key
            ),
            target_idea_id=(
                str(self.target_idea_id)
                if self.target_idea_id is not None
                else None
            ),
            guidance=sorted(
                validate_nonempty_trimmed(value, "guidance")
                for value in self.guidance
            ),
        )

    def encode(self) -> dict[str, Any]:
        """Encode the current schema for the JSON database boundary."""

        encoded = {
            field.name: jsonable(deepcopy(getattr(self, field.name)))
            for field in fields(self)
        }
        return {"snapshot_version": self.SNAPSHOT_VERSION, **encoded}

    @classmethod
    def decode(
        cls,
        snapshot: Mapping[str, Any],
        *,
        current: CyclePolicySnapshot,
    ) -> CyclePolicySnapshot:
        """Decode a legacy or versioned shape against today's effective policy.

        Positive versions remain readable for forward compatibility. Unknown
        fields are ignored, and missing fields retain their current values.
        """

        if not isinstance(snapshot, Mapping):
            raise ValueError("Cycle policy snapshot must be an object")
        snapshot_version = snapshot.get("snapshot_version")
        if snapshot_version is not None and (
            isinstance(snapshot_version, bool)
            or not isinstance(snapshot_version, int)
            or snapshot_version < 1
        ):
            raise ValueError("Cycle policy snapshot has an invalid version")

        validated_current = current.validated()
        decoded = {
            field.name: deepcopy(
                snapshot.get(field.name, getattr(validated_current, field.name))
            )
            for field in fields(cls)
        }
        return cls(**decoded).validated()


@dataclass(frozen=True)
class CyclePolicyPatch:
    """Typed changes to Cycle behavior, never arbitrary table columns."""

    name: _CyclePatchField[str | None] = UNSET_CYCLE_FIELD
    prompt: _CyclePatchField[str | None] = UNSET_CYCLE_FIELD
    timezone_name: _CyclePatchField[str | None] = UNSET_CYCLE_FIELD
    schedule_expr: _CyclePatchField[str | None] = UNSET_CYCLE_FIELD
    run_at: _CyclePatchField[str | datetime | None] = UNSET_CYCLE_FIELD
    enabled: _CyclePatchField[bool | None] = UNSET_CYCLE_FIELD
    max_concurrency: _CyclePatchField[int] = UNSET_CYCLE_FIELD
    timeout_seconds: _CyclePatchField[int | None] = UNSET_CYCLE_FIELD
    retry_policy: _CyclePatchField[dict[str, JsonValue]] = UNSET_CYCLE_FIELD
    model_override: _CyclePatchField[str | None] = UNSET_CYCLE_FIELD
    thinking_override: _CyclePatchField[str | None] = UNSET_CYCLE_FIELD
    execution_policy_key: _CyclePatchField[str | None] = UNSET_CYCLE_FIELD
    target_idea_id: _CyclePatchField[str | None] = UNSET_CYCLE_FIELD
    guidance: _CyclePatchField[CycleGuidanceValues] = UNSET_CYCLE_FIELD
    guidance_additions: _CyclePatchField[CycleGuidanceValues] = UNSET_CYCLE_FIELD


@dataclass(frozen=True)
class _CyclePolicyRevert:
    """Typed adapter request used only by the revert path."""

    change_id: int


@dataclass(frozen=True)
class EffectiveCyclePolicy:
    workspace_id: str
    policy_kind: str
    target_type: str
    target_id: str
    version: int
    revision_id: int | None
    snapshot: CyclePolicySnapshot


@dataclass(frozen=True)
class CyclePolicyPreview:
    before: EffectiveCyclePolicy
    after_snapshot: CyclePolicySnapshot
    changed_fields: tuple[str, ...]
    preview_digest: str
    reverted_from_id: int | None = None


@dataclass(frozen=True)
class _CyclePolicyPreviewDigestPayload:
    workspace_id: str
    policy_kind: str
    target_type: str
    target_id: str
    expected_version: int
    before_snapshot: CyclePolicySnapshot
    after_snapshot: CyclePolicySnapshot
    changed_fields: tuple[str, ...]
    reverted_from_id: int | None


@dataclass(frozen=True)
class BehaviorChangeRecord:
    id: int
    workspace_id: str
    policy_kind: str
    target_type: str
    target_id: str
    version: int
    actor_type: str
    actor_id: str
    source_reference: str
    rationale: str
    before_snapshot: CyclePolicySnapshot
    after_snapshot: CyclePolicySnapshot
    changed_fields: tuple[str, ...]
    cycle_revision_id: int
    applied_at: datetime
    reverted_from_id: int | None


@dataclass(frozen=True)
class CyclePolicyApplied:
    effective_policy: EffectiveCyclePolicy
    change: BehaviorChangeRecord
    revision: CycleRevision


@dataclass(frozen=True)
class CyclePolicyConflict:
    reason: str
    latest_effective_policy: EffectiveCyclePolicy


CyclePolicyApplyResult = CyclePolicyApplied | CyclePolicyConflict


async def async_read_effective_cycle_policy(
    session,
    *,
    actor: CycleActor,
    cycle_id: int,
) -> EffectiveCyclePolicy:
    cycle = await _load_scoped_cycle(session, actor=actor, cycle_id=cycle_id)
    return await _effective_policy(session, cycle)


async def async_preview_cycle_policy_change(
    session,
    *,
    actor: CycleActor,
    cycle_id: int,
    proposal: CyclePolicyPatch,
) -> CyclePolicyPreview:
    cycle = await _load_scoped_cycle(session, actor=actor, cycle_id=cycle_id)
    current = await _effective_policy(session, cycle)
    return await _preview_for_cycle(
        session,
        actor=actor,
        cycle=cycle,
        current=current,
        proposal=proposal,
    )


async def async_apply_cycle_policy_change(
    session,
    *,
    actor: CycleActor,
    cycle_id: int,
    proposal: CyclePolicyPatch | _CyclePolicyRevert,
    expected_version: int,
    preview_digest: str,
    rationale: str,
    source_reference: str,
    reverted_from_id: int | None = None,
) -> CyclePolicyApplyResult:
    """Apply one reviewed change inside the caller's transaction.

    A savepoint keeps the policy write set atomic even when a caller catches an
    apply-time exception and continues using its outer transaction. The Cycle
    row lock serializes version allocation on PostgreSQL.
    """

    clean_rationale = validate_nonempty_trimmed(rationale, "rationale")
    clean_source_reference = validate_nonempty_trimmed(
        source_reference,
        "source_reference",
    )
    if isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise ValueError("expected_version must be an integer")
    clean_digest = validate_nonempty_trimmed(preview_digest, "preview_digest")

    async with session.begin_nested():
        cycle = await _load_scoped_cycle(
            session,
            actor=actor,
            cycle_id=cycle_id,
            for_update=True,
        )
        current = await _effective_policy(session, cycle)
        if current.version != expected_version:
            return CyclePolicyConflict(
                reason="stale_version",
                latest_effective_policy=current,
            )

        if reverted_from_id is not None and not isinstance(
            proposal,
            _CyclePolicyRevert,
        ):
            await _load_revert_source(
                session,
                cycle=cycle,
                change_id=reverted_from_id,
            )

        preview = await _preview_for_cycle(
            session,
            actor=actor,
            cycle=cycle,
            current=current,
            proposal=proposal,
            reverted_from_id=reverted_from_id,
        )
        if preview.preview_digest != clean_digest:
            return CyclePolicyConflict(
                reason="stale_preview_digest",
                latest_effective_policy=current,
            )
        if not preview.changed_fields:
            raise ValueError("proposed change does not change Cycle policy")

        preview.after_snapshot.apply_to(cycle)
        cycle.maintainer_type = actor.source_type
        cycle.maintainer_id = actor.revision_source_id
        revision = await async_record_cycle_revision(
            session,
            cycle,
            source_type=actor.source_type,
            source_id=actor.revision_source_id,
            rationale=clean_rationale,
        )
        await _replace_active_guidance(
            session,
            cycle=cycle,
            desired_guidance=preview.after_snapshot.guidance,
            actor=actor,
            rationale=clean_rationale,
            revision_id=revision.id,
        )

        change = BehaviorChangeAudit(
            workspace_id=current.workspace_id,
            policy_kind=CYCLE_POLICY_KIND,
            target_type=CYCLE_TARGET_TYPE,
            target_id=str(cycle.id),
            version=current.version + 1,
            actor_type=actor.source_type,
            actor_id=actor.revision_source_id,
            source_reference=clean_source_reference,
            rationale=clean_rationale,
            before_snapshot=current.snapshot.encode(),
            after_snapshot=preview.after_snapshot.encode(),
            changed_fields=list(preview.changed_fields),
            cycle_revision_id=revision.id,
            reverted_from_id=reverted_from_id,
            applied_at=datetime.now(timezone.utc),
        )
        session.add(change)
        await session.flush()

        publish_cycle_change_strict(
            action="update",
            org_id=cycle.org_id,
            user_id=cycle.user_id,
            cycle_id=cycle.id,
            target_idea_id=cycle.target_idea_id,
        )

        effective = EffectiveCyclePolicy(
            workspace_id=current.workspace_id,
            policy_kind=CYCLE_POLICY_KIND,
            target_type=CYCLE_TARGET_TYPE,
            target_id=str(cycle.id),
            version=change.version,
            revision_id=revision.id,
            snapshot=deepcopy(preview.after_snapshot),
        )
        return CyclePolicyApplied(
            effective_policy=effective,
            change=_record(change, current_snapshot=preview.after_snapshot),
            revision=revision,
        )


async def async_list_cycle_policy_history(
    session,
    *,
    actor: CycleActor,
    cycle_id: int,
    limit: int = 100,
) -> list[BehaviorChangeRecord]:
    cycle = await _load_scoped_cycle(session, actor=actor, cycle_id=cycle_id)
    current = await _effective_policy(session, cycle)
    clean_limit = max(1, min(int(limit), 500))
    rows = list(
        (
            await session.scalars(
                select(BehaviorChangeAudit)
                .where(*_audit_target_conditions(cycle))
                .order_by(BehaviorChangeAudit.version.desc())
                .limit(clean_limit)
            )
        ).all()
    )
    return [
        _record(row, current_snapshot=current.snapshot)
        for row in rows
    ]


async def async_preview_cycle_policy_revert(
    session,
    *,
    actor: CycleActor,
    cycle_id: int,
    change_id: int,
) -> CyclePolicyPreview:
    cycle = await _load_scoped_cycle(session, actor=actor, cycle_id=cycle_id)
    current = await _effective_policy(session, cycle)
    return await _preview_for_cycle(
        session,
        actor=actor,
        cycle=cycle,
        current=current,
        proposal=_CyclePolicyRevert(change_id),
        reverted_from_id=change_id,
    )


async def async_apply_cycle_policy_revert(
    session,
    *,
    actor: CycleActor,
    cycle_id: int,
    change_id: int,
    expected_version: int,
    preview_digest: str,
    rationale: str,
    source_reference: str,
) -> CyclePolicyApplyResult:
    return await async_apply_cycle_policy_change(
        session,
        actor=actor,
        cycle_id=cycle_id,
        proposal=_CyclePolicyRevert(change_id),
        expected_version=expected_version,
        preview_digest=preview_digest,
        rationale=rationale,
        source_reference=source_reference,
        reverted_from_id=change_id,
    )


async def _load_scoped_cycle(
    session,
    *,
    actor: CycleActor,
    cycle_id: int,
    for_update: bool = False,
) -> Cycle:
    statement = select(Cycle).where(
        Cycle.id == cycle_id,
        *cycle_scope_conditions(actor),
    )
    if for_update:
        statement = statement.with_for_update()
    cycle = (await session.scalars(statement)).first()
    if cycle is None:
        raise ValueError("Cycle not found")
    return cycle


async def _effective_policy(session, cycle: Cycle) -> EffectiveCyclePolicy:
    guidance_rows = list(
        (
            await session.scalars(
                select(CycleGuidance)
                .where(
                    CycleGuidance.cycle_id == cycle.id,
                    CycleGuidance.is_active.is_(True),
                )
                .order_by(CycleGuidance.created_at.asc(), CycleGuidance.id.asc())
            )
        ).all()
    )
    version = int(
        (
            await session.scalar(
                select(func.coalesce(func.max(BehaviorChangeAudit.version), 0)).where(
                    *_audit_target_conditions(cycle)
                )
            )
        )
        or 0
    )
    revision_id = await session.scalar(
        select(CycleRevision.id)
        .where(CycleRevision.cycle_id == cycle.id)
        .order_by(CycleRevision.revision_number.desc(), CycleRevision.id.desc())
        .limit(1)
    )
    return EffectiveCyclePolicy(
        workspace_id=_workspace_id(cycle),
        policy_kind=CYCLE_POLICY_KIND,
        target_type=CYCLE_TARGET_TYPE,
        target_id=str(cycle.id),
        version=version,
        revision_id=revision_id,
        snapshot=CyclePolicySnapshot.from_cycle(cycle, guidance_rows),
    )


async def _preview_for_cycle(
    session,
    *,
    actor: CycleActor,
    cycle: Cycle,
    current: EffectiveCyclePolicy,
    proposal: CyclePolicyPatch | _CyclePolicyRevert,
    reverted_from_id: int | None = None,
) -> CyclePolicyPreview:
    if isinstance(proposal, _CyclePolicyRevert):
        source = await _load_revert_source(
            session,
            cycle=cycle,
            change_id=proposal.change_id,
        )
        after = CyclePolicySnapshot.decode(
            source.before_snapshot,
            current=current.snapshot,
        )
    else:
        after = _project_patch(current.snapshot, proposal)
    if after.target_idea_id != current.snapshot.target_idea_id:
        await _validate_target_idea(
            session,
            actor=actor,
            target_idea_id=after.target_idea_id,
        )
    changed_fields = tuple(
        sorted(
            field.name
            for field in fields(after)
            if getattr(current.snapshot, field.name) != getattr(after, field.name)
        )
    )
    digest = _preview_digest(
        current=current,
        after_snapshot=after,
        changed_fields=changed_fields,
        reverted_from_id=reverted_from_id,
    )
    return CyclePolicyPreview(
        before=current,
        after_snapshot=after,
        changed_fields=changed_fields,
        preview_digest=digest,
        reverted_from_id=reverted_from_id,
    )


def _project_patch(
    current_snapshot: CyclePolicySnapshot,
    patch: CyclePolicyPatch,
) -> CyclePolicySnapshot:
    after = deepcopy(current_snapshot)
    timezone_name = after.timezone
    if _set(patch.timezone_name) and patch.timezone_name is not None:
        timezone_name = validate_timezone_name(patch.timezone_name)
        after = replace(after, timezone=timezone_name)

    if _set(patch.run_at) and patch.run_at is not None:
        after = replace(
            after,
            schedule_expr=build_one_time_schedule_expr(
                patch.run_at,
                timezone_name,
            ),
        )
    elif _set(patch.schedule_expr) and patch.schedule_expr is not None:
        after = replace(
            after,
            schedule_expr=validate_schedule_expr(
                patch.schedule_expr,
                timezone_name,
            ),
        )
    elif after.timezone != current_snapshot.timezone:
        after = replace(
            after,
            schedule_expr=validate_schedule_expr(
                after.schedule_expr,
                timezone_name,
            ),
        )

    if _set(patch.name) and patch.name is not None:
        after = replace(
            after,
            name=validate_nonempty_trimmed(patch.name, "name"),
        )
    if _set(patch.prompt) and patch.prompt is not None:
        after = replace(
            after,
            prompt=validate_nonempty_trimmed(patch.prompt, "prompt"),
        )

    if _set(patch.enabled) and patch.enabled is not None:
        if not isinstance(patch.enabled, bool):
            raise ValueError("enabled must be a boolean")
        after = replace(after, enabled=patch.enabled)
    if _set(patch.max_concurrency):
        after = replace(
            after,
            max_concurrency=_validated_max_concurrency(patch.max_concurrency),
        )
    if _set(patch.timeout_seconds):
        after = replace(
            after,
            timeout_seconds=validate_cycle_timeout_seconds(patch.timeout_seconds),
        )
    if _set(patch.retry_policy):
        if not isinstance(patch.retry_policy, dict):
            raise ValueError("retry_policy must be an object")
        after = replace(
            after,
            retry_policy=cast(
                dict[str, JsonValue],
                jsonable(deepcopy(patch.retry_policy)),
            ),
        )
    if _set(patch.model_override):
        after = replace(
            after,
            model_override=validate_model_override(patch.model_override),
        )
    if _set(patch.thinking_override):
        after = replace(
            after,
            thinking_override=validate_thinking_override(
                patch.thinking_override
            ),
        )
    if _set(patch.execution_policy_key):
        after = replace(
            after,
            execution_policy_key=validate_cycle_execution_policy_key(
                patch.execution_policy_key
            ),
        )
    else:
        validate_cycle_execution_policy_key(after.execution_policy_key)
    if _set(patch.target_idea_id):
        after = replace(
            after,
            target_idea_id=(
                str(patch.target_idea_id)
                if patch.target_idea_id is not None
                else None
            ),
        )
    if _set(patch.guidance) and _set(patch.guidance_additions):
        raise ValueError("guidance and guidance_additions cannot be changed together")
    if _set(patch.guidance):
        if not isinstance(patch.guidance, (list, tuple)):
            raise ValueError("guidance must be a list of strings")
        after = replace(
            after,
            guidance=sorted(
                validate_nonempty_trimmed(value, "guidance")
                for value in patch.guidance
            ),
        )
    elif _set(patch.guidance_additions):
        if not isinstance(patch.guidance_additions, (list, tuple)):
            raise ValueError("guidance_additions must be a list of strings")
        after = replace(
            after,
            guidance=sorted(
                [
                    *after.guidance,
                    *(
                        validate_nonempty_trimmed(value, "guidance")
                        for value in patch.guidance_additions
                    ),
                ]
            ),
        )
    return after.validated()


async def _replace_active_guidance(
    session,
    *,
    cycle: Cycle,
    desired_guidance: list[str],
    actor: CycleActor,
    rationale: str,
    revision_id: int,
) -> None:
    active_rows = list(
        (
            await session.scalars(
                select(CycleGuidance)
                .where(
                    CycleGuidance.cycle_id == cycle.id,
                    CycleGuidance.is_active.is_(True),
                )
                .order_by(CycleGuidance.created_at.asc(), CycleGuidance.id.asc())
            )
        ).all()
    )
    unmatched = list(desired_guidance)
    for row in active_rows:
        try:
            index = unmatched.index(row.guidance)
        except ValueError:
            row.is_active = False
        else:
            unmatched.pop(index)

    for guidance in unmatched:
        session.add(
            CycleGuidance(
                cycle_id=cycle.id,
                revision_id=revision_id,
                source_type=actor.source_type,
                source_id=actor.revision_source_id,
                guidance=guidance,
                rationale=rationale,
                is_active=True,
            )
        )


async def _validate_target_idea(
    session,
    *,
    actor: CycleActor,
    target_idea_id: str | None,
) -> None:
    if target_idea_id is None:
        return
    row = await session.scalar(
        select(Idea.id).where(*target_idea_scope_conditions(target_idea_id, actor))
    )
    if row is None:
        raise ValueError("target_idea_id must belong to the current workspace")


async def _load_revert_source(
    session,
    *,
    cycle: Cycle,
    change_id: int,
) -> BehaviorChangeAudit:
    row = await session.scalar(
        select(BehaviorChangeAudit).where(
            BehaviorChangeAudit.id == change_id,
            *_audit_target_conditions(cycle),
        )
    )
    if row is None:
        raise ValueError("Behavior change not found")
    return row


def _preview_digest(
    *,
    current: EffectiveCyclePolicy,
    after_snapshot: CyclePolicySnapshot,
    changed_fields: tuple[str, ...],
    reverted_from_id: int | None,
) -> str:
    payload = _CyclePolicyPreviewDigestPayload(
        workspace_id=current.workspace_id,
        policy_kind=current.policy_kind,
        target_type=current.target_type,
        target_id=current.target_id,
        expected_version=current.version,
        before_snapshot=current.snapshot,
        after_snapshot=after_snapshot,
        changed_fields=changed_fields,
        reverted_from_id=reverted_from_id,
    )
    canonical = json.dumps(
        jsonable(payload),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _record(
    row: BehaviorChangeAudit,
    *,
    current_snapshot: CyclePolicySnapshot,
) -> BehaviorChangeRecord:
    applied_at = row.applied_at
    if applied_at.tzinfo is None:
        applied_at = applied_at.replace(tzinfo=timezone.utc)
    return BehaviorChangeRecord(
        id=row.id,
        workspace_id=row.workspace_id,
        policy_kind=row.policy_kind,
        target_type=row.target_type,
        target_id=row.target_id,
        version=row.version,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        source_reference=row.source_reference,
        rationale=row.rationale,
        before_snapshot=CyclePolicySnapshot.decode(
            row.before_snapshot,
            current=current_snapshot,
        ),
        after_snapshot=CyclePolicySnapshot.decode(
            row.after_snapshot,
            current=current_snapshot,
        ),
        changed_fields=tuple(row.changed_fields or []),
        cycle_revision_id=row.cycle_revision_id,
        applied_at=applied_at,
        reverted_from_id=row.reverted_from_id,
    )


def _audit_target_conditions(cycle: Cycle) -> tuple[Any, ...]:
    return (
        BehaviorChangeAudit.policy_kind == CYCLE_POLICY_KIND,
        BehaviorChangeAudit.target_type == CYCLE_TARGET_TYPE,
        BehaviorChangeAudit.target_id == str(cycle.id),
    )


def _workspace_id(cycle: Cycle) -> str:
    return str(cycle.org_id or cycle.user_id)


def _validated_max_concurrency(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_concurrency must be an integer >= 1")
    return value


def _set(
    value: _CyclePatchField[_PatchValueT],
) -> TypeGuard[_PatchValueT]:
    return value is not UNSET_CYCLE_FIELD
