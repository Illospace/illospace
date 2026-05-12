"""Scheduler-facing handoff hooks for bounded agency."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from brain.platform.db.models.agency import AgencyCandidate, AgencyDecision
from brain.platform.db.models.scheduler import SchedulerJob, SchedulerRun

_HANDOFF_ALLOWED_CLASSES = {"read_only", "practice_safe"}


def _execution_class(candidate: AgencyCandidate) -> str:
    return (candidate.reversibility_class or "read_only").lower()


def build_scheduler_handoff(
    candidate: AgencyCandidate,
    decision: AgencyDecision | dict[str, Any],
) -> dict[str, Any] | None:
    """Prepare a scheduler-facing handoff seed without executing it."""
    decision_name = decision.decision if isinstance(decision, AgencyDecision) else decision.get("decision")
    budget_snapshot = (
        decision.budget_snapshot if isinstance(decision, AgencyDecision) else decision.get("budget_snapshot", {})
    )
    policy_snapshot = (
        decision.policy_snapshot if isinstance(decision, AgencyDecision) else decision.get("policy_snapshot", {})
    )

    if decision_name != "approve":
        return None
    if not budget_snapshot.get("auto_execute_enabled", False):
        return None

    execution_class = _execution_class(candidate)
    if execution_class not in _HANDOFF_ALLOWED_CLASSES:
        return None
    if execution_class != "read_only" and not candidate.target_binding_id:
        return None
    auto_exec_flag = policy_snapshot.get("auto_exec_flag")
    runtime = policy_snapshot.get("runtime") or {}
    if auto_exec_flag and not runtime.get(auto_exec_flag, False):
        return None

    scheduler_job_key = f"agency_{candidate.drive_type}_{candidate.candidate_key[:12]}"
    return {
        "scheduler_job_key": scheduler_job_key,
        "family": scheduler_job_key,
        "handler_kind": "agency_recommendation",
        "handler_ref": "brain.systems.agency.handoff:run_candidate",
        "execution_class": execution_class,
        "target_binding_selector": {"target_binding_id": candidate.target_binding_id}
        if candidate.target_binding_id
        else {},
        "payload": {
            "candidate_id": candidate.id,
            "candidate_key": candidate.candidate_key,
            "user_id": str(candidate.user_id) if candidate.user_id else None,
            "org_id": str(candidate.org_id) if candidate.org_id else None,
            "memory_visibility": "org" if candidate.org_id else "private",
            "proposal_kind": candidate.proposal_kind,
            "source_type": candidate.source_type,
            "source_refs": candidate.source_refs,
            "proposed_run_payload": candidate.proposed_run_payload,
            "budget_snapshot": budget_snapshot,
            "policy_snapshot": policy_snapshot,
            "decision": decision_name,
            "execution_class": execution_class,
        },
    }


def _ensure_scheduler_job(
    session: Session,
    handoff: dict[str, Any],
    *,
    now: datetime | None = None,
) -> SchedulerJob:
    now = now or datetime.now(timezone.utc)
    job_key = handoff["scheduler_job_key"]
    job = session.scalar(select(SchedulerJob).where(SchedulerJob.job_key == job_key))
    if job is None:
        job = SchedulerJob(
            job_key=job_key,
            family=handoff["family"],
            program_key=f"agency_{handoff['payload']['execution_class']}",
            handler_kind=handoff["handler_kind"],
            handler_ref=handoff["handler_ref"],
            cron_expr="0 0 1 1 *",
            timezone="UTC",
            enabled=False,
            owner_mode="scheduler",
            priority=100,
            max_concurrency=1,
            timeout_seconds=60,
            retry_policy={"max_attempts": 1, "backoff_seconds": 0},
            misfire_policy="record",
            load_shed_policy={},
            default_payload=handoff["payload"],
            target_binding_selector=handoff.get("target_binding_selector") or {},
            next_run_at=now,
            pause_reason="bounded agency handoff seed",
        )
        session.add(job)
        session.flush()
        return job

    job.family = handoff["family"]
    job.program_key = f"agency_{handoff['payload']['execution_class']}"
    job.handler_kind = handoff["handler_kind"]
    job.handler_ref = handoff["handler_ref"]
    job.enabled = False
    job.owner_mode = "scheduler"
    job.default_payload = handoff["payload"]
    job.target_binding_selector = handoff.get("target_binding_selector") or {}
    job.pause_reason = "bounded agency handoff seed"
    job.next_run_at = now
    session.flush()
    return job


def materialize_scheduler_handoff(
    session: Session,
    candidate: AgencyCandidate,
    decision: AgencyDecision | dict[str, Any],
    *,
    now: datetime | None = None,
) -> SchedulerRun | None:
    """Persist a scheduler-backed handoff record for a safe candidate."""
    handoff = build_scheduler_handoff(candidate, decision)
    if handoff is None:
        return None

    now = now or datetime.now(timezone.utc)
    job = _ensure_scheduler_job(session, handoff, now=now)
    decision_id = decision.id if isinstance(decision, AgencyDecision) else decision.get("id")
    idempotency_key = f"{job.job_key}:{candidate.id}:{decision_id or candidate.candidate_key}"
    existing = session.scalar(select(SchedulerRun).where(SchedulerRun.idempotency_key == idempotency_key))
    if existing is not None:
        return existing

    run = SchedulerRun(
        job_id=job.id,
        scheduled_for=now,
        window_start=now,
        window_end=now,
        status="recorded",
        attempt=1,
        idempotency_key=idempotency_key,
        payload=handoff["payload"],
    )
    session.add(run)
    session.flush()
    return run


def run_candidate(payload: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Validate a scheduler handoff payload without broadening the execution surface."""
    now = now or datetime.now(timezone.utc)
    if payload.get("decision") != "approve":
        return {"status": "skipped", "reason": "decision_not_approved", "at": now.isoformat()}
    execution_class = (payload.get("execution_class") or "read_only").lower()
    if execution_class not in _HANDOFF_ALLOWED_CLASSES:
        return {"status": "blocked", "reason": "unsupported_execution_class", "at": now.isoformat()}
    return {
        "status": "recorded",
        "reason": "scheduler_handoff_only",
        "execution_class": execution_class,
        "at": now.isoformat(),
    }
