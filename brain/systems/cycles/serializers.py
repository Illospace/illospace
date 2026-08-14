"""Cycle read-model serializers."""
from __future__ import annotations

from copy import deepcopy
from datetime import timezone
from typing import TYPE_CHECKING

from brain.platform.db.models.cycle import (
    Cycle,
    CycleBehaviorChangeAudit,
    CycleGuidance,
    CycleOutputTarget,
    CycleRevision,
    CycleRun,
    CycleRunEvaluation,
)
from brain.systems.cycles.common import (
    REUSABLE_THREAD_EXECUTION_MODE,
    cycle_executor_binding,
    creator_payload,
    json_dict,
    json_list,
    required_datetime,
    string_or_none,
)
from brain.systems.cycles.schedules import safe_humanize_schedule

if TYPE_CHECKING:
    from brain.systems.cycles.behavior_policy import (
        BehaviorChangeRecord,
        CyclePolicyPreview,
    )
    from brain.systems.cycles.behavior_policy_contract import CyclePolicySnapshot
    from brain.systems.cycles.behavior_policy_read_model import (
        EffectiveCyclePolicyReadModel,
    )


_CYCLE_POLICY_EDITABLE_FIELDS = (
    "prompt",
    "schedule_expr",
    "timezone",
    "enabled",
    "model_override",
    "thinking_override",
    "executor_binding",
    "skill_ids",
    "guidance",
)


def _serialize_cycle_policy_snapshot(snapshot: CyclePolicySnapshot) -> dict:
    """Project a policy snapshot onto the behavior-policy HTTP shape."""

    configuration = {}
    for field_name in snapshot.configuration_field_names():
        configuration[field_name] = deepcopy(getattr(snapshot, field_name))
        if field_name == "schedule_expr":
            configuration["schedule_human"] = safe_humanize_schedule(
                snapshot.schedule_expr,
                snapshot.timezone,
            )
    return {
        "configuration": configuration,
        "guidance": list(snapshot.guidance),
    }


def _utc_datetime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def serialize_behavior_change_summary(
    change: BehaviorChangeRecord | None,
) -> dict | None:
    if change is None:
        return None
    return {
        "id": change.id,
        "version": change.version,
        "actor_type": change.actor_type,
        "actor_id": change.actor_id,
        "source_reference": change.source_reference,
        "rationale": change.rationale,
        "changed_fields": list(change.changed_fields),
        "applied_at": _utc_datetime(change.applied_at),
        "reverted_from_id": change.reverted_from_id,
    }


def serialize_behavior_change_record(change: BehaviorChangeRecord) -> dict:
    return {
        **serialize_behavior_change_summary(change),
        "workspace_id": change.workspace_id,
        "target_id": change.target_id,
        "before_snapshot": _serialize_cycle_policy_snapshot(change.before_snapshot),
        "after_snapshot": _serialize_cycle_policy_snapshot(change.after_snapshot),
        "cycle_revision_id": change.cycle_revision_id,
    }


def serialize_effective_cycle_policy(
    policy: EffectiveCyclePolicyReadModel,
) -> dict:
    revision = policy.source_revision
    latest_change = policy.latest_change
    snapshot_payload = _serialize_cycle_policy_snapshot(policy.snapshot)
    output_targets = [
        {
            "id": target.id,
            "target_type": target.target_type,
            "target_id": string_or_none(target.target_id),
            "label": target.label,
            "config": deepcopy(target.config or {}),
            "source_type": target.source_type,
            "source_id": string_or_none(target.source_id),
            "rationale": target.rationale,
            "created_at": _utc_datetime(target.created_at),
            "updated_at": _utc_datetime(target.updated_at),
        }
        for target in policy.output_targets
    ]
    field_sources = {
        source.field_name: {
            "version": source.version,
            "cycle_revision_id": source.cycle_revision_id,
            "actor_type": source.actor_type,
            "actor_id": source.actor_id,
            "source_reference": source.source_reference,
            "rationale": source.rationale,
            "changed_at": _utc_datetime(source.changed_at),
            "change_id": source.change_id,
        }
        for source in policy.field_sources
    }
    return {
        "workspace_id": policy.workspace_id,
        "target_id": policy.target_id,
        "version": policy.version,
        "revision_id": policy.revision_id,
        **snapshot_payload,
        "editable_fields": list(_CYCLE_POLICY_EDITABLE_FIELDS),
        "output_targets": output_targets,
        "output_targets_read_only": True,
        "source": {
            "revision_id": policy.revision_id,
            "actor_type": revision.source_type if revision is not None else None,
            "actor_id": (
                string_or_none(revision.source_id)
                if revision is not None
                else None
            ),
            "rationale": revision.rationale if revision is not None else None,
            "source_reference": (
                latest_change.source_reference
                if latest_change is not None
                else None
            ),
            "changed_at": (
                _utc_datetime(revision.created_at)
                if revision is not None
                else None
            ),
        },
        "field_sources": field_sources,
        "latest_change": serialize_behavior_change_summary(latest_change),
    }


def serialize_cycle_policy_preview(preview: CyclePolicyPreview) -> dict:
    changed_fields = list(preview.changed_fields)
    warnings = []
    if changed_fields:
        warnings.append(
            {
                "code": "admitted_runs_unchanged",
                "message": (
                    "CycleRuns admitted before apply keep their existing "
                    "policy snapshots."
                ),
            }
        )
    else:
        warnings.append(
            {
                "code": "no_changes",
                "message": "The proposal does not change the effective policy.",
            }
        )
    if "enabled" in preview.changed_fields and not preview.after_snapshot.enabled:
        warnings.append(
            {
                "code": "future_runs_disabled",
                "message": "Disabling this Cycle stops new scheduled admissions.",
            }
        )
    if {"schedule_expr", "timezone"}.intersection(preview.changed_fields):
        warnings.append(
            {
                "code": "future_schedule_changed",
                "message": "The new schedule applies after this policy is applied.",
            }
        )
    return {
        "expected_version": preview.before.version,
        "preview_digest": preview.preview_digest,
        "before": _serialize_cycle_policy_snapshot(preview.before.snapshot),
        "after": _serialize_cycle_policy_snapshot(preview.after_snapshot),
        "changed_fields": changed_fields,
        "diff": [
            _serialize_cycle_policy_diff_entry(preview, field_name)
            for field_name in preview.changed_fields
        ],
        "warnings": warnings,
        "affected_runs": {
            "admitted_runs": "unchanged",
            "future_runs": (
                "use_proposed_policy_after_apply"
                if changed_fields
                else "unchanged"
            ),
        },
        "reverted_from_id": preview.reverted_from_id,
    }


def _serialize_cycle_policy_diff_entry(
    preview: CyclePolicyPreview,
    field_name: str,
) -> dict:
    before = preview.before.snapshot
    after = preview.after_snapshot
    before_value = deepcopy(getattr(before, field_name))
    after_value = deepcopy(getattr(after, field_name))
    if field_name == "guidance":
        return {
            "field": field_name,
            "kind": "collection",
            "before": before_value,
            "after": after_value,
            "added": [value for value in after_value if value not in before_value],
            "removed": [value for value in before_value if value not in after_value],
        }
    if field_name in {"schedule_expr", "timezone"}:
        return {
            "field": field_name,
            "kind": "schedule",
            "before": _schedule_diff_value(before),
            "after": _schedule_diff_value(after),
        }
    return {
        "field": field_name,
        "kind": "value",
        "before": before_value,
        "after": after_value,
    }


def _schedule_diff_value(snapshot: CyclePolicySnapshot) -> dict:
    return {
        "schedule_expr": snapshot.schedule_expr,
        "schedule_human": safe_humanize_schedule(
            snapshot.schedule_expr,
            snapshot.timezone,
        ),
        "timezone": snapshot.timezone,
    }


def serialize_behavior_change(row: CycleBehaviorChangeAudit | None) -> dict | None:
    if row is None:
        return None
    applied_at = row.applied_at
    if applied_at.tzinfo is None:
        applied_at = applied_at.replace(tzinfo=timezone.utc)
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "target_id": row.target_id,
        "version": row.version,
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "source_reference": row.source_reference,
        "rationale": row.rationale,
        "before_snapshot": deepcopy(row.before_snapshot),
        "after_snapshot": deepcopy(row.after_snapshot),
        "changed_fields": list(row.changed_fields or []),
        "cycle_revision_id": row.cycle_revision_id,
        "applied_at": applied_at.isoformat(),
        "reverted_from_id": row.reverted_from_id,
    }


def serialize_cycle_revision(revision: CycleRevision | None) -> dict | None:
    if revision is None:
        return None
    return {
        "id": revision.id,
        "cycle_id": revision.cycle_id,
        "revision_number": revision.revision_number,
        "source_type": revision.source_type,
        "source_id": string_or_none(revision.source_id),
        "rationale": revision.rationale,
        "name": revision.name,
        "prompt": revision.prompt,
        "schedule_expr": revision.schedule_expr,
        "timezone": revision.timezone,
        "enabled": revision.enabled,
        "model_override": revision.model_override,
        "thinking_override": revision.thinking_override,
        "execution_policy_key": getattr(revision, "execution_policy_key", None),
        "target_idea_id": string_or_none(revision.target_idea_id),
        "context_policy": json_dict(revision.context_policy),
        "created_at": required_datetime(revision.created_at),
    }


def serialize_cycle_guidance(guidance: CycleGuidance) -> dict:
    return {
        "id": guidance.id,
        "cycle_id": guidance.cycle_id,
        "revision_id": guidance.revision_id,
        "source_type": guidance.source_type,
        "source_id": string_or_none(guidance.source_id),
        "guidance": guidance.guidance,
        "rationale": guidance.rationale,
        "is_active": guidance.is_active,
        "created_at": required_datetime(guidance.created_at),
    }


def serialize_cycle_output_target(target: CycleOutputTarget) -> dict:
    return {
        "id": target.id,
        "cycle_id": target.cycle_id,
        "revision_id": target.revision_id,
        "target_type": target.target_type,
        "target_id": string_or_none(target.target_id),
        "label": target.label,
        "config": json_dict(target.config),
        "source_type": target.source_type,
        "source_id": string_or_none(target.source_id),
        "rationale": target.rationale,
        "is_active": target.is_active,
        "created_at": required_datetime(target.created_at, target.updated_at),
        "updated_at": required_datetime(target.updated_at, target.created_at),
    }


def serialize_cycle_run_evaluation(evaluation: CycleRunEvaluation) -> dict:
    return {
        "id": evaluation.id,
        "cycle_id": evaluation.cycle_id,
        "cycle_run_id": evaluation.cycle_run_id,
        "evaluator_type": evaluation.evaluator_type,
        "evaluator_id": string_or_none(evaluation.evaluator_id),
        "summary": evaluation.summary,
        "score": evaluation.score,
        "details": json_dict(evaluation.details),
        "created_at": required_datetime(evaluation.created_at),
    }


def serialize_cycle(cycle: Cycle) -> dict:
    created_at = required_datetime(cycle.created_at, cycle.updated_at)
    updated_at = required_datetime(cycle.updated_at, created_at)
    creator = creator_payload(cycle)
    return {
        "id": cycle.id,
        "user_id": str(cycle.user_id),
        "org_id": string_or_none(cycle.org_id),
        "workspace_id": string_or_none(cycle.org_id),
        **creator,
        "name": cycle.name,
        "prompt": cycle.prompt,
        "schedule_expr": cycle.schedule_expr,
        "schedule_human": safe_humanize_schedule(cycle.schedule_expr, cycle.timezone),
        "timezone": cycle.timezone,
        "enabled": cycle.enabled,
        "max_concurrency": max(int(getattr(cycle, "max_concurrency", 1) or 1), 1),
        "timeout_seconds": getattr(cycle, "timeout_seconds", None),
        "model_override": cycle.model_override,
        "thinking_override": cycle.thinking_override,
        "execution_policy_key": getattr(cycle, "execution_policy_key", None),
        "executor_binding": cycle_executor_binding(cycle),
        "skill_ids": list(getattr(cycle, "skill_ids", None) or []),
        "execution_mode": REUSABLE_THREAD_EXECUTION_MODE,
        "target_idea_id": string_or_none(cycle.target_idea_id),
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
        "idea_id": string_or_none(run.idea_id),
        "run_id": run.run_id,
        "prompt_snapshot": run.prompt_snapshot,
        "guidance_snapshot": json_list(getattr(run, "guidance_snapshot", None)),
        "output_targets_snapshot": json_list(getattr(run, "output_targets_snapshot", None)),
        "context_snapshot": json_dict(getattr(run, "context_snapshot", None)),
        "self_review_summary": getattr(run, "self_review_summary", None),
        "created_at": required_datetime(run.created_at, run.scheduled_for),
    }
