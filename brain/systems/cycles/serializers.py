"""Cycle read-model serializers."""
from __future__ import annotations

from brain.platform.db.models.cycle import (
    Cycle,
    CycleGuidance,
    CycleOutputTarget,
    CycleRevision,
    CycleRun,
    CycleRunEvaluation,
)
from brain.systems.cycles.common import (
    REUSABLE_THREAD_EXECUTION_MODE,
    creator_payload,
    json_dict,
    json_list,
    required_datetime,
    string_or_none,
)
from brain.systems.cycles.schedules import safe_humanize_schedule


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
        "model_override": cycle.model_override,
        "thinking_override": cycle.thinking_override,
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
