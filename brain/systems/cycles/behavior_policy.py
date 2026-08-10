"""Reviewed Cycle behavior-policy commands and their immutable audit envelope.

This module is the only write path for an existing Cycle's behavior fields and
active guidance set. API and agent adapters can differ, but both must preview
and apply through this contract.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

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
from brain.systems.cycles.events import publish_cycle_change
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
    "EffectiveCyclePolicy",
    "async_apply_cycle_policy_change",
    "async_apply_cycle_policy_revert",
    "async_list_cycle_policy_history",
    "async_preview_cycle_policy_change",
    "async_preview_cycle_policy_revert",
    "async_read_effective_cycle_policy",
]

CYCLE_POLICY_KIND = "cycle"
CYCLE_TARGET_TYPE = "cycle"


class _UnsetPolicyField:
    pass


UNSET_POLICY_FIELD = _UnsetPolicyField()


@dataclass(frozen=True)
class CyclePolicyPatch:
    """Typed changes to Cycle behavior, never arbitrary table columns."""

    name: Any = UNSET_POLICY_FIELD
    prompt: Any = UNSET_POLICY_FIELD
    timezone_name: Any = UNSET_POLICY_FIELD
    schedule_expr: Any = UNSET_POLICY_FIELD
    run_at: Any = UNSET_POLICY_FIELD
    enabled: Any = UNSET_POLICY_FIELD
    max_concurrency: Any = UNSET_POLICY_FIELD
    timeout_seconds: Any = UNSET_POLICY_FIELD
    retry_policy: Any = UNSET_POLICY_FIELD
    model_override: Any = UNSET_POLICY_FIELD
    thinking_override: Any = UNSET_POLICY_FIELD
    execution_policy_key: Any = UNSET_POLICY_FIELD
    target_idea_id: Any = UNSET_POLICY_FIELD
    guidance: Any = UNSET_POLICY_FIELD
    guidance_additions: Any = UNSET_POLICY_FIELD


@dataclass(frozen=True)
class _CyclePolicyReplacement:
    """Validated full replacement used only by the revert adapter."""

    snapshot: Mapping[str, Any]


@dataclass(frozen=True)
class EffectiveCyclePolicy:
    workspace_id: str
    policy_kind: str
    target_type: str
    target_id: str
    version: int
    revision_id: int | None
    snapshot: dict[str, Any]


@dataclass(frozen=True)
class CyclePolicyPreview:
    before: EffectiveCyclePolicy
    after_snapshot: dict[str, Any]
    changed_fields: tuple[str, ...]
    preview_digest: str
    reverted_from_id: int | None = None


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
    before_snapshot: dict[str, Any]
    after_snapshot: dict[str, Any]
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
    proposal: CyclePolicyPatch | _CyclePolicyReplacement,
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

        if reverted_from_id is not None:
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

        _apply_cycle_snapshot(cycle, preview.after_snapshot)
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
            desired_guidance=preview.after_snapshot["guidance"],
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
            before_snapshot=deepcopy(current.snapshot),
            after_snapshot=deepcopy(preview.after_snapshot),
            changed_fields=list(preview.changed_fields),
            cycle_revision_id=revision.id,
            reverted_from_id=reverted_from_id,
            applied_at=datetime.now(timezone.utc),
        )
        session.add(change)
        await session.flush()

        publish_cycle_change(
            action="update",
            org_id=cycle.org_id,
            user_id=cycle.user_id,
            cycle_id=cycle.id,
            target_idea_id=cycle.target_idea_id,
            strict=True,
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
            change=_record(change),
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
    return [_record(row) for row in rows]


async def async_preview_cycle_policy_revert(
    session,
    *,
    actor: CycleActor,
    cycle_id: int,
    change_id: int,
) -> CyclePolicyPreview:
    cycle = await _load_scoped_cycle(session, actor=actor, cycle_id=cycle_id)
    source = await _load_revert_source(session, cycle=cycle, change_id=change_id)
    current = await _effective_policy(session, cycle)
    return await _preview_for_cycle(
        session,
        actor=actor,
        cycle=cycle,
        current=current,
        proposal=_CyclePolicyReplacement(source.before_snapshot),
        reverted_from_id=source.id,
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
    cycle = await _load_scoped_cycle(session, actor=actor, cycle_id=cycle_id)
    source = await _load_revert_source(session, cycle=cycle, change_id=change_id)
    return await async_apply_cycle_policy_change(
        session,
        actor=actor,
        cycle_id=cycle_id,
        proposal=_CyclePolicyReplacement(source.before_snapshot),
        expected_version=expected_version,
        preview_digest=preview_digest,
        rationale=rationale,
        source_reference=source_reference,
        reverted_from_id=source.id,
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
        snapshot=_cycle_snapshot(cycle, guidance_rows),
    )


async def _preview_for_cycle(
    session,
    *,
    actor: CycleActor,
    cycle: Cycle,
    current: EffectiveCyclePolicy,
    proposal: CyclePolicyPatch | _CyclePolicyReplacement,
    reverted_from_id: int | None = None,
) -> CyclePolicyPreview:
    if isinstance(proposal, _CyclePolicyReplacement):
        after = _validated_snapshot(proposal.snapshot)
    else:
        after = _project_patch(current.snapshot, proposal)
    if after["target_idea_id"] != current.snapshot["target_idea_id"]:
        await _validate_target_idea(
            session,
            actor=actor,
            target_idea_id=after["target_idea_id"],
        )
    changed_fields = tuple(
        key for key in sorted(after) if current.snapshot.get(key) != after.get(key)
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
    current_snapshot: Mapping[str, Any],
    patch: CyclePolicyPatch,
) -> dict[str, Any]:
    after = deepcopy(dict(current_snapshot))
    timezone_name = after["timezone"]
    if _set(patch.timezone_name) and patch.timezone_name is not None:
        timezone_name = validate_timezone_name(patch.timezone_name)
        after["timezone"] = timezone_name

    if _set(patch.run_at) and patch.run_at is not None:
        after["schedule_expr"] = build_one_time_schedule_expr(
            patch.run_at,
            timezone_name,
        )
    elif _set(patch.schedule_expr) and patch.schedule_expr is not None:
        after["schedule_expr"] = validate_schedule_expr(
            patch.schedule_expr,
            timezone_name,
        )
    elif after["timezone"] != current_snapshot["timezone"]:
        after["schedule_expr"] = validate_schedule_expr(
            after["schedule_expr"],
            timezone_name,
        )

    for field in ("name", "prompt"):
        value = getattr(patch, field)
        if _set(value) and value is not None:
            after[field] = validate_nonempty_trimmed(value, field)

    if _set(patch.enabled) and patch.enabled is not None:
        if not isinstance(patch.enabled, bool):
            raise ValueError("enabled must be a boolean")
        after["enabled"] = patch.enabled
    if _set(patch.max_concurrency):
        after["max_concurrency"] = _validated_max_concurrency(patch.max_concurrency)
    if _set(patch.timeout_seconds):
        after["timeout_seconds"] = validate_cycle_timeout_seconds(
            patch.timeout_seconds
        )
    if _set(patch.retry_policy):
        if not isinstance(patch.retry_policy, dict):
            raise ValueError("retry_policy must be an object")
        after["retry_policy"] = jsonable(deepcopy(patch.retry_policy))
    if _set(patch.model_override):
        after["model_override"] = validate_model_override(patch.model_override)
    if _set(patch.thinking_override):
        after["thinking_override"] = validate_thinking_override(
            patch.thinking_override
        )
    if _set(patch.execution_policy_key):
        after["execution_policy_key"] = validate_cycle_execution_policy_key(
            patch.execution_policy_key
        )
    else:
        validate_cycle_execution_policy_key(after["execution_policy_key"])
    if _set(patch.target_idea_id):
        after["target_idea_id"] = (
            str(patch.target_idea_id) if patch.target_idea_id is not None else None
        )
    if _set(patch.guidance) and _set(patch.guidance_additions):
        raise ValueError("guidance and guidance_additions cannot be changed together")
    if _set(patch.guidance):
        if not isinstance(patch.guidance, (list, tuple)):
            raise ValueError("guidance must be a list of strings")
        after["guidance"] = sorted(
            validate_nonempty_trimmed(value, "guidance")
            for value in patch.guidance
        )
    elif _set(patch.guidance_additions):
        if not isinstance(patch.guidance_additions, (list, tuple)):
            raise ValueError("guidance_additions must be a list of strings")
        after["guidance"] = sorted(
            [
                *after["guidance"],
                *(
                    validate_nonempty_trimmed(value, "guidance")
                    for value in patch.guidance_additions
                ),
            ]
        )
    return _validated_snapshot(after)


def _validated_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    expected_fields = {
        "name",
        "prompt",
        "schedule_expr",
        "timezone",
        "enabled",
        "max_concurrency",
        "timeout_seconds",
        "retry_policy",
        "model_override",
        "thinking_override",
        "execution_policy_key",
        "target_idea_id",
        "guidance",
    }
    if set(snapshot) != expected_fields:
        raise ValueError("Cycle policy snapshot has an invalid field set")
    timezone_name = validate_timezone_name(snapshot["timezone"])
    if not isinstance(snapshot["enabled"], bool):
        raise ValueError("enabled must be a boolean")
    if not isinstance(snapshot["retry_policy"], dict):
        raise ValueError("retry_policy must be an object")
    if not isinstance(snapshot["guidance"], (list, tuple)):
        raise ValueError("guidance must be a list of strings")
    return {
        "name": validate_nonempty_trimmed(snapshot["name"], "name"),
        "prompt": validate_nonempty_trimmed(snapshot["prompt"], "prompt"),
        "schedule_expr": validate_schedule_expr(
            snapshot["schedule_expr"],
            timezone_name,
        ),
        "timezone": timezone_name,
        "enabled": snapshot["enabled"],
        "max_concurrency": _validated_max_concurrency(
            snapshot["max_concurrency"]
        ),
        "timeout_seconds": validate_cycle_timeout_seconds(
            snapshot["timeout_seconds"]
        ),
        "retry_policy": jsonable(deepcopy(snapshot["retry_policy"])),
        "model_override": validate_model_override(snapshot["model_override"]),
        "thinking_override": validate_thinking_override(
            snapshot["thinking_override"]
        ),
        "execution_policy_key": validate_cycle_execution_policy_key(
            snapshot["execution_policy_key"]
        ),
        "target_idea_id": (
            str(snapshot["target_idea_id"])
            if snapshot["target_idea_id"] is not None
            else None
        ),
        "guidance": sorted(
            validate_nonempty_trimmed(value, "guidance")
            for value in snapshot["guidance"]
        ),
    }


def _cycle_snapshot(cycle: Cycle, guidance_rows: list[CycleGuidance]) -> dict[str, Any]:
    return _validated_snapshot(
        {
            "name": cycle.name,
            "prompt": cycle.prompt,
            "schedule_expr": cycle.schedule_expr,
            "timezone": cycle.timezone,
            "enabled": bool(cycle.enabled),
            "max_concurrency": max(int(cycle.max_concurrency or 1), 1),
            "timeout_seconds": cycle.timeout_seconds,
            "retry_policy": dict(cycle.retry_policy or {}),
            "model_override": cycle.model_override,
            "thinking_override": cycle.thinking_override,
            "execution_policy_key": cycle.execution_policy_key,
            "target_idea_id": (
                str(cycle.target_idea_id) if cycle.target_idea_id is not None else None
            ),
            "guidance": [row.guidance for row in guidance_rows],
        }
    )


def _apply_cycle_snapshot(cycle: Cycle, snapshot: Mapping[str, Any]) -> None:
    cycle.name = snapshot["name"]
    cycle.prompt = snapshot["prompt"]
    cycle.schedule_expr = snapshot["schedule_expr"]
    cycle.timezone = snapshot["timezone"]
    cycle.enabled = snapshot["enabled"]
    cycle.max_concurrency = snapshot["max_concurrency"]
    cycle.timeout_seconds = snapshot["timeout_seconds"]
    cycle.retry_policy = deepcopy(snapshot["retry_policy"])
    cycle.model_override = snapshot["model_override"]
    cycle.thinking_override = snapshot["thinking_override"]
    cycle.execution_policy_key = snapshot["execution_policy_key"]
    cycle.target_idea_id = snapshot["target_idea_id"]
    cycle.execution_mode = canonical_execution_mode()
    cycle.reopen_archived = True
    cycle.next_run_at = compute_next_run_at(cycle.schedule_expr, cycle.timezone)
    cycle.updated_at = datetime.now(timezone.utc)


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
    after_snapshot: Mapping[str, Any],
    changed_fields: tuple[str, ...],
    reverted_from_id: int | None,
) -> str:
    payload = {
        "workspace_id": current.workspace_id,
        "policy_kind": current.policy_kind,
        "target_type": current.target_type,
        "target_id": current.target_id,
        "expected_version": current.version,
        "before_snapshot": current.snapshot,
        "after_snapshot": after_snapshot,
        "changed_fields": list(changed_fields),
        "reverted_from_id": reverted_from_id,
    }
    canonical = json.dumps(
        jsonable(payload),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _record(row: BehaviorChangeAudit) -> BehaviorChangeRecord:
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
        before_snapshot=deepcopy(row.before_snapshot),
        after_snapshot=deepcopy(row.after_snapshot),
        changed_fields=tuple(row.changed_fields or []),
        cycle_revision_id=row.cycle_revision_id,
        applied_at=applied_at,
        reverted_from_id=row.reverted_from_id,
    )


def serialize_behavior_change(row: BehaviorChangeAudit | None) -> dict | None:
    if row is None:
        return None
    record = _record(row)
    return {
        "id": record.id,
        "workspace_id": record.workspace_id,
        "policy_kind": record.policy_kind,
        "target_type": record.target_type,
        "target_id": record.target_id,
        "version": record.version,
        "actor_type": record.actor_type,
        "actor_id": record.actor_id,
        "source_reference": record.source_reference,
        "rationale": record.rationale,
        "before_snapshot": record.before_snapshot,
        "after_snapshot": record.after_snapshot,
        "changed_fields": list(record.changed_fields),
        "cycle_revision_id": record.cycle_revision_id,
        "applied_at": record.applied_at.isoformat(),
        "reverted_from_id": record.reverted_from_id,
    }


def _audit_target_conditions(cycle: Cycle) -> tuple[Any, ...]:
    return (
        BehaviorChangeAudit.policy_kind == CYCLE_POLICY_KIND,
        BehaviorChangeAudit.target_type == CYCLE_TARGET_TYPE,
        BehaviorChangeAudit.target_id == str(cycle.id),
    )


def _workspace_id(cycle: Cycle) -> str:
    return str(cycle.org_id or cycle.user_id)


def _validated_max_concurrency(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_concurrency must be an integer >= 1")
    return value


def _set(value: Any) -> bool:
    return value is not UNSET_POLICY_FIELD
