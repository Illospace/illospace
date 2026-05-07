"""Learning observatory router.

This surface is intentionally read-only. It exposes admin/debug state without
adding tuning controls to normal chat paths.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import can_manage_system
from brain.app.api.deps import get_db, rate_limit
from brain.platform.db.models.learning import (
    LearningSignal,
    PolicyPromotion,
    PolicyUpdateCandidate,
    TrajectoryEvalCase,
)
from brain.platform.db.models.skill_quality import SkillRunEvidence
from brain.systems.learning.budget import BudgetLane, LearningBudgetPolicy
from brain.systems.learning.read_models import build_learning_observatory_read_model


router = APIRouter(
    prefix="/api/learning",
    tags=["learning"],
    dependencies=[Depends(rate_limit)],
)


@router.get("/observatory")
def learning_observatory(
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    if not can_manage_system(user):
        raise HTTPException(status_code=403, detail="Permission denied")

    org_id = str(user["org_id"]) if user.get("org_id") else None
    return build_learning_observatory_from_db(
        db,
        org_id=org_id,
        include_all_orgs=bool(user.get("internal") and org_id is None),
        limit=limit,
    ).to_payload()


def build_learning_observatory_from_db(
    db: Session,
    *,
    org_id: str | None,
    include_all_orgs: bool = False,
    limit: int = 100,
):
    warnings: list[str] = []
    resolved_limit = max(1, min(500, int(limit or 100)))

    learning_signals = _safe_recent_rows(
        db,
        LearningSignal,
        org_id=org_id,
        include_all_orgs=include_all_orgs,
        limit=resolved_limit,
        warnings=warnings,
        label="learning signals",
    )
    eval_cases = _safe_recent_rows(
        db,
        TrajectoryEvalCase,
        org_id=org_id,
        include_all_orgs=include_all_orgs,
        limit=resolved_limit,
        warnings=warnings,
        label="trajectory eval cases",
    )
    skill_evidence = _safe_recent_rows(
        db,
        SkillRunEvidence,
        org_id=org_id,
        include_all_orgs=include_all_orgs,
        limit=resolved_limit,
        warnings=warnings,
        label="skill quality evidence",
    )
    policy_candidates = _safe_recent_rows(
        db,
        PolicyUpdateCandidate,
        org_id=org_id,
        include_all_orgs=include_all_orgs,
        limit=resolved_limit,
        warnings=warnings,
        label="policy update candidates",
    )
    policy_promotions = _safe_recent_rows(
        db,
        PolicyPromotion,
        org_id=org_id,
        include_all_orgs=include_all_orgs,
        limit=resolved_limit,
        warnings=warnings,
        label="policy promotions",
    )

    return build_learning_observatory_read_model(
        outcome_sources=(*learning_signals, *eval_cases, *skill_evidence),
        skill_quality_sources=skill_evidence,
        context_sources=learning_signals,
        stale_conflict_sources=(*learning_signals, *policy_candidates),
        night_budget_source=_night_budget_usage_from_signals(learning_signals),
        policy_sources=(*policy_candidates, *policy_promotions),
        generated_at=datetime.now(timezone.utc),
        scope={
            "org_id": org_id,
            "include_all_orgs": include_all_orgs,
            "limit": resolved_limit,
        },
        warnings=warnings,
    )


def _safe_recent_rows(
    db: Session,
    model: Any,
    *,
    org_id: str | None,
    include_all_orgs: bool,
    limit: int,
    warnings: list[str],
    label: str,
) -> tuple[Any, ...]:
    try:
        stmt = select(model)
        if org_id and not include_all_orgs and hasattr(model, "org_id"):
            stmt = stmt.where(model.org_id == org_id)
        if hasattr(model, "created_at"):
            stmt = stmt.order_by(model.created_at.desc())
        stmt = stmt.limit(limit)
        return tuple(db.scalars(stmt).all())
    except SQLAlchemyError:
        _rollback_quietly(db)
        warnings.append(f"{label} unavailable")
        return ()


def _night_budget_usage_from_signals(signals: tuple[Any, ...]) -> dict[str, Any]:
    policy = LearningBudgetPolicy.from_env()
    budget_tokens = policy.limit_for(BudgetLane.NIGHT)
    items: list[dict[str, Any]] = []
    spent_tokens = 0
    spent_by_tenant: dict[str, int] = {}
    spent_by_work_type: dict[str, int] = {}

    for signal in signals:
        payload = _mapping(getattr(signal, "payload", None))
        budget = _mapping(payload.get("budget"))
        if budget.get("lane") != BudgetLane.NIGHT.value:
            continue
        tokens = (
            _int(budget.get("would_spend_tokens"))
            or _int(_mapping(budget.get("cost_estimate")).get("estimated_tokens"))
            or 0
        )
        work_type = str(payload.get("job_type") or getattr(signal, "signal_type", None) or "night")
        tenant = _tenant_key(signal)
        item = {
            "candidate": {
                "work_type": work_type,
                "estimated_tokens": tokens,
                "org_id": getattr(signal, "org_id", None),
                "user_id": getattr(signal, "user_id", None),
            },
            "decision": budget,
            "tenant_key": tenant,
        }
        items.append(item)
        if budget.get("allowed") is True or budget.get("action") == "allow":
            spent_tokens += tokens
            spent_by_tenant[tenant] = spent_by_tenant.get(tenant, 0) + tokens
            spent_by_work_type[work_type] = spent_by_work_type.get(work_type, 0) + tokens

    return {
        "budget_tokens": budget_tokens,
        "spent_tokens": spent_tokens,
        "remaining_tokens": max(0, budget_tokens - spent_tokens),
        "items": items,
        "spent_by_tenant": spent_by_tenant,
        "spent_by_work_type": spent_by_work_type,
    }


def _tenant_key(row: Any) -> str:
    org_id = getattr(row, "org_id", None)
    if org_id:
        return f"org:{org_id}"
    user_id = getattr(row, "user_id", None)
    if user_id:
        return f"user:{user_id}"
    return "global"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rollback_quietly(db: Session) -> None:
    try:
        db.rollback()
    except Exception:
        pass
