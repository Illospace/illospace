"""Agency policy and budget evaluation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.agency import (
    CANDIDATE_STATE_EXPIRED,
    CANDIDATE_STATE_PROPOSED,
    CANDIDATE_STATE_REJECTED,
    CANDIDATE_STATE_SUPPRESSED,
    CANDIDATE_STATE_APPROVED,
    AgencyBudget,
    AgencyCandidate,
)
from brain.systems.services.runtime_introspection import get_agency_runtime_settings

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_REVERSIBILITY_ORDER = {
    "read_only": 0,
    "practice_safe": 1,
    "repo_local": 2,
    "stateful": 3,
}
_AUTO_EXEC_FLAG_BY_REVERSIBILITY = {
    "read_only": "auto_execute_read_only",
    "practice_safe": "auto_execute_practice_runs",
    "repo_local": "auto_execute_repo_local",
}


def _risk_level(value: str | None) -> int:
    return _RISK_ORDER.get((value or "low").lower(), 0)


def _reversibility_level(value: str | None) -> int:
    return _REVERSIBILITY_ORDER.get((value or "read_only").lower(), 0)


def _auto_exec_flag(candidate: AgencyCandidate) -> str | None:
    return _AUTO_EXEC_FLAG_BY_REVERSIBILITY.get((candidate.reversibility_class or "").lower())


def _target_is_resolved(candidate: AgencyCandidate) -> bool:
    if candidate.target_binding_id:
        return True
    return (candidate.reversibility_class or "read_only").lower() == "read_only"


def budget_scope_for_candidate(candidate: AgencyCandidate) -> tuple[str, str]:
    if candidate.org_id:
        return "org", candidate.org_id
    if candidate.user_id:
        return "user", candidate.user_id
    return "global", "global"


def evaluate_candidate_budget(
    session,
    candidate: AgencyCandidate,
    *,
    now: datetime | None = None,
) -> AgencyBudget | None:
    """Resolve the active budget for a candidate, if any."""
    now = now or datetime.now(timezone.utc)
    scope_type, scope_id = budget_scope_for_candidate(candidate)
    stmt = (
        select(AgencyBudget)
        .where(
            AgencyBudget.active.is_(True),
            AgencyBudget.scope_type == scope_type,
            AgencyBudget.scope_id == scope_id,
            AgencyBudget.window_start <= now,
            AgencyBudget.window_end >= now,
        )
        .order_by(
            AgencyBudget.drive_type.is_(None),
            AgencyBudget.window_start.desc(),
            AgencyBudget.id.desc(),
        )
    )
    budgets = session.scalars(stmt).all()
    if candidate.drive_type:
        for budget in budgets:
            if budget.drive_type == candidate.drive_type:
                return budget
    return budgets[0] if budgets else None


def budget_snapshot_for_candidate(
    budget: AgencyBudget | None,
    candidate: AgencyCandidate | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a stable, auditable budget snapshot for policy and ledger records."""
    now = now or datetime.now(timezone.utc)
    if budget is None:
        if candidate is not None:
            scope_type, scope_id = budget_scope_for_candidate(candidate)
            drive_type = candidate.drive_type
        else:
            scope_type, scope_id, drive_type = "none", "none", None
        return {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "drive_type": drive_type,
            "window_start": now.isoformat(),
            "window_end": now.isoformat(),
            "max_candidates": 0,
            "max_auto_exec": 0,
            "max_estimated_cost": 0.0,
            "max_estimated_tokens": 0,
            "require_review_above_risk": "medium",
            "auto_execute_enabled": False,
            "cooldown_hours": 24,
            "consumed_candidates": 0,
            "consumed_auto_exec": 0,
            "consumed_cost": 0.0,
            "consumed_tokens": 0,
            "remaining_candidates": 0,
            "remaining_auto_exec": 0,
            "remaining_cost": 0.0,
            "remaining_tokens": 0,
            "reservation": {
                "reserved_candidates": 0,
                "reserved_auto_exec": 0,
                "reserved_cost": 0.0,
                "reserved_tokens": 0,
            },
            "active": False,
        }
    return {
        "id": budget.id,
        "scope_type": budget.scope_type,
        "scope_id": budget.scope_id,
        "drive_type": budget.drive_type,
        "window_start": budget.window_start.isoformat(),
        "window_end": budget.window_end.isoformat(),
        "max_candidates": budget.max_candidates,
        "max_auto_exec": budget.max_auto_exec,
        "max_estimated_cost": budget.max_estimated_cost,
        "max_estimated_tokens": budget.max_estimated_tokens,
        "require_review_above_risk": budget.require_review_above_risk,
        "auto_execute_enabled": budget.auto_execute_enabled,
        "cooldown_hours": budget.cooldown_hours,
        "consumed_candidates": budget.consumed_candidates,
        "consumed_auto_exec": budget.consumed_auto_exec,
        "consumed_cost": budget.consumed_cost,
        "consumed_tokens": budget.consumed_tokens,
        "remaining_candidates": max(0, budget.max_candidates - budget.consumed_candidates),
        "remaining_auto_exec": max(0, budget.max_auto_exec - budget.consumed_auto_exec),
        "remaining_cost": max(0.0, budget.max_estimated_cost - budget.consumed_cost),
        "remaining_tokens": max(0, budget.max_estimated_tokens - budget.consumed_tokens),
        "reservation": {
            "reserved_candidates": 0,
            "reserved_auto_exec": 0,
            "reserved_cost": 0.0,
            "reserved_tokens": 0,
        },
        "active": budget.active,
    }


def evaluate_candidate(
    session,
    candidate: AgencyCandidate,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate a candidate against policy and budgets.

    Default posture is recommendation-first. Only explicit approval and
    resolved targets can advance toward scheduler handoff.
    """
    now = now or datetime.now(timezone.utc)
    budget = evaluate_candidate_budget(session, candidate, now=now)
    runtime = get_agency_runtime_settings()
    policy_snapshot = {
        "drive_type": candidate.drive_type,
        "risk_class": candidate.risk_class,
        "reversibility_class": candidate.reversibility_class,
        "target_binding_id": candidate.target_binding_id,
        "candidate_status": candidate.status,
        "auto_exec_flag": _auto_exec_flag(candidate),
        "runtime": runtime,
    }
    budget_snapshot = budget_snapshot_for_candidate(budget, candidate, now=now)

    if candidate.expires_at and candidate.expires_at <= now:
        return {
            "decision": "block",
            "candidate_status": CANDIDATE_STATE_EXPIRED,
            "reason_code": "candidate_expired",
            "reason_text": "Candidate expired before it could be considered.",
            "actor_type": "system",
            "actor_id": "agency",
            "policy_snapshot": policy_snapshot,
            "budget_snapshot": budget_snapshot,
            "scheduler_run_id": None,
            "run_id": None,
        }

    if candidate.suppression_until and candidate.suppression_until > now:
        return {
            "decision": "block",
            "candidate_status": CANDIDATE_STATE_SUPPRESSED,
            "reason_code": "cooldown_active",
            "reason_text": "Candidate is under cooldown suppression.",
            "actor_type": "system",
            "actor_id": "agency",
            "policy_snapshot": policy_snapshot,
            "budget_snapshot": budget_snapshot,
            "scheduler_run_id": None,
            "run_id": None,
        }

    if not _target_is_resolved(candidate) and candidate.reversibility_class != "read_only":
        return {
            "decision": "block",
            "candidate_status": CANDIDATE_STATE_REJECTED,
            "reason_code": "unresolved_target",
            "reason_text": "Candidate has no resolved target binding.",
            "actor_type": "system",
            "actor_id": "agency",
            "policy_snapshot": policy_snapshot,
            "budget_snapshot": budget_snapshot,
            "scheduler_run_id": None,
            "run_id": None,
        }

    if budget is None:
        return {
            "decision": "recommend",
            "candidate_status": CANDIDATE_STATE_PROPOSED,
            "reason_code": "no_active_budget",
            "reason_text": "No active agency budget was found, so the candidate remains a recommendation.",
            "actor_type": "system",
            "actor_id": "agency",
            "policy_snapshot": policy_snapshot,
            "budget_snapshot": budget_snapshot,
            "scheduler_run_id": None,
            "run_id": None,
        }

    auto_exec_flag = _auto_exec_flag(candidate)
    execution_class = (candidate.reversibility_class or "read_only").lower()
    if runtime.get("recommendation_mode", True):
        return {
            "decision": "recommend",
            "candidate_status": CANDIDATE_STATE_PROPOSED,
            "reason_code": "recommendation_mode_enabled",
            "reason_text": "Agency recommendation mode is enabled, so the candidate stays review-first.",
            "actor_type": "system",
            "actor_id": "agency",
            "policy_snapshot": policy_snapshot,
            "budget_snapshot": budget_snapshot,
            "scheduler_run_id": None,
            "run_id": None,
        }

    if not budget.auto_execute_enabled:
        return {
            "decision": "recommend",
            "candidate_status": CANDIDATE_STATE_PROPOSED,
            "reason_code": "auto_exec_disabled",
            "reason_text": "Auto-execution is disabled, so the candidate stays recommendation-only.",
            "actor_type": "system",
            "actor_id": "agency",
            "policy_snapshot": policy_snapshot,
            "budget_snapshot": budget_snapshot,
            "scheduler_run_id": None,
            "run_id": None,
        }

    if execution_class not in {"read_only", "practice_safe"}:
        return {
            "decision": "recommend",
            "candidate_status": CANDIDATE_STATE_PROPOSED,
            "reason_code": "auto_exec_class_disabled",
            "reason_text": "The candidate reversibility class is not eligible for bounded auto-execution.",
            "actor_type": "system",
            "actor_id": "agency",
            "policy_snapshot": policy_snapshot,
            "budget_snapshot": budget_snapshot,
            "scheduler_run_id": None,
            "run_id": None,
        }

    if not runtime.get(auto_exec_flag or "", False):
        return {
            "decision": "recommend",
            "candidate_status": CANDIDATE_STATE_PROPOSED,
            "reason_code": "auto_exec_disabled",
            "reason_text": "Auto-execution is disabled, so the candidate stays recommendation-only.",
            "actor_type": "system",
            "actor_id": "agency",
            "policy_snapshot": policy_snapshot,
            "budget_snapshot": budget_snapshot,
            "scheduler_run_id": None,
            "run_id": None,
        }

    if budget.max_candidates <= 0 or budget.consumed_candidates >= budget.max_candidates:
        return {
            "decision": "block",
            "candidate_status": CANDIDATE_STATE_SUPPRESSED,
            "reason_code": "candidate_budget_exhausted",
            "reason_text": "Candidate budget for the current window is exhausted.",
            "actor_type": "system",
            "actor_id": "agency",
            "policy_snapshot": policy_snapshot,
            "budget_snapshot": budget_snapshot,
            "scheduler_run_id": None,
            "run_id": None,
        }

    if budget.max_auto_exec <= 0 or budget.consumed_auto_exec >= budget.max_auto_exec:
        return {
            "decision": "block",
            "candidate_status": CANDIDATE_STATE_SUPPRESSED,
            "reason_code": "auto_exec_budget_exhausted",
            "reason_text": "Auto-execution budget for the current window is exhausted.",
            "actor_type": "system",
            "actor_id": "agency",
            "policy_snapshot": policy_snapshot,
            "budget_snapshot": budget_snapshot,
            "scheduler_run_id": None,
            "run_id": None,
        }

    if budget.max_estimated_tokens and candidate.estimated_tokens > budget_snapshot["remaining_tokens"]:
        return {
            "decision": "block",
            "candidate_status": CANDIDATE_STATE_SUPPRESSED,
            "reason_code": "token_budget_exhausted",
            "reason_text": "Estimated token cost exceeds the remaining budget.",
            "actor_type": "system",
            "actor_id": "agency",
            "policy_snapshot": policy_snapshot,
            "budget_snapshot": budget_snapshot,
            "scheduler_run_id": None,
            "run_id": None,
        }

    if budget.max_estimated_cost and candidate.estimated_cost > budget_snapshot["remaining_cost"]:
        return {
            "decision": "block",
            "candidate_status": CANDIDATE_STATE_SUPPRESSED,
            "reason_code": "cost_budget_exhausted",
            "reason_text": "Estimated cost exceeds the remaining budget.",
            "actor_type": "system",
            "actor_id": "agency",
            "policy_snapshot": policy_snapshot,
            "budget_snapshot": budget_snapshot,
            "scheduler_run_id": None,
            "run_id": None,
        }

    if _risk_level(candidate.risk_class) >= _risk_level(budget.require_review_above_risk):
        return {
            "decision": "recommend",
            "candidate_status": CANDIDATE_STATE_PROPOSED,
            "reason_code": "review_required",
            "reason_text": "Candidate risk exceeds the auto-execute review threshold.",
            "actor_type": "system",
            "actor_id": "agency",
            "policy_snapshot": policy_snapshot,
            "budget_snapshot": budget_snapshot,
            "scheduler_run_id": None,
            "run_id": None,
        }

    return {
        "decision": "approve",
        "candidate_status": CANDIDATE_STATE_APPROVED,
        "reason_code": "budget_and_policy_ok",
        "reason_text": "Candidate passed bounded policy checks and is eligible for scheduler handoff.",
        "actor_type": "system",
        "actor_id": "agency",
        "policy_snapshot": policy_snapshot,
        "budget_snapshot": budget_snapshot,
        "scheduler_run_id": None,
        "run_id": None,
    }
