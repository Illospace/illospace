"""Agency candidate generation and persistence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select, text

from brain.platform.db.models.agency import (
    CANDIDATE_STATE_APPROVED,
    CANDIDATE_STATE_AUTO_EXECUTED,
    CANDIDATE_STATE_EXPIRED,
    CANDIDATE_STATE_PROPOSED,
    CANDIDATE_STATE_REJECTED,
    CANDIDATE_STATE_SUPPRESSED,
    AgencyApproval,
    AgencyBudget,
    AgencyBudgetEvent,
    AgencyCandidate,
    AgencyDecision,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork

from .handoff import materialize_scheduler_handoff
from .policy import budget_snapshot_for_candidate, evaluate_candidate, evaluate_candidate_budget
from brain.app.scheduler.executor import execute_scheduler_run
from brain.systems.runs.token_usage import summarize_run_usage


@dataclass(frozen=True)
class CandidateSpec:
    """Normalized in-memory candidate specification."""

    drive_type: str
    source_type: str
    source_refs: list[dict[str, Any]]
    proposal_kind: str
    proposed_run_payload: dict[str, Any]
    org_id: str | None = None
    user_id: str | None = None
    target_binding_id: str | None = None
    risk_class: str = "low"
    reversibility_class: str = "read_only"
    expected_value: float = 0.0
    novelty_score: float = 0.0
    urgency_score: float = 0.0
    estimated_cost: float = 0.0
    estimated_tokens: int = 0
    status: str = CANDIDATE_STATE_PROPOSED
    suppression_until: datetime | None = None
    expires_at: datetime | None = None


def _normalize_source_refs(source_refs: Iterable[dict[str, Any] | str]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for ref in source_refs:
        if isinstance(ref, str):
            refs.append({"kind": "reference", "value": ref})
        elif isinstance(ref, dict):
            refs.append(ref)
        else:
            refs.append({"kind": "reference", "value": str(ref)})
    return refs


def _candidate_key(spec: CandidateSpec) -> str:
    payload = {
        "drive_type": spec.drive_type,
        "source_type": spec.source_type,
        "source_refs": spec.source_refs,
        "proposal_kind": spec.proposal_kind,
        "target_binding_id": spec.target_binding_id,
        "proposed_run_payload": spec.proposed_run_payload,
    }
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _as_spec(
    *,
    drive_type: str,
    source_type: str,
    source_refs: Iterable[dict[str, Any] | str],
    proposal_kind: str,
    proposed_run_payload: dict[str, Any],
    org_id: str | None = None,
    user_id: str | None = None,
    target_binding_id: str | None = None,
    risk_class: str = "low",
    reversibility_class: str = "read_only",
    expected_value: float = 0.0,
    novelty_score: float = 0.0,
    urgency_score: float = 0.0,
    estimated_cost: float = 0.0,
    estimated_tokens: int = 0,
    status: str = CANDIDATE_STATE_PROPOSED,
    suppression_until: datetime | None = None,
    expires_at: datetime | None = None,
) -> CandidateSpec:
    refs = _normalize_source_refs(source_refs)
    if not refs:
        raise ValueError("agency candidates require evidence pointers")
    return CandidateSpec(
        drive_type=drive_type,
        source_type=source_type,
        source_refs=refs,
        proposal_kind=proposal_kind,
        proposed_run_payload=proposed_run_payload,
        org_id=org_id,
        user_id=user_id,
        target_binding_id=target_binding_id,
        risk_class=risk_class,
        reversibility_class=reversibility_class,
        expected_value=expected_value,
        novelty_score=novelty_score,
        urgency_score=urgency_score,
        estimated_cost=estimated_cost,
        estimated_tokens=estimated_tokens,
        status=status,
        suppression_until=suppression_until,
        expires_at=expires_at,
    )


def _persist_candidate(
    session,
    spec: CandidateSpec,
    *,
    now: datetime | None = None,
) -> AgencyCandidate:
    now = now or datetime.now(timezone.utc)
    candidate_key = _candidate_key(spec)
    existing = session.scalar(
        select(AgencyCandidate).where(AgencyCandidate.candidate_key == candidate_key)
    )
    if existing is not None:
        if existing.suppression_until and existing.suppression_until > now:
            return existing
        existing.drive_type = spec.drive_type
        existing.source_type = spec.source_type
        existing.source_refs = spec.source_refs
        existing.org_id = spec.org_id
        existing.user_id = spec.user_id
        existing.target_binding_id = spec.target_binding_id
        existing.proposal_kind = spec.proposal_kind
        existing.proposed_run_payload = spec.proposed_run_payload
        existing.risk_class = spec.risk_class
        existing.reversibility_class = spec.reversibility_class
        existing.expected_value = spec.expected_value
        existing.novelty_score = spec.novelty_score
        existing.urgency_score = spec.urgency_score
        existing.estimated_cost = spec.estimated_cost
        existing.estimated_tokens = spec.estimated_tokens
        existing.status = spec.status
        existing.expires_at = spec.expires_at
        if spec.suppression_until:
            existing.suppression_until = spec.suppression_until
        session.flush()
        return existing

    candidate = AgencyCandidate(
        candidate_key=candidate_key,
        drive_type=spec.drive_type,
        source_type=spec.source_type,
        source_refs=spec.source_refs,
        org_id=spec.org_id,
        user_id=spec.user_id,
        target_binding_id=spec.target_binding_id,
        proposal_kind=spec.proposal_kind,
        proposed_run_payload=spec.proposed_run_payload,
        risk_class=spec.risk_class,
        reversibility_class=spec.reversibility_class,
        expected_value=spec.expected_value,
        novelty_score=spec.novelty_score,
        urgency_score=spec.urgency_score,
        estimated_cost=spec.estimated_cost,
        estimated_tokens=spec.estimated_tokens,
        status=spec.status,
        suppression_until=spec.suppression_until,
        expires_at=spec.expires_at,
    )
    session.add(candidate)
    session.flush()
    return candidate


def record_candidate(
    *,
    drive_type: str,
    source_type: str,
    source_refs: Iterable[dict[str, Any] | str],
    proposal_kind: str,
    proposed_run_payload: dict[str, Any],
    org_id: str | None = None,
    user_id: str | None = None,
    target_binding_id: str | None = None,
    risk_class: str = "low",
    reversibility_class: str = "read_only",
    expected_value: float = 0.0,
    novelty_score: float = 0.0,
    urgency_score: float = 0.0,
    estimated_cost: float = 0.0,
    estimated_tokens: int = 0,
    status: str = CANDIDATE_STATE_PROPOSED,
    suppression_until: datetime | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> AgencyCandidate:
    """Persist a candidate with fingerprint dedupe."""
    spec = _as_spec(
        drive_type=drive_type,
        source_type=source_type,
        source_refs=source_refs,
        proposal_kind=proposal_kind,
        proposed_run_payload=proposed_run_payload,
        org_id=org_id,
        user_id=user_id,
        target_binding_id=target_binding_id,
        risk_class=risk_class,
        reversibility_class=reversibility_class,
        expected_value=expected_value,
        novelty_score=novelty_score,
        urgency_score=urgency_score,
        estimated_cost=estimated_cost,
        estimated_tokens=estimated_tokens,
        status=status,
        suppression_until=suppression_until,
        expires_at=expires_at,
    )
    with UnitOfWork() as uow:
        return _persist_candidate(uow.session, spec, now=now)


def _empty_reservation() -> dict[str, Any]:
    return {
        "reserved_candidates": 0,
        "reserved_auto_exec": 0,
        "reserved_cost": 0.0,
        "reserved_tokens": 0,
    }


def _snapshot_with_reservation(
    snapshot: dict[str, Any],
    reservation: dict[str, Any] | None = None,
    *,
    denied: bool = False,
    reason_code: str | None = None,
) -> dict[str, Any]:
    enriched = {
        **snapshot,
        "reservation": reservation or _empty_reservation(),
    }
    if denied:
        enriched["reservation_denied"] = True
    if reason_code:
        enriched["reservation_reason_code"] = reason_code
    return enriched


def _record_budget_event(
    session,
    *,
    event_type: str,
    candidate: AgencyCandidate,
    budget: AgencyBudget | None,
    before_snapshot: dict[str, Any],
    after_snapshot: dict[str, Any],
    delta_candidates: int = 0,
    delta_auto_exec: int = 0,
    delta_cost: float = 0.0,
    delta_tokens: int = 0,
    actor_type: str = "system",
    actor_id: str | None = "agency",
    reason_code: str | None = None,
    decision_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgencyBudgetEvent:
    event = AgencyBudgetEvent(
        budget_id=budget.id if budget is not None else None,
        candidate_id=candidate.id,
        decision_id=decision_id,
        event_type=event_type,
        scope_type=after_snapshot["scope_type"],
        scope_id=after_snapshot["scope_id"],
        drive_type=after_snapshot.get("drive_type"),
        actor_type=actor_type,
        actor_id=actor_id,
        reason_code=reason_code,
        delta_candidates=delta_candidates,
        delta_auto_exec=delta_auto_exec,
        delta_cost=delta_cost,
        delta_tokens=delta_tokens,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        event_metadata=metadata or {},
    )
    session.add(event)
    session.flush()
    return event


def reserve_candidate_budget(
    session,
    candidate: AgencyCandidate,
    *,
    actor_type: str = "system",
    actor_id: str | None = "agency",
    reason_code: str = "candidate_budget_reserved",
    decision_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reserve one candidate slot for an approved or recommended proposal."""
    now = now or datetime.now(timezone.utc)
    budget = evaluate_candidate_budget(session, candidate, now=now)
    before = budget_snapshot_for_candidate(budget, candidate, now=now)
    if budget is None:
        snapshot = _snapshot_with_reservation(before, reason_code="no_active_budget")
        _record_budget_event(
            session,
            event_type="candidate_reservation_skipped",
            candidate=candidate,
            budget=None,
            before_snapshot=before,
            after_snapshot=snapshot,
            actor_type=actor_type,
            actor_id=actor_id,
            reason_code="no_active_budget",
            decision_id=decision_id,
        )
        return snapshot

    if budget.max_candidates <= 0 or budget.consumed_candidates >= budget.max_candidates:
        snapshot = _snapshot_with_reservation(
            before,
            denied=True,
            reason_code="candidate_budget_exhausted",
        )
        _record_budget_event(
            session,
            event_type="candidate_reservation_denied",
            candidate=candidate,
            budget=budget,
            before_snapshot=before,
            after_snapshot=snapshot,
            actor_type=actor_type,
            actor_id=actor_id,
            reason_code="candidate_budget_exhausted",
            decision_id=decision_id,
        )
        return snapshot

    budget.consumed_candidates = max(0, budget.consumed_candidates + 1)
    session.flush()
    reservation = {
        "reserved_candidates": 1,
        "reserved_auto_exec": 0,
        "reserved_cost": 0.0,
        "reserved_tokens": 0,
    }
    snapshot = _snapshot_with_reservation(
        budget_snapshot_for_candidate(budget, candidate, now=now),
        reservation,
        reason_code=reason_code,
    )
    _record_budget_event(
        session,
        event_type="candidate_reserved",
        candidate=candidate,
        budget=budget,
        before_snapshot=before,
        after_snapshot=snapshot,
        delta_candidates=1,
        actor_type=actor_type,
        actor_id=actor_id,
        reason_code=reason_code,
        decision_id=decision_id,
    )
    return snapshot


def reserve_auto_exec_budget(
    session,
    candidate: AgencyCandidate,
    *,
    actor_type: str = "system",
    actor_id: str | None = "agency",
    reason_code: str = "auto_exec_budget_reserved",
    decision_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reserve auto-execution budget for a scheduler-backed handoff."""
    now = now or datetime.now(timezone.utc)
    budget = evaluate_candidate_budget(session, candidate, now=now)
    before = budget_snapshot_for_candidate(budget, candidate, now=now)
    if budget is None:
        snapshot = _snapshot_with_reservation(
            before,
            denied=True,
            reason_code="no_active_budget_for_auto_exec",
        )
        _record_budget_event(
            session,
            event_type="auto_exec_reservation_denied",
            candidate=candidate,
            budget=None,
            before_snapshot=before,
            after_snapshot=snapshot,
            actor_type=actor_type,
            actor_id=actor_id,
            reason_code="no_active_budget_for_auto_exec",
            decision_id=decision_id,
        )
        return snapshot

    estimated_cost = float(candidate.estimated_cost or 0.0)
    estimated_tokens = int(candidate.estimated_tokens or 0)
    denial_reason: str | None = None
    if budget.max_auto_exec <= 0 or budget.consumed_auto_exec >= budget.max_auto_exec:
        denial_reason = "auto_exec_budget_exhausted"
    elif budget.max_estimated_cost and estimated_cost > before["remaining_cost"]:
        denial_reason = "cost_budget_exhausted"
    elif budget.max_estimated_tokens and estimated_tokens > before["remaining_tokens"]:
        denial_reason = "token_budget_exhausted"
    if denial_reason:
        snapshot = _snapshot_with_reservation(
            before,
            denied=True,
            reason_code=denial_reason,
        )
        _record_budget_event(
            session,
            event_type="auto_exec_reservation_denied",
            candidate=candidate,
            budget=budget,
            before_snapshot=before,
            after_snapshot=snapshot,
            actor_type=actor_type,
            actor_id=actor_id,
            reason_code=denial_reason,
            decision_id=decision_id,
        )
        return snapshot

    budget.consumed_auto_exec = max(0, budget.consumed_auto_exec + 1)
    budget.consumed_cost = max(0.0, budget.consumed_cost + estimated_cost)
    budget.consumed_tokens = max(0, budget.consumed_tokens + estimated_tokens)
    session.flush()
    reservation = {
        "reserved_candidates": 0,
        "reserved_auto_exec": 1,
        "reserved_cost": estimated_cost,
        "reserved_tokens": estimated_tokens,
    }
    snapshot = _snapshot_with_reservation(
        budget_snapshot_for_candidate(budget, candidate, now=now),
        reservation,
        reason_code=reason_code,
    )
    _record_budget_event(
        session,
        event_type="auto_exec_reserved",
        candidate=candidate,
        budget=budget,
        before_snapshot=before,
        after_snapshot=snapshot,
        delta_auto_exec=1,
        delta_cost=estimated_cost,
        delta_tokens=estimated_tokens,
        actor_type=actor_type,
        actor_id=actor_id,
        reason_code=reason_code,
        decision_id=decision_id,
    )
    return snapshot


def release_budget(
    session,
    candidate: AgencyCandidate,
    *,
    candidate_slots: int = 1,
    auto_exec_slots: int = 0,
    cost: float | None = None,
    tokens: int | None = None,
    actor_type: str = "system",
    actor_id: str | None = "agency",
    reason_code: str = "budget_released",
    decision_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Release a prior budget reservation after cancellation or failure."""
    now = now or datetime.now(timezone.utc)
    budget = evaluate_candidate_budget(session, candidate, now=now)
    before = budget_snapshot_for_candidate(budget, candidate, now=now)
    if budget is None:
        snapshot = _snapshot_with_reservation(before, reason_code="no_active_budget")
        _record_budget_event(
            session,
            event_type="release_skipped",
            candidate=candidate,
            budget=None,
            before_snapshot=before,
            after_snapshot=snapshot,
            actor_type=actor_type,
            actor_id=actor_id,
            reason_code="no_active_budget",
            decision_id=decision_id,
        )
        return snapshot

    release_cost = float(cost if cost is not None else candidate.estimated_cost or 0.0)
    release_tokens = int(tokens if tokens is not None else candidate.estimated_tokens or 0)
    if candidate_slots:
        budget.consumed_candidates = max(0, budget.consumed_candidates - candidate_slots)
    if auto_exec_slots:
        budget.consumed_auto_exec = max(0, budget.consumed_auto_exec - auto_exec_slots)
        budget.consumed_cost = max(0.0, budget.consumed_cost - release_cost)
        budget.consumed_tokens = max(0, budget.consumed_tokens - release_tokens)
    session.flush()
    reservation = {
        "reserved_candidates": -candidate_slots,
        "reserved_auto_exec": -auto_exec_slots,
        "reserved_cost": -release_cost,
        "reserved_tokens": -release_tokens,
    }
    snapshot = _snapshot_with_reservation(
        budget_snapshot_for_candidate(budget, candidate, now=now),
        reservation,
        reason_code=reason_code,
    )
    _record_budget_event(
        session,
        event_type="budget_released",
        candidate=candidate,
        budget=budget,
        before_snapshot=before,
        after_snapshot=snapshot,
        delta_candidates=-candidate_slots,
        delta_auto_exec=-auto_exec_slots,
        delta_cost=-release_cost,
        delta_tokens=-release_tokens,
        actor_type=actor_type,
        actor_id=actor_id,
        reason_code=reason_code,
        decision_id=decision_id,
    )
    return snapshot


def _scheduler_run_snapshot(run) -> dict[str, Any]:
    return {
        "id": run.id,
        "job_id": run.job_id,
        "status": run.status,
        "lease_id": run.lease_id,
        "agent_run_id": getattr(run, "agent_run_id", None),
        "scheduled_for": run.scheduled_for.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "result_summary": run.result_summary or {},
        "error_text": run.error_text,
    }


def _candidate_status_from_scheduler_run(run_status: str | None) -> str | None:
    if run_status is None:
        return None
    if run_status == "settled_success":
        return CANDIDATE_STATE_AUTO_EXECUTED
    if run_status in {"blocked", "failed", "settled_failure"}:
        return CANDIDATE_STATE_SUPPRESSED
    return CANDIDATE_STATE_APPROVED


def _record_approval(
    session,
    candidate: AgencyCandidate,
    *,
    actor_id: str,
    actor_role: str,
    reason: str,
    approval_kind: str,
    expires_at: datetime | None = None,
) -> AgencyApproval:
    approval = AgencyApproval(
        candidate_id=candidate.id,
        actor_id=actor_id,
        actor_role=actor_role,
        approval_kind=approval_kind,
        reason=reason,
        expires_at=expires_at,
        active=True,
    )
    session.add(approval)
    session.flush()
    return approval


def _merge_reservations(*snapshots: dict[str, Any]) -> dict[str, Any]:
    merged = _empty_reservation()
    for snapshot in snapshots:
        reservation = snapshot.get("reservation") or {}
        merged["reserved_candidates"] += int(reservation.get("reserved_candidates") or 0)
        merged["reserved_auto_exec"] += int(reservation.get("reserved_auto_exec") or 0)
        merged["reserved_cost"] += float(reservation.get("reserved_cost") or 0.0)
        merged["reserved_tokens"] += int(reservation.get("reserved_tokens") or 0)
    return merged


def record_decision(
    *,
    candidate_id: int,
    decision: str,
    reason_code: str,
    reason_text: str,
    policy_snapshot: dict[str, Any],
    budget_snapshot: dict[str, Any],
    actor_type: str = "system",
    actor_id: str | None = "agency",
    scheduler_run_id: int | None = None,
    run_id: int | None = None,
) -> AgencyDecision:
    """Persist a review decision and update candidate state."""
    with UnitOfWork() as uow:
        candidate = uow.session.get(AgencyCandidate, candidate_id)
        if candidate is None:
            raise LookupError(f"AgencyCandidate {candidate_id} not found")

        if reason_code == "candidate_expired":
            candidate.status = CANDIDATE_STATE_EXPIRED
        elif decision in {"block", "defer", "suppress"}:
            cooldown_hours = int(budget_snapshot.get("cooldown_hours") or 24)
            candidate.suppression_until = datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)
            candidate.status = CANDIDATE_STATE_SUPPRESSED
        elif decision == "approve":
            candidate.status = CANDIDATE_STATE_APPROVED
        elif decision == "recommend":
            candidate.status = CANDIDATE_STATE_PROPOSED
        elif decision == "reject":
            candidate.status = CANDIDATE_STATE_REJECTED
        else:
            candidate.status = decision

        decision_row = AgencyDecision(
            candidate_id=candidate_id,
            decision=decision,
            actor_type=actor_type,
            actor_id=actor_id,
            reason_code=reason_code,
            reason_text=reason_text,
            policy_snapshot=policy_snapshot,
            budget_snapshot=budget_snapshot,
            scheduler_run_id=scheduler_run_id,
            run_id=run_id,
        )
        uow.session.add(decision_row)
        uow.session.flush()
        return decision_row


def _manual_review_snapshots(
    session,
    candidate: AgencyCandidate,
    *,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evaluation = evaluate_candidate(session, candidate, now=now)
    return evaluation["policy_snapshot"], evaluation["budget_snapshot"]


def approve_candidate_review(
    session,
    candidate_id: int,
    *,
    actor_id: str,
    actor_role: str,
    reason: str,
    expires_at: datetime | None = None,
    actor_type: str = "human",
    now: datetime | None = None,
) -> tuple[AgencyCandidate, AgencyDecision, AgencyApproval | None]:
    """Approve a candidate and connect it to existing execution primitives when safe."""
    now = now or datetime.now(timezone.utc)
    reason = reason.strip()
    if not reason:
        raise ValueError("Approval reason is required")
    if expires_at and expires_at <= now:
        raise ValueError("Approval expiry must be in the future")

    candidate = session.get(AgencyCandidate, candidate_id)
    if candidate is None:
        raise LookupError(f"AgencyCandidate {candidate_id} not found")
    if candidate.expires_at and candidate.expires_at <= now:
        candidate.status = CANDIDATE_STATE_EXPIRED
        raise ValueError("Candidate has expired")
    if candidate.suppression_until and candidate.suppression_until > now:
        candidate.status = CANDIDATE_STATE_SUPPRESSED
        raise ValueError("Candidate is suppressed")

    policy_snapshot, budget_snapshot = _manual_review_snapshots(session, candidate, now=now)
    record = AgencyDecision(
        candidate_id=candidate.id,
        decision="approve",
        actor_type=actor_type,
        actor_id=actor_id,
        reason_code="manual_approval",
        reason_text=reason,
        policy_snapshot=policy_snapshot,
        budget_snapshot=budget_snapshot,
    )
    session.add(record)
    session.flush()

    reserved_snapshot = reserve_candidate_budget(
        session,
        candidate,
        actor_type=actor_type,
        actor_id=actor_id,
        reason_code="manual_approval_candidate_slot",
        decision_id=record.id,
        now=now,
    )
    if reserved_snapshot.get("reservation_denied"):
        record.decision = "block"
        record.reason_code = reserved_snapshot.get(
            "reservation_reason_code",
            "candidate_budget_exhausted",
        )
        record.reason_text = "Candidate budget is exhausted; approval was not granted."
        record.budget_snapshot = reserved_snapshot
        candidate.status = CANDIDATE_STATE_SUPPRESSED
        cooldown_hours = int(reserved_snapshot.get("cooldown_hours") or 24)
        candidate.suppression_until = now + timedelta(hours=cooldown_hours)
        session.flush()
        return candidate, record, None

    budget_snapshot = {
        **budget_snapshot,
        "reservation": reserved_snapshot["reservation"],
    }
    record.budget_snapshot = budget_snapshot
    candidate.status = CANDIDATE_STATE_APPROVED
    approval = _record_approval(
        session,
        candidate,
        actor_id=actor_id,
        actor_role=actor_role,
        approval_kind="manual",
        reason=reason,
        expires_at=expires_at,
    )

    handoff_run = materialize_scheduler_handoff(session, candidate, record, now=now)
    if handoff_run is None:
        session.flush()
        return candidate, record, approval

    auto_exec_snapshot = reserve_auto_exec_budget(
        session,
        candidate,
        actor_type=actor_type,
        actor_id=actor_id,
        reason_code="manual_approval_auto_exec_slot",
        decision_id=record.id,
        now=now,
    )
    budget_snapshot = {
        **budget_snapshot,
        "reservation": _merge_reservations(reserved_snapshot, auto_exec_snapshot),
    }
    if auto_exec_snapshot.get("reservation_denied"):
        budget_snapshot["auto_exec_reservation_denied"] = auto_exec_snapshot.get(
            "reservation_reason_code"
        )
        record.budget_snapshot = budget_snapshot
        session.flush()
        return candidate, record, approval

    executed_run = execute_scheduler_run(
        session,
        handoff_run.id,
        owner_id=actor_id or "agency",
        now=now,
    )
    if executed_run is not None:
        record.scheduler_run_id = executed_run.id
        record.run_id = getattr(executed_run, "agent_run_id", None)
        budget_snapshot = {
            **budget_snapshot,
            "scheduler_run": _scheduler_run_snapshot(executed_run),
        }
        candidate.status = _candidate_status_from_scheduler_run(executed_run.status) or candidate.status
    record.budget_snapshot = budget_snapshot
    session.flush()
    return candidate, record, approval


def reject_candidate_review(
    session,
    candidate_id: int,
    *,
    actor_id: str,
    actor_role: str,
    reason: str,
    actor_type: str = "human",
    now: datetime | None = None,
) -> tuple[AgencyCandidate, AgencyDecision]:
    """Reject a candidate with an auditable decision record."""
    now = now or datetime.now(timezone.utc)
    reason = reason.strip()
    if not reason:
        raise ValueError("Rejection reason is required")
    candidate = session.get(AgencyCandidate, candidate_id)
    if candidate is None:
        raise LookupError(f"AgencyCandidate {candidate_id} not found")
    policy_snapshot, budget_snapshot = _manual_review_snapshots(session, candidate, now=now)
    candidate.status = CANDIDATE_STATE_REJECTED
    decision = AgencyDecision(
        candidate_id=candidate.id,
        decision="reject",
        actor_type=actor_type,
        actor_id=actor_id,
        reason_code="manual_rejection",
        reason_text=reason,
        policy_snapshot=policy_snapshot,
        budget_snapshot=budget_snapshot,
    )
    session.add(decision)
    session.flush()
    return candidate, decision


def suppress_candidate_review(
    session,
    candidate_id: int,
    *,
    actor_id: str,
    actor_role: str,
    reason: str,
    suppress_until: datetime | None = None,
    actor_type: str = "human",
    now: datetime | None = None,
) -> tuple[AgencyCandidate, AgencyDecision]:
    """Suppress a candidate until an explicit timestamp or budget cooldown."""
    now = now or datetime.now(timezone.utc)
    reason = reason.strip()
    if not reason:
        raise ValueError("Suppression reason is required")
    candidate = session.get(AgencyCandidate, candidate_id)
    if candidate is None:
        raise LookupError(f"AgencyCandidate {candidate_id} not found")
    policy_snapshot, budget_snapshot = _manual_review_snapshots(session, candidate, now=now)
    if suppress_until is None:
        cooldown_hours = int(budget_snapshot.get("cooldown_hours") or 24)
        suppress_until = now + timedelta(hours=cooldown_hours)
    if suppress_until <= now:
        raise ValueError("Suppression expiry must be in the future")

    candidate.status = CANDIDATE_STATE_SUPPRESSED
    candidate.suppression_until = suppress_until
    decision = AgencyDecision(
        candidate_id=candidate.id,
        decision="suppress",
        actor_type=actor_type,
        actor_id=actor_id,
        reason_code="manual_suppression",
        reason_text=f"{reason} (role={actor_role})",
        policy_snapshot=policy_snapshot,
        budget_snapshot=budget_snapshot,
    )
    session.add(decision)
    session.flush()
    return candidate, decision


def evaluate_and_record_candidate(
    *,
    drive_type: str,
    source_type: str,
    source_refs: Iterable[dict[str, Any] | str],
    proposal_kind: str,
    proposed_run_payload: dict[str, Any],
    org_id: str | None = None,
    user_id: str | None = None,
    target_binding_id: str | None = None,
    risk_class: str = "low",
    reversibility_class: str = "read_only",
    expected_value: float = 0.0,
    novelty_score: float = 0.0,
    urgency_score: float = 0.0,
    estimated_cost: float = 0.0,
    estimated_tokens: int = 0,
    status: str = CANDIDATE_STATE_PROPOSED,
    source_metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[AgencyCandidate, AgencyDecision]:
    """Persist a candidate, evaluate it, and record the review decision."""
    now = now or datetime.now(timezone.utc)
    with UnitOfWork() as uow:
        spec = _as_spec(
            drive_type=drive_type,
            source_type=source_type,
            source_refs=source_refs,
            proposal_kind=proposal_kind,
            proposed_run_payload={
                **proposed_run_payload,
                **({"source_metadata": source_metadata} if source_metadata else {}),
            },
            org_id=org_id,
            user_id=user_id,
            target_binding_id=target_binding_id,
            risk_class=risk_class,
            reversibility_class=reversibility_class,
            expected_value=expected_value,
            novelty_score=novelty_score,
            urgency_score=urgency_score,
            estimated_cost=estimated_cost,
            estimated_tokens=estimated_tokens,
            status=status,
        )
        candidate = _persist_candidate(uow.session, spec)
        decision = evaluate_candidate(uow.session, candidate, now=now)
        budget_snapshot = decision["budget_snapshot"]
        record = AgencyDecision(
            candidate_id=candidate.id,
            decision=decision["decision"],
            actor_type=decision["actor_type"],
            actor_id=decision.get("actor_id"),
            reason_code=decision["reason_code"],
            reason_text=decision["reason_text"],
            policy_snapshot=decision["policy_snapshot"],
            budget_snapshot=budget_snapshot,
            scheduler_run_id=decision.get("scheduler_run_id"),
            run_id=decision.get("run_id"),
        )
        uow.session.add(record)
        uow.session.flush()

        candidate.status = decision["candidate_status"]
        reserved_snapshot: dict[str, Any] | None = None
        auto_exec_snapshot: dict[str, Any] | None = None
        if decision["decision"] != "block":
            reserved_snapshot = reserve_candidate_budget(
                uow.session,
                candidate,
                reason_code=f"{decision['decision']}_candidate_slot",
                decision_id=record.id,
                now=now,
            )
            budget_snapshot = {
                **budget_snapshot,
                "reservation": reserved_snapshot["reservation"],
            }
            if reserved_snapshot.get("reservation_denied"):
                record.decision = "block"
                record.reason_code = reserved_snapshot.get(
                    "reservation_reason_code",
                    "candidate_budget_exhausted",
                )
                record.reason_text = "Candidate budget was exhausted before reservation."
                candidate.status = CANDIDATE_STATE_SUPPRESSED
                record.budget_snapshot = reserved_snapshot
                if not candidate.suppression_until:
                    cooldown_hours = int(reserved_snapshot.get("cooldown_hours") or 24)
                    candidate.suppression_until = now + timedelta(hours=cooldown_hours)
                uow.session.flush()
                return candidate, record
            record.budget_snapshot = budget_snapshot

        if decision["decision"] == "approve":
            _record_approval(
                uow.session,
                candidate,
                actor_id=decision.get("actor_id") or "agency",
                actor_role="system",
                approval_kind="auto_policy",
                reason=decision["reason_text"],
                expires_at=candidate.expires_at,
            )
            handoff_run = materialize_scheduler_handoff(
                uow.session,
                candidate,
                record,
                now=now,
            )
            if handoff_run is not None:
                auto_exec_snapshot = reserve_auto_exec_budget(
                    uow.session,
                    candidate,
                    reason_code="auto_exec_candidate_slot",
                    decision_id=record.id,
                    now=now,
                )
                budget_snapshot = {
                    **budget_snapshot,
                    "reservation": _merge_reservations(
                        reserved_snapshot or {},
                        auto_exec_snapshot,
                    ),
                }
                if auto_exec_snapshot.get("reservation_denied"):
                    candidate.status = CANDIDATE_STATE_APPROVED
                    budget_snapshot["auto_exec_reservation_denied"] = auto_exec_snapshot.get(
                        "reservation_reason_code"
                    )
                    record.budget_snapshot = budget_snapshot
                else:
                    executed_run = execute_scheduler_run(
                        uow.session,
                        handoff_run.id,
                        owner_id="agency",
                        now=now,
                    )
                    if executed_run is not None:
                        record.scheduler_run_id = executed_run.id
                        record.run_id = getattr(executed_run, "agent_run_id", None)
                        budget_snapshot = {
                            **budget_snapshot,
                            "scheduler_run": _scheduler_run_snapshot(executed_run),
                        }
                        record.budget_snapshot = budget_snapshot
                        candidate.status = _candidate_status_from_scheduler_run(executed_run.status) or candidate.status
        elif decision["decision"] == "block":
            candidate.status = decision["candidate_status"]
        elif decision["decision"] == "recommend":
            candidate.status = CANDIDATE_STATE_PROPOSED
        if candidate.status == CANDIDATE_STATE_SUPPRESSED and not candidate.suppression_until:
            cooldown_hours = int(budget_snapshot.get("cooldown_hours") or 24)
            candidate.suppression_until = now + timedelta(hours=cooldown_hours)
        uow.session.flush()
        return candidate, record


def _evidence_from_context(context: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for run in (context.get("agent_runses") or [])[:5]:
        refs.append({
            "kind": "agent_runs",
            "source_table": "agent_runs",
            "id": run.get("id"),
            "idea_id": run.get("idea_id"),
            "status": run.get("status"),
            "skill_used": run.get("skill_used"),
            "target_status": run.get("target_status"),
            "error_classification": run.get("error_classification"),
            "postmortem": run.get("postmortem"),
            "tokens_total": run.get("tokens_total"),
            "estimated_cost": run.get("estimated_cost"),
        })
    for execution in (context.get("skill_executions") or [])[:5]:
        refs.append({
            "kind": "skill_execution",
            "source_table": "skill_executions",
            "id": execution.get("id"),
            "skill_id": execution.get("skill_id"),
            "skill_name": execution.get("skill_name"),
            "outcome": execution.get("outcome"),
            "duration_sec": execution.get("duration_sec"),
            "started_at": execution.get("started_at"),
            "task_description": execution.get("task_description"),
        })
    for memory in (context.get("new_memories") or [])[:5]:
        refs.append({
            "kind": "memory",
            "source_table": "memories",
            "id": memory.get("id"),
            "memory_type": memory.get("memory_type"),
            "source": memory.get("source"),
            "salience": memory.get("salience"),
            "emotion_label": memory.get("emotion_label"),
        })
    for retrieval in (context.get("retrievals") or [])[:5]:
        refs.append({
            "kind": "retrieval_log",
            "source_table": "retrieval_log",
            "query_text": retrieval.get("query_text"),
            "results_returned": retrieval.get("results_returned"),
            "top_score": retrieval.get("top_score"),
            "was_relevant": retrieval.get("was_relevant"),
            "feedback": retrieval.get("feedback"),
        })
    for task in (context.get("tasks") or [])[:5]:
        refs.append({
            "kind": "task",
            "source_table": "tasks",
            "description": task.get("description"),
            "task_type": task.get("task_type"),
            "outcome": task.get("outcome"),
            "strategy_chosen": task.get("strategy_chosen"),
            "guardrails": task.get("guardrails"),
        })
    for metric in (context.get("previous_metrics") or [])[:3]:
        refs.append({
            "kind": "daily_metric",
            "source_table": "daily_metrics",
            "metric_date": metric.get("metric_date"),
            "avg_valence": metric.get("avg_valence"),
            "valence_trend": metric.get("valence_trend"),
            "frustration_count": metric.get("frustration_count"),
            "joy_count": metric.get("joy_count"),
        })
    return refs


def _drive_for_kind(kind: str, *, default: str = "curiosity") -> str:
    mapping = {
        "skill_refinement": "competence",
        "new_skill": "competence",
        "guardian_rule": "prevention",
        "postmortem_lesson": "prevention",
        "implement_proposal": "integrity",
        "curiosity_followup": "curiosity",
        "learning_signal": "integrity",
    }
    return mapping.get(kind, default)


def mirror_reflection_result(
    *,
    reflection: dict[str, Any],
    context: dict[str, Any],
    target_date: datetime | None = None,
    org_id: str | None = None,
    user_id: str | None = None,
) -> list[tuple[AgencyCandidate, AgencyDecision]]:
    """Mirror nightly reflection outputs into agency candidates."""
    evidence_refs = _evidence_from_context(context)
    evidence_refs.append({
        "kind": "reflection_output",
        "date": (target_date or datetime.now(timezone.utc)).date().isoformat(),
    })
    results: list[tuple[AgencyCandidate, AgencyDecision]] = []

    for ref in reflection.get("skill_refinements", []):
        results.append(
            evaluate_and_record_candidate(
                drive_type=_drive_for_kind("skill_refinement"),
                source_type="nightly_reflection",
                source_refs=evidence_refs,
                proposal_kind="skill_refinement",
                proposed_run_payload={
                    "skill_name": ref.get("skill_name"),
                    "change_type": ref.get("change_type"),
                    "change": ref.get("change"),
                    "reason": ref.get("reason"),
                    "new_procedure": ref.get("new_procedure"),
                },
                org_id=org_id,
                user_id=user_id,
                risk_class="medium",
                reversibility_class="repo_local" if ref.get("new_procedure") else "read_only",
                expected_value=0.7,
                novelty_score=0.3,
                urgency_score=0.6,
                estimated_tokens=250,
                status=CANDIDATE_STATE_PROPOSED,
                source_metadata={"origin": "nightly_reflection"},
            )
        )

    for prop in reflection.get("new_skills_proposed", []):
        results.append(
            evaluate_and_record_candidate(
                drive_type=_drive_for_kind("new_skill"),
                source_type="nightly_reflection",
                source_refs=evidence_refs,
                proposal_kind="new_skill",
                proposed_run_payload=prop,
                org_id=org_id,
                user_id=user_id,
                risk_class="medium",
                reversibility_class="stateful",
                expected_value=0.5,
                novelty_score=0.9,
                urgency_score=0.4,
                estimated_tokens=350,
                status=CANDIDATE_STATE_PROPOSED,
                source_metadata={"origin": "nightly_reflection"},
            )
        )

    for prop in reflection.get("system_proposals", []):
        area = (prop.get("area") or "other").lower()
        drive = "integrity" if area in {"memory", "retrieval", "consolidation"} else "prevention"
        results.append(
            evaluate_and_record_candidate(
                drive_type=drive,
                source_type="nightly_reflection",
                source_refs=evidence_refs,
                proposal_kind="system_proposal",
                proposed_run_payload=prop,
                org_id=org_id,
                user_id=user_id,
                risk_class="low" if prop.get("priority") == "low" else "medium",
                reversibility_class="read_only",
                expected_value=0.6,
                novelty_score=0.2,
                urgency_score=0.5 if prop.get("priority") == "high" else 0.3,
                estimated_tokens=180,
                status=CANDIDATE_STATE_PROPOSED,
                source_metadata={"origin": "nightly_reflection", "area": area},
            )
        )

    return results


def mirror_guardian_signals(
    *,
    target_date: str,
    recurring_patterns: list[dict[str, Any]],
    violations: list[dict[str, Any]],
    org_id: str | None = None,
    user_id: str | None = None,
) -> list[tuple[AgencyCandidate, AgencyDecision]]:
    """Mirror guardian violation patterns into agency candidates."""
    evidence_refs = [
        {"kind": "violation_log", "date": target_date, "context": row.get("context"), "detected_by": row.get("detected_by")}
        for row in violations[:5]
    ]
    results: list[tuple[AgencyCandidate, AgencyDecision]] = []
    for pattern in recurring_patterns:
        results.append(
            evaluate_and_record_candidate(
                drive_type="prevention",
                source_type="nightly_guardian",
                source_refs=evidence_refs + [{
                    "kind": "guardian_pattern",
                    "context": pattern.get("context"),
                    "count": pattern.get("cnt"),
                }],
                proposal_kind="guardian_rule",
                proposed_run_payload={
                    "context": pattern.get("context"),
                    "count": pattern.get("cnt"),
                    "source_date": target_date,
                },
                org_id=org_id,
                user_id=user_id,
                risk_class="medium",
                reversibility_class="read_only",
                expected_value=0.8,
                novelty_score=0.2,
                urgency_score=0.7,
                estimated_tokens=120,
                status=CANDIDATE_STATE_PROPOSED,
                source_metadata={"origin": "nightly_guardian"},
            )
        )
    return results


def mirror_implement_proposal(
    *,
    proposal: dict[str, Any],
    source_refs: list[dict[str, Any] | str],
    org_id: str | None = None,
    user_id: str | None = None,
) -> tuple[AgencyCandidate, AgencyDecision]:
    """Mirror a nightly implementation proposal into agency."""
    action = (proposal.get("action") or "log_only").lower()
    drive = "integrity" if action in {"replace", "append"} else "prevention"
    risk = "medium" if action != "log_only" else "low"
    return evaluate_and_record_candidate(
        drive_type=drive,
        source_type="nightly_implement",
        source_refs=source_refs,
        proposal_kind="implement_proposal",
        proposed_run_payload=proposal,
        org_id=org_id,
        user_id=user_id,
        target_binding_id=proposal.get("target_file"),
        risk_class=risk,
        reversibility_class="repo_local" if action in {"append", "replace"} else "read_only",
        expected_value=0.4,
        novelty_score=0.1,
        urgency_score=0.5,
        estimated_tokens=100,
        status=CANDIDATE_STATE_PROPOSED,
        source_metadata={"origin": "nightly_implement"},
    )


def mirror_curiosity_reading(
    *,
    reading: dict[str, Any],
    source_refs: list[dict[str, Any] | str],
    org_id: str | None = None,
    user_id: str | None = None,
) -> tuple[AgencyCandidate, AgencyDecision] | None:
    """Mirror a curiosity reading into a recommendation candidate."""
    if reading.get("nothing_recent"):
        return None
    application = reading.get("concrete_application")
    if not application or application == "none":
        return None
    return evaluate_and_record_candidate(
        drive_type="curiosity",
        source_type="curiosity_reading",
        source_refs=source_refs,
        proposal_kind="curiosity_followup",
        proposed_run_payload={
            "item_title": reading.get("item_title"),
            "item_url": reading.get("item_url"),
            "concrete_application": application,
            "worth_deep_dive": reading.get("worth_deep_dive", False),
        },
        org_id=org_id,
        user_id=user_id,
        risk_class="low",
        reversibility_class="read_only",
        expected_value=0.3,
        novelty_score=0.8,
        urgency_score=0.2,
        estimated_tokens=80,
        status=CANDIDATE_STATE_PROPOSED,
        source_metadata={"origin": "curiosity"},
    )


def mirror_learning_signal(
    *,
    task_description: str,
    skill_name: str | None,
    run_id: int | None,
    cognitive_misses: list[str] | None,
    org_id: str | None = None,
    user_id: str | None = None,
) -> tuple[AgencyCandidate, AgencyDecision] | None:
    """Mirror AgentRun learning misses into agency candidates."""
    if not cognitive_misses:
        return None
    evidence_refs = [{
        "kind": "learning_signal",
        "source_table": "agent_runs",
        "run_id": run_id,
        "skill_name": skill_name,
        "cognitive_misses": cognitive_misses,
    }]
    if run_id is not None:
        with UnitOfWork() as uow:
            run_row = summarize_run_usage(uow.session, run_id)
        if run_row:
            evidence_refs.append({
                "kind": "agent_runs",
                "source_table": "agent_runs",
                "id": run_row.get("id"),
                "thread_id": run_row.get("thread_id"),
                "status": run_row.get("status"),
                "skill_used": run_row.get("skill_used"),
                "tokens_total": run_row.get("tokens_total"),
                "estimated_cost": run_row.get("estimated_cost"),
                "created_at": run_row.get("created_at"),
            })
    return evaluate_and_record_candidate(
        drive_type="integrity",
        source_type="agent_run_learning",
        source_refs=evidence_refs,
        proposal_kind="learning_signal",
        proposed_run_payload={
            "task_description": task_description[:500],
            "skill_name": skill_name,
            "run_id": run_id,
            "cognitive_misses": cognitive_misses,
        },
        org_id=org_id,
        user_id=user_id,
        risk_class="low",
        reversibility_class="read_only",
        expected_value=0.5,
        novelty_score=0.4,
        urgency_score=0.5,
        estimated_tokens=60,
        status=CANDIDATE_STATE_PROPOSED,
        source_metadata={"origin": "agent_run_learning"},
    )
