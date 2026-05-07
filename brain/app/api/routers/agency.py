"""Agency review and budget ledger routes."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from brain.systems.agency.core import (
    approve_candidate_review,
    reject_candidate_review,
    suppress_candidate_review,
)
from brain.app.api.auth import get_current_user
from brain.app.api.authorization import can_manage_scheduler
from brain.app.api.deps import get_db, rate_limit
from brain.platform.db.models.agency import (
    CANDIDATE_STATES,
    AgencyApproval,
    AgencyBudgetEvent,
    AgencyCandidate,
    AgencyDecision,
)

router = APIRouter(
    prefix="/api/agency",
    tags=["agency"],
    dependencies=[Depends(rate_limit)],
)


class AgencyApprovalRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    expires_at: datetime | None = None


class AgencyRejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class AgencySuppressRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    suppress_until: datetime | None = None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _is_agency_reviewer(user: dict[str, Any]) -> bool:
    return can_manage_scheduler(user) or user.get("role") in {"owner", "admin", "service"}


def _require_agency_reviewer(user: dict[str, Any]) -> None:
    if not _is_agency_reviewer(user):
        raise HTTPException(status_code=403, detail="Agency review permission required")


def _visible_candidate_predicate(user: dict[str, Any]):
    if _is_agency_reviewer(user) and user.get("principal_type") == "service":
        return None
    clauses = []
    user_id = user.get("id")
    org_id = user.get("org_id")
    if user_id:
        clauses.append(AgencyCandidate.user_id == str(user_id))
    if org_id:
        clauses.append(AgencyCandidate.org_id == str(org_id))
    if _is_agency_reviewer(user):
        clauses.append(AgencyCandidate.org_id.is_(None) & AgencyCandidate.user_id.is_(None))
    if not clauses:
        return AgencyCandidate.id == -1
    return or_(*clauses)


def _get_candidate_or_404(db: Session, candidate_id: int, user: dict[str, Any]) -> AgencyCandidate:
    stmt = select(AgencyCandidate).where(AgencyCandidate.id == candidate_id)
    predicate = _visible_candidate_predicate(user)
    if predicate is not None:
        stmt = stmt.where(predicate)
    candidate = db.scalars(stmt).first()
    if candidate is None:
        raise HTTPException(status_code=404, detail="Agency candidate not found")
    return candidate


def _serialize_decision(decision: AgencyDecision) -> dict[str, Any]:
    return {
        "id": decision.id,
        "candidate_id": decision.candidate_id,
        "decision": decision.decision,
        "actor_type": decision.actor_type,
        "actor_id": decision.actor_id,
        "reason_code": decision.reason_code,
        "reason_text": decision.reason_text,
        "policy_snapshot": decision.policy_snapshot or {},
        "budget_snapshot": decision.budget_snapshot or {},
        "scheduler_run_id": decision.scheduler_run_id,
        "run_id": decision.run_id,
        "created_at": _iso(decision.created_at),
    }


def _serialize_approval(approval: AgencyApproval | None) -> dict[str, Any] | None:
    if approval is None:
        return None
    return {
        "id": approval.id,
        "candidate_id": approval.candidate_id,
        "actor_id": approval.actor_id,
        "actor_role": approval.actor_role,
        "approval_kind": approval.approval_kind,
        "reason": approval.reason,
        "expires_at": _iso(approval.expires_at),
        "active": approval.active,
        "created_at": _iso(approval.created_at),
    }


def _serialize_candidate(candidate: AgencyCandidate, *, include_review: bool = False) -> dict[str, Any]:
    payload = {
        "id": candidate.id,
        "candidate_key": candidate.candidate_key,
        "drive_type": candidate.drive_type,
        "source_type": candidate.source_type,
        "source_refs": candidate.source_refs or [],
        "org_id": candidate.org_id,
        "user_id": candidate.user_id,
        "target_binding_id": candidate.target_binding_id,
        "proposal_kind": candidate.proposal_kind,
        "proposed_run_payload": candidate.proposed_run_payload or {},
        "risk_class": candidate.risk_class,
        "reversibility_class": candidate.reversibility_class,
        "expected_value": candidate.expected_value,
        "novelty_score": candidate.novelty_score,
        "urgency_score": candidate.urgency_score,
        "estimated_cost": candidate.estimated_cost,
        "estimated_tokens": candidate.estimated_tokens,
        "status": candidate.status,
        "suppression_until": _iso(candidate.suppression_until),
        "expires_at": _iso(candidate.expires_at),
        "created_at": _iso(candidate.created_at),
        "updated_at": _iso(candidate.updated_at),
    }
    if include_review:
        payload["decisions"] = [
            _serialize_decision(decision)
            for decision in getattr(candidate, "_agency_decisions", [])
        ]
        payload["approvals"] = [
            _serialize_approval(approval)
            for approval in getattr(candidate, "_agency_approvals", [])
        ]
    return payload


def _serialize_budget_event(event: AgencyBudgetEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "budget_id": event.budget_id,
        "candidate_id": event.candidate_id,
        "decision_id": event.decision_id,
        "event_type": event.event_type,
        "scope_type": event.scope_type,
        "scope_id": event.scope_id,
        "drive_type": event.drive_type,
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "reason_code": event.reason_code,
        "delta_candidates": event.delta_candidates,
        "delta_auto_exec": event.delta_auto_exec,
        "delta_cost": event.delta_cost,
        "delta_tokens": event.delta_tokens,
        "before_snapshot": event.before_snapshot or {},
        "after_snapshot": event.after_snapshot or {},
        "metadata": event.event_metadata or {},
        "created_at": _iso(event.created_at),
    }


@router.get("/candidates")
def list_candidates(
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    if status is not None and status not in CANDIDATE_STATES:
        raise HTTPException(status_code=400, detail="Unknown agency candidate status")
    stmt = select(AgencyCandidate)
    predicate = _visible_candidate_predicate(user)
    if predicate is not None:
        stmt = stmt.where(predicate)
    if status is not None:
        stmt = stmt.where(AgencyCandidate.status == status)
    stmt = stmt.order_by(AgencyCandidate.created_at.desc()).limit(max(1, min(limit, 200)))
    return [_serialize_candidate(candidate) for candidate in db.scalars(stmt).all()]


@router.get("/candidates/{candidate_id}")
def get_candidate(
    candidate_id: int,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    candidate = _get_candidate_or_404(db, candidate_id, user)
    decisions = db.scalars(
        select(AgencyDecision)
        .where(AgencyDecision.candidate_id == candidate.id)
        .order_by(AgencyDecision.created_at.desc())
    ).all()
    approvals = db.scalars(
        select(AgencyApproval)
        .where(AgencyApproval.candidate_id == candidate.id)
        .order_by(AgencyApproval.created_at.desc())
    ).all()
    setattr(candidate, "_agency_decisions", decisions)
    setattr(candidate, "_agency_approvals", approvals)
    return _serialize_candidate(candidate, include_review=True)


@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(
    candidate_id: int,
    body: AgencyApprovalRequest,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_agency_reviewer(user)
    _get_candidate_or_404(db, candidate_id, user)
    try:
        candidate, decision, approval = approve_candidate_review(
            db,
            candidate_id,
            actor_id=str(user["id"]),
            actor_role=str(user.get("role") or "unknown"),
            actor_type=str(user.get("principal_type") or "human"),
            reason=body.reason,
            expires_at=body.expires_at,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "candidate": _serialize_candidate(candidate),
        "decision": _serialize_decision(decision),
        "approval": _serialize_approval(approval),
    }


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(
    candidate_id: int,
    body: AgencyRejectRequest,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_agency_reviewer(user)
    _get_candidate_or_404(db, candidate_id, user)
    try:
        candidate, decision = reject_candidate_review(
            db,
            candidate_id,
            actor_id=str(user["id"]),
            actor_role=str(user.get("role") or "unknown"),
            actor_type=str(user.get("principal_type") or "human"),
            reason=body.reason,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "candidate": _serialize_candidate(candidate),
        "decision": _serialize_decision(decision),
    }


@router.post("/candidates/{candidate_id}/suppress")
def suppress_candidate(
    candidate_id: int,
    body: AgencySuppressRequest,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_agency_reviewer(user)
    _get_candidate_or_404(db, candidate_id, user)
    try:
        candidate, decision = suppress_candidate_review(
            db,
            candidate_id,
            actor_id=str(user["id"]),
            actor_role=str(user.get("role") or "unknown"),
            actor_type=str(user.get("principal_type") or "human"),
            reason=body.reason,
            suppress_until=body.suppress_until,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "candidate": _serialize_candidate(candidate),
        "decision": _serialize_decision(decision),
    }


@router.get("/budget-events")
def list_budget_events(
    candidate_id: int | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_agency_reviewer(user)
    stmt = select(AgencyBudgetEvent)
    if candidate_id is not None:
        _get_candidate_or_404(db, candidate_id, user)
        stmt = stmt.where(AgencyBudgetEvent.candidate_id == candidate_id)
    elif not (_is_agency_reviewer(user) and user.get("principal_type") == "service"):
        org_id = user.get("org_id")
        if org_id:
            stmt = stmt.where(AgencyBudgetEvent.scope_id == str(org_id))
        else:
            stmt = stmt.where(AgencyBudgetEvent.actor_id == str(user.get("id")))
    stmt = stmt.order_by(AgencyBudgetEvent.created_at.desc()).limit(max(1, min(limit, 500)))
    return [_serialize_budget_event(event) for event in db.scalars(stmt).all()]
