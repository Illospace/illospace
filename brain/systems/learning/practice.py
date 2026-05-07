"""Isolated practice-loop records with strict non-production guards."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from brain.platform.db.models.learning import PracticeRun
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)

ALLOWED_ISOLATION_MODES = {"sandbox", "fixture", "mock", "ephemeral"}
VALID_PRACTICE_VISIBILITIES = {"private", "org"}
PRODUCTION_SURFACE_TOKENS = (
    "prod",
    "production",
    "live",
    "real-user",
    "customer",
    "billing",
    "payment",
)


def _clamp_score(value: float) -> float:
    return max(0.0, min(1.0, round(float(value), 3)))


def _clean_text(value) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _practice_scope(
    *,
    user_id: str | None,
    org_id: str | None,
    visibility: str | None = None,
) -> dict:
    resolved_user_id = _clean_text(user_id)
    resolved_org_id = _clean_text(org_id)
    resolved_visibility = _clean_text(visibility) or ("org" if resolved_org_id else "private")
    if not resolved_user_id:
        raise ValueError("practice learning writes require user_id")
    if resolved_visibility not in VALID_PRACTICE_VISIBILITIES:
        raise ValueError(f"invalid practice visibility: {resolved_visibility!r}")
    if resolved_visibility == "org" and not resolved_org_id:
        raise ValueError("org-scoped practice runs require org_id")
    return {
        "user_id": resolved_user_id,
        "org_id": resolved_org_id,
        "visibility": resolved_visibility,
    }


def validate_practice_guardrails(
    *,
    isolation_mode: str,
    workspace_template: str,
    synthesized_task: str,
    touched_production: bool,
) -> None:
    """Fail closed if a practice run could touch production surfaces."""
    if touched_production:
        raise ValueError("practice runs must never be recorded with touched_production=true")

    if isolation_mode not in ALLOWED_ISOLATION_MODES:
        raise ValueError(f"unsupported practice isolation_mode: {isolation_mode!r}")

    haystack = " ".join([workspace_template or "", synthesized_task or ""]).lower()
    if any(token in haystack for token in PRODUCTION_SURFACE_TOKENS):
        raise ValueError("practice runs must stay off production-like targets")


def score_practice_outcome(
    outcome: dict,
    *,
    cost_budget: float,
    isolation_mode: str,
    touched_production: bool = False,
) -> tuple[float, dict]:
    """Convert a practice outcome into a bounded score plus audit breakdown."""
    success = bool(outcome.get("success"))
    quality = outcome.get("quality")
    if quality is None:
        quality = outcome.get("score")
    if quality is None:
        quality = 1.0 if success else 0.0

    actual_cost = outcome.get("actual_cost")
    if actual_cost is None:
        actual_cost = outcome.get("cost_used")
    if actual_cost is None:
        actual_cost = outcome.get("tokens_cost")

    budget = max(float(cost_budget or 0.0), 0.01)
    if actual_cost is None:
        budget_score = 1.0 if success else 0.7
    else:
        overspend_ratio = max(0.0, float(actual_cost) - budget) / budget
        budget_score = max(0.0, 1.0 - overspend_ratio)

    isolation_score = 1.0 if isolation_mode in ALLOWED_ISOLATION_MODES else 0.0
    touch_penalty = 0.6 if touched_production else 0.0
    retry_penalty = min(0.25, 0.05 * float(outcome.get("retries", 0) or 0))
    failure_penalty = 0.35 if not success else 0.0

    base_score = (0.5 * float(quality)) + (0.3 * budget_score) + (0.2 * isolation_score)
    if outcome.get("explicit_score") is not None:
        explicit_score = _clamp_score(outcome["explicit_score"])
        base_score = (base_score + explicit_score) / 2.0

    score = _clamp_score(base_score - touch_penalty - retry_penalty - failure_penalty)
    breakdown = {
        "quality_score": _clamp_score(float(quality)),
        "budget_score": _clamp_score(budget_score),
        "isolation_score": _clamp_score(isolation_score),
        "touch_penalty": round(touch_penalty, 3),
        "retry_penalty": round(retry_penalty, 3),
        "failure_penalty": round(failure_penalty, 3),
    }
    return score, breakdown
def record_practice_run(
    *,
    origin_skill_name: str,
    user_id: str | None,
    org_id: str | None = None,
    visibility: str | None = None,
    synthesized_task: str,
    workspace_template: str,
    cost_budget: float,
    isolation_mode: str,
    origin_policy_promotion_id: int | None = None,
    run_id: int | None = None,
    outcome: dict | None = None,
    score: float | None = None,
    touched_production: bool = False,
    run_status: str = "queued",
) -> dict:
    """Persist an isolated practice run record after validating guardrails."""
    scope = _practice_scope(user_id=user_id, org_id=org_id, visibility=visibility)
    validate_practice_guardrails(
        isolation_mode=isolation_mode,
        workspace_template=workspace_template,
        synthesized_task=synthesized_task,
        touched_production=touched_production,
    )

    payload = {
        "origin_skill_name": origin_skill_name,
        "user_id": scope["user_id"],
        "org_id": scope["org_id"],
        "visibility": scope["visibility"],
        "origin_policy_promotion_id": origin_policy_promotion_id,
        "synthesized_task": synthesized_task,
        "workspace_template": workspace_template,
        "cost_budget": float(cost_budget),
        "isolation_mode": isolation_mode,
        "run_id": run_id,
        "outcome": outcome or {},
        "score": score,
        "touched_production": False,
        "run_status": run_status,
    }

    try:
        with UnitOfWork() as uow:
            target = PracticeRun(
                origin_skill_name=origin_skill_name,
                user_id=scope["user_id"],
                org_id=scope["org_id"],
                visibility=scope["visibility"],
                origin_policy_promotion_id=origin_policy_promotion_id,
                synthesized_task=synthesized_task,
                isolation_mode=isolation_mode,
                workspace_template=workspace_template,
                cost_budget=float(cost_budget),
                run_status=run_status,
                run_id=run_id,
                outcome=outcome or {},
                score=score,
                touched_production=False,
            )
            uow.session.add(target)
            uow.session.flush()
            payload["id"] = target.id
            payload["created_at"] = datetime.now(timezone.utc).isoformat()
            return payload
    except Exception as exc:
        logger.debug("Practice run persistence failed: %s", exc)
        raise


def _finalize_practice_run(
    practice_run_id: int,
    *,
    outcome: dict,
    score: float,
    run_status: str,
    touched_production: bool,
) -> None:
    with UnitOfWork() as uow:
        row = uow.session.get(PracticeRun, practice_run_id)
        if not row:
            raise ValueError(f"practice run {practice_run_id} disappeared before completion")
        row.run_status = run_status
        row.outcome = outcome
        row.score = score
        row.touched_production = touched_production
        uow.session.flush()


def execute_practice_run(
    *,
    origin_skill_name: str,
    user_id: str | None,
    org_id: str | None = None,
    visibility: str | None = None,
    synthesized_task: str,
    workspace_template: str,
    cost_budget: float,
    isolation_mode: str,
    runner: Callable[[dict], dict],
    origin_policy_promotion_id: int | None = None,
    run_id: int | None = None,
    runner_name: str | None = None,
) -> dict:
    """Execute a practice loop in isolation and persist the full lifecycle."""
    if not callable(runner):
        raise TypeError("runner must be callable")
    scope = _practice_scope(user_id=user_id, org_id=org_id, visibility=visibility)

    validate_practice_guardrails(
        isolation_mode=isolation_mode,
        workspace_template=workspace_template,
        synthesized_task=synthesized_task,
        touched_production=False,
    )

    record = record_practice_run(
        origin_skill_name=origin_skill_name,
        user_id=scope["user_id"],
        org_id=scope["org_id"],
        visibility=scope["visibility"],
        synthesized_task=synthesized_task,
        workspace_template=workspace_template,
        cost_budget=cost_budget,
        isolation_mode=isolation_mode,
        origin_policy_promotion_id=origin_policy_promotion_id,
        run_id=run_id,
        outcome={
            "runner_name": runner_name or getattr(runner, "__name__", "practice_runner"),
            "run_status": "running",
        },
        score=None,
        touched_production=False,
        run_status="running",
    )

    execution_context = {
        "origin_skill_name": origin_skill_name,
        "user_id": scope["user_id"],
        "org_id": scope["org_id"],
        "visibility": scope["visibility"],
        "synthesized_task": synthesized_task,
        "workspace_template": workspace_template,
        "cost_budget": float(cost_budget),
        "isolation_mode": isolation_mode,
        "origin_policy_promotion_id": origin_policy_promotion_id,
        "run_id": run_id,
    }

    try:
        outcome = runner(execution_context)
        if not isinstance(outcome, dict):
            raise TypeError("practice runner must return a dict outcome")

        touched_production = bool(outcome.get("touched_production", False))
        validate_practice_guardrails(
            isolation_mode=isolation_mode,
            workspace_template=workspace_template,
            synthesized_task=synthesized_task,
            touched_production=touched_production,
        )

        final_status = outcome.get("run_status") or ("completed" if outcome.get("success") else "failed")
        score, breakdown = score_practice_outcome(
            outcome,
            cost_budget=cost_budget,
            isolation_mode=isolation_mode,
            touched_production=touched_production,
        )
        final_outcome = {
            **outcome,
            "runner_name": runner_name or getattr(runner, "__name__", "practice_runner"),
            "execution_context": execution_context,
            "score_breakdown": breakdown,
        }
        _finalize_practice_run(
            record["id"],
            outcome=final_outcome,
            score=score,
            run_status=final_status,
            touched_production=touched_production,
        )
        record.update(
            {
                "run_status": final_status,
                "outcome": final_outcome,
                "score": score,
                "touched_production": touched_production,
            }
        )
        return record
    except Exception as exc:
        failure_outcome = {
            "success": False,
            "error": str(exc),
            "runner_name": runner_name or getattr(runner, "__name__", "practice_runner"),
            "execution_context": execution_context,
            "touched_production": False,
        }
        failure_score, breakdown = score_practice_outcome(
            failure_outcome,
            cost_budget=cost_budget,
            isolation_mode=isolation_mode,
            touched_production=False,
        )
        failure_outcome["score_breakdown"] = breakdown
        _finalize_practice_run(
            record["id"],
            outcome=failure_outcome,
            score=failure_score,
            run_status="failed",
            touched_production=False,
        )
        record.update(
            {
                "run_status": "failed",
                "outcome": failure_outcome,
                "score": failure_score,
                "touched_production": False,
            }
        )
        logger.debug("Practice run execution failed: %s", exc)
        raise
