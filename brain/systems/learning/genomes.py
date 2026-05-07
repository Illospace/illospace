"""Derive compact run genomes from persisted execution facts."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.agent import AgentApiCall
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.learning import RunGenome
from brain.platform.db.models.system import RetrievalDecision
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)

NEGATIVE_IMPLICIT_FEEDBACK_TAGS = {
    "memory_failure",
    "shallow_reasoning",
    "wrong_autonomy",
    "dead_code",
    "action_paralysis",
}


@dataclass(frozen=True)
class RunLearningGate:
    """Evidence gate for deciding whether a run can produce durable learning."""

    user_id: str
    org_id: str | None
    visibility: str
    evidence_status: str
    learning_outcome: str
    positive_learning_allowed: bool
    negative_example: bool
    confidence_ceiling: float
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "org_id": self.org_id,
            "visibility": self.visibility,
            "evidence_status": self.evidence_status,
            "learning_outcome": self.learning_outcome,
            "positive_learning_allowed": self.positive_learning_allowed,
            "negative_example": self.negative_example,
            "confidence_ceiling": self.confidence_ceiling,
            "evidence": self.evidence,
        }


def _bucket(value: float | int | None, bounds: list[tuple[float, str]], default: str) -> str:
    if value is None:
        return default
    for limit, label in bounds:
        if value <= limit:
            return label
    return bounds[-1][1] if bounds else default


def _bucket_tokens(token_total: int | None) -> str:
    return _bucket(
        token_total,
        [(5_000, "tiny"), (15_000, "small"), (40_000, "medium"), (80_000, "large")],
        "unknown",
    )


def _bucket_latency(latency_sec: float | None) -> str:
    return _bucket(
        latency_sec,
        [(30, "short"), (120, "medium"), (300, "long"), (900, "very_long")],
        "unknown",
    )


def _stable_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash_payload(payload: dict) -> str:
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _normalize_list(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, tuple):
                return list(parsed)
        except Exception:
            pass
    return [value]


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(_clean_text(row.get("status")) or "unknown" for row in rows))


def _payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _run_facts(row: AgentRunRow) -> dict[str, Any]:
    metadata = _payload(row.metadata_)
    target_ref = _payload(row.target_ref)
    workspace_ref = _payload(row.workspace_ref)
    model_policy = _payload(row.model_policy)
    context = _payload(metadata.get("context"))
    verification = _payload(metadata.get("verification"))
    feedback = _payload(metadata.get("feedback"))
    return {
        "id": row.id,
        "idea_id": row.thread_id,
        "event": target_ref.get("event"),
        "message": row.input_message,
        "skill_used": metadata.get("skill_used") or target_ref.get("skill") or context.get("skill"),
        "model_used": metadata.get("model_used") or model_policy.get("model"),
        "thinking_used": metadata.get("thinking_used") or model_policy.get("thinking"),
        "brain_context_loaded": bool(row.context_summary or context.get("loaded") or workspace_ref),
        "preloaded_memory_count": int(context.get("preloaded_memory_count") or 0),
        "brain_recall_used": bool(context.get("brain_recall_used")),
        "brain_skills_used": bool(context.get("brain_skills_used") or metadata.get("skill_used")),
        "attention_required": bool(metadata.get("attention_required")),
        "cognitive_misses": _normalize_list(metadata.get("cognitive_misses")),
        "adaptations": _normalize_list(metadata.get("adaptations")),
        "contract_status": verification.get("status") or metadata.get("contract_status") or target_ref.get("contract_status"),
        "contract_type": verification.get("contract_type") or metadata.get("contract_type") or target_ref.get("contract_type") or row.recipe,
        "target_status": metadata.get("target_status") or target_ref.get("status") or row.status,
        "verification_attempts": int(verification.get("attempts") or metadata.get("verification_attempts") or 0),
        "verification_last_error": verification.get("last_error") or metadata.get("verification_last_error"),
        "verification_warnings": _normalize_list(verification.get("warnings") or metadata.get("verification_warnings")),
        "status": row.status,
        "consumer_type": row.recipe,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "tokens_total": metadata.get("tokens_total"),
        "user_id": row.user_id,
        "user_org_id": row.org_id,
        "skill_feedback": feedback.get("skill") or metadata.get("skill_feedback"),
        "implicit_feedback_tags": _normalize_list(feedback.get("implicit_tags") or metadata.get("implicit_feedback_tags")),
    }


def _tool_rows_from_events(events: list[AgentRunEventRow]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        payload = _payload(event.payload)
        rows.append({"tool_name": payload.get("tool_name") or payload.get("tool") or "unknown"})
    return rows


def _verification_rows_from_session(session, run_id: int) -> list[dict[str, Any]]:
    event_rows = session.scalars(
        select(AgentRunEventRow)
        .where(
            AgentRunEventRow.run_id == run_id,
            AgentRunEventRow.event_type.in_(
                [
                    "run.verification_started",
                    "run.verification_warning",
                    "run.verification_completed",
                    "run.verification_result",
                    "run.gate_completed",
                    "run.verifier_completed",
                ]
            ),
        )
        .order_by(AgentRunEventRow.sequence_no.asc(), AgentRunEventRow.id.asc())
    ).all()
    rows: list[dict[str, Any]] = []
    for event in event_rows:
        payload = _payload(event.payload)
        rows.append(
            {
                "verifier_type": payload.get("verifier_type") or payload.get("gate") or payload.get("type") or event.event_type,
                "status": payload.get("status") or payload.get("result") or "unknown",
                "severity": payload.get("severity") or ("required" if payload.get("required") else "advisory"),
                "failure_reason": payload.get("failure_reason") or payload.get("reason") or payload.get("error"),
            }
        )
    artifact_rows = session.scalars(
        select(AgentRunArtifactRow)
        .where(
            AgentRunArtifactRow.run_id == run_id,
            AgentRunArtifactRow.artifact_type.in_(["verifier_evidence", "verification_evidence"]),
        )
        .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
    ).all()
    for artifact in artifact_rows:
        payload = _payload(artifact.payload)
        rows.append(
            {
                "verifier_type": payload.get("verifier_type") or payload.get("gate") or artifact.artifact_type,
                "status": payload.get("status") or "observed",
                "severity": payload.get("severity") or "advisory",
                "failure_reason": payload.get("failure_reason") or payload.get("reason"),
            }
        )
    return rows


def _strategy_row_from_events(events: list[AgentRunEventRow]) -> dict[str, Any] | None:
    for event in reversed(events):
        payload = _payload(event.payload)
        if event.event_type == "run.learning.strategy_observed":
            return {
                "strategy": payload.get("strategy"),
                "success": payload.get("success"),
                "tokens_used": payload.get("tokens_used"),
                "duration_sec": payload.get("duration_sec"),
            }
    return None


def _prediction_rows_from_artifacts(artifacts: list[AgentRunArtifactRow]) -> list[dict[str, Any]]:
    return [
        _payload(artifact.payload)
        for artifact in artifacts
        if artifact.artifact_type == "prediction"
    ]


def _build_learning_gate(
    run: dict[str, Any],
    verifier_rows: list[dict[str, Any]],
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> RunLearningGate | None:
    resolved_user_id = _clean_text(user_id) or _clean_text(run.get("user_id"))
    resolved_org_id = _clean_text(org_id) or _clean_text(run.get("user_org_id"))
    if not resolved_user_id:
        return None

    visibility = "org" if resolved_org_id else "private"
    run_status = (_clean_text(run.get("status")) or "").lower()
    contract_status = (_clean_text(run.get("contract_status")) or "").lower()
    skill_feedback = (_clean_text(run.get("skill_feedback")) or "").lower()
    implicit_feedback_tags = {
        str(tag).strip()
        for tag in _normalize_list(run.get("implicit_feedback_tags"))
        if str(tag).strip()
    }

    normalized_verifiers = [dict(row) for row in verifier_rows]
    required_verifiers = [
        row for row in normalized_verifiers
        if (_clean_text(row.get("severity")) or "").lower() == "required"
    ]
    required_failures = [
        row for row in required_verifiers
        if (_clean_text(row.get("status")) or "").lower() == "failed"
    ]
    required_warnings = [
        row for row in required_verifiers
        if (_clean_text(row.get("status")) or "").lower() == "warning"
    ]
    positive_statuses = {"passed", "pass", "satisfied", "success", "completed"}
    negative_statuses = {"failed", "fail", "blocked", "error"}
    required_passed = bool(required_verifiers) and not required_failures and not required_warnings and all(
        (_clean_text(row.get("status")) or "").lower() in positive_statuses
        for row in required_verifiers
    )
    any_verified_failure = any(
        (_clean_text(row.get("status")) or "").lower() in negative_statuses
        for row in normalized_verifiers
    )
    all_available_checks_passed = bool(normalized_verifiers) and not any_verified_failure and all(
        (_clean_text(row.get("status")) or "").lower() in positive_statuses
        for row in normalized_verifiers
    )
    human_positive = skill_feedback == "good"
    human_negative = skill_feedback == "bad" or bool(implicit_feedback_tags & NEGATIVE_IMPLICIT_FEEDBACK_TAGS)
    run_completed = run_status == "completed"
    verifier_passed = run_completed and contract_status not in negative_statuses and (
        required_passed or all_available_checks_passed
    )
    positive_learning_allowed = bool((human_positive or verifier_passed) and not human_negative)

    if human_positive and not human_negative:
        evidence_status = "human_positive"
        learning_outcome = "positive"
        confidence_ceiling = 0.95
    elif verifier_passed:
        evidence_status = "verifier_passed"
        learning_outcome = "positive"
        confidence_ceiling = 0.9
    elif human_negative:
        evidence_status = "human_negative"
        learning_outcome = "negative"
        confidence_ceiling = 0.4
    elif required_failures or any_verified_failure or contract_status in negative_statuses:
        evidence_status = "verifier_failed"
        learning_outcome = "negative"
        confidence_ceiling = 0.35
    elif not run_completed:
        evidence_status = "run_failed"
        learning_outcome = "negative"
        confidence_ceiling = 0.35
    elif contract_status != "satisfied" or not required_verifiers:
        evidence_status = "unverified_success"
        learning_outcome = "unverified"
        confidence_ceiling = 0.3
    else:
        evidence_status = "unverified"
        learning_outcome = "unverified"
        confidence_ceiling = 0.3

    negative_example = learning_outcome in {"negative", "unverified"}
    evidence = {
        "run_status": run_status,
        "contract_status": contract_status,
        "skill_feedback": skill_feedback or None,
        "implicit_feedback_tags": sorted(implicit_feedback_tags),
        "verification_count": len(normalized_verifiers),
        "required_verifier_count": len(required_verifiers),
        "required_verifier_failures": [
            {
                "verifier_type": row.get("verifier_type"),
                "failure_reason": row.get("failure_reason"),
            }
            for row in required_failures[:10]
        ],
        "required_verifier_warnings": [
            {
                "verifier_type": row.get("verifier_type"),
                "failure_reason": row.get("failure_reason"),
            }
            for row in required_warnings[:10]
        ],
        "verification_status_counts": _status_counts(normalized_verifiers),
        "human_positive": human_positive,
        "human_negative": human_negative,
        "verifier_passed": verifier_passed,
    }
    return RunLearningGate(
        user_id=resolved_user_id,
        org_id=resolved_org_id,
        visibility=visibility,
        evidence_status=evidence_status,
        learning_outcome=learning_outcome,
        positive_learning_allowed=positive_learning_allowed,
        negative_example=negative_example,
        confidence_ceiling=confidence_ceiling,
        evidence=evidence,
    )


def evaluate_run_learning_gate(
    run_id: int,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the scoped evidence gate for a run without persisting anything."""
    try:
        with UnitOfWork() as uow:
            run_row = uow.session.get(AgentRunRow, int(run_id))
            if not run_row:
                return None
            run = _run_facts(run_row)
            verifier_rows = _verification_rows_from_session(uow.session, int(run_id))
    except Exception as exc:
        logger.debug("Learning gate evaluation failed for run %s: %s", run_id, exc)
        return None

    gate = _build_learning_gate(dict(run), [dict(row) for row in verifier_rows], user_id=user_id, org_id=org_id)
    return gate.as_dict() if gate else None


def _coerce_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def derive_run_genome(
    run_id: int,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict | None:
    """Derive a compact genome from persisted facts only."""
    try:
        with UnitOfWork() as uow:
            run_row = uow.session.get(AgentRunRow, int(run_id))
            if not run_row:
                return None
            run = _run_facts(run_row)

            api_models = uow.session.scalars(
                select(AgentApiCall).where(AgentApiCall.run_id == int(run_id))
            ).all()
            api_rows = [
                {
                    "model": row.model,
                    "tokens_input": row.tokens_input,
                    "tokens_output": row.tokens_output,
                    "cache_read": row.cache_read,
                    "cache_write": row.cache_write,
                    "latency_ms": row.latency_ms,
                }
                for row in api_models
            ]

            run_events = uow.session.scalars(
                select(AgentRunEventRow)
                .where(AgentRunEventRow.run_id == int(run_id))
                .order_by(AgentRunEventRow.sequence_no.asc(), AgentRunEventRow.id.asc())
            ).all()
            tool_rows = _tool_rows_from_events(
                [
                    event
                    for event in run_events
                    if event.event_type in {"run.tool_completed", "run.tool_failed", "run.tool_started"}
                ]
            )
            strategy_row = _strategy_row_from_events(list(run_events))

            run_artifacts = uow.session.scalars(
                select(AgentRunArtifactRow)
                .where(AgentRunArtifactRow.run_id == int(run_id))
                .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
            ).all()
            prediction_rows = _prediction_rows_from_artifacts(list(run_artifacts))

            retrieval_models = uow.session.scalars(
                select(RetrievalDecision).where(RetrievalDecision.run_id == int(run_id))
            ).all()
            retrieval_rows = [
                {
                    "mode": row.mode,
                    "stage": row.stage,
                    "selected_item_ids": row.selected_item_ids,
                    "suppressed_item_ids": row.suppressed_item_ids,
                    "omission_risk_score": row.omission_risk_score,
                    "contradiction_risk_score": row.contradiction_risk_score,
                    "candidate_count": row.candidate_count,
                    "preload_budget_tokens": row.preload_budget_tokens,
                    "lazy_budget_tokens": row.lazy_budget_tokens,
                    "policy_version": row.policy_version,
                }
                for row in retrieval_models
            ]

            verifier_rows = _verification_rows_from_session(uow.session, int(run_id))
    except Exception as exc:
        logger.debug("Genome derivation failed for run %s: %s", run_id, exc)
        return None

    gate = _build_learning_gate(
        dict(run),
        [dict(row) for row in verifier_rows],
        user_id=user_id,
        org_id=org_id,
    )
    if not gate:
        logger.debug("Genome derivation skipped for run %s: missing learning owner context", run_id)
        return None

    api_models = Counter((row["model"] or "unknown") for row in api_rows)
    tool_counts = Counter((row["tool_name"] or "unknown") for row in tool_rows)
    tool_names = sorted(tool_counts)
    selected_item_count = sum(len(_normalize_list(row["selected_item_ids"])) for row in retrieval_rows)
    suppressed_item_count = sum(len(_normalize_list(row["suppressed_item_ids"])) for row in retrieval_rows)

    cognitive_misses = _normalize_list(run["cognitive_misses"])
    adaptations = _normalize_list(run["adaptations"])
    verification_warnings = _normalize_list(run["verification_warnings"])
    started_at = _coerce_datetime(run["started_at"])
    completed_at = _coerce_datetime(run["completed_at"])

    total_tokens = run["tokens_total"]
    if total_tokens is None and api_rows:
        total_tokens = sum(
            (row["tokens_input"] or 0) + (row["tokens_output"] or 0)
            for row in api_rows
        )

    duration_sec = None
    if completed_at and started_at:
        duration_sec = max(0.0, (completed_at - started_at).total_seconds())
    elif strategy_row and strategy_row.get("duration_sec") is not None:
        duration_sec = float(strategy_row["duration_sec"] or 0)

    task_family = run["skill_used"] or run["contract_type"] or run["consumer_type"] or "general"
    target_family = run["target_status"] or run["consumer_type"] or "unspecified"
    success = run["status"] == "completed"
    rework_required = bool(run["attention_required"] or cognitive_misses or (strategy_row and not strategy_row.get("success", True)))

    context_profile = {
        "brain_context_loaded": bool(run["brain_context_loaded"]),
        "brain_recall_used": bool(run["brain_recall_used"]),
        "brain_skills_used": bool(run["brain_skills_used"]),
        "preloaded_memory_count": int(run["preloaded_memory_count"] or 0),
        "attention_required": bool(run["attention_required"]),
        "cognitive_miss_count": len(cognitive_misses),
        "adaptation_count": len(adaptations),
        "api_call_count": len(api_rows),
        "tool_call_count": len(tool_rows),
        "retrieval_decision_count": len(retrieval_rows),
        "prediction_count": len(prediction_rows),
        "token_total": total_tokens or 0,
    }

    retrieval_profile = {
        "decision_count": len(retrieval_rows),
        "mode_counts": dict(Counter((row["mode"] or "unknown") for row in retrieval_rows)),
        "stage_counts": dict(Counter((row["stage"] or "unknown") for row in retrieval_rows)),
        "selected_item_count": selected_item_count,
        "suppressed_item_count": suppressed_item_count,
        "candidate_count": sum(int(row["candidate_count"] or 0) for row in retrieval_rows),
        "preload_budget_tokens": sum(int(row["preload_budget_tokens"] or 0) for row in retrieval_rows),
        "lazy_budget_tokens": sum(int(row["lazy_budget_tokens"] or 0) for row in retrieval_rows),
        "policy_versions": sorted({row["policy_version"] for row in retrieval_rows if row["policy_version"]}),
        "omission_risk_score": max(
            [float(row["omission_risk_score"]) for row in retrieval_rows if row["omission_risk_score"] is not None] or [0.0]
        ),
        "contradiction_risk_score": max(
            [float(row["contradiction_risk_score"]) for row in retrieval_rows if row["contradiction_risk_score"] is not None] or [0.0]
        ),
    }

    verifier_outcome = {
        "contract_status": run["contract_status"],
        "contract_type": run["contract_type"],
        "verification_attempts": int(run["verification_attempts"] or 0),
        "verification_last_error": run["verification_last_error"],
        "verification_warnings": verification_warnings,
        "target_status": run["target_status"],
        "verification": gate.evidence.get("verification_status_counts", {}),
        "evidence_status": gate.evidence_status,
        "skill_outcome": None,
    }

    prediction_profile = {
        "count": len(prediction_rows),
        "avg_prediction_error": round(
            sum(float(row["prediction_error"] or 0) for row in prediction_rows) / max(1, len(prediction_rows)),
            3,
        ) if prediction_rows else 0.0,
        "resolved_quality": [
            float(row["actual_quality"])
            for row in prediction_rows
            if row.get("actual_quality") is not None
        ],
        "insight_count": sum(1 for row in prediction_rows if row.get("insight")),
    }

    model_mix = {
        "run_model": run["model_used"],
        "thinking_model": run["thinking_used"],
        "api_models": dict(api_models),
        "token_total": total_tokens or 0,
    }

    tool_mix = {
        "tool_counts": dict(tool_counts),
        "unique_tools": tool_names,
        "total_tool_calls": len(tool_rows),
    }

    genome_signature = {
        "task_family": task_family,
        "target_family": target_family,
        "strategy_name": strategy_row["strategy"] if strategy_row else None,
        "skill_name": run["skill_used"],
        "model_mix": model_mix,
        "tool_mix": tool_mix,
        "retrieval_profile": retrieval_profile,
        "verifier_outcome": verifier_outcome,
        "token_cost_bucket": _bucket_tokens(total_tokens),
        "latency_bucket": _bucket_latency(duration_sec),
        "success": success,
        "rework_required": rework_required,
    }

    genome_hash = _hash_payload(genome_signature)
    satisfaction_proxy = 1.0 if success else 0.35
    satisfaction_proxy -= min(0.25, 0.05 * context_profile["cognitive_miss_count"])
    satisfaction_proxy -= min(0.2, 0.04 * verifier_outcome["verification_attempts"])
    satisfaction_proxy -= min(0.2, 0.03 * prediction_profile["insight_count"])
    satisfaction_proxy = max(0.0, min(1.0, round(satisfaction_proxy, 3)))

    return {
        "run_id": run_id,
        "user_id": gate.user_id,
        "org_id": gate.org_id,
        "visibility": gate.visibility,
        "genome_hash": genome_hash,
        "task_family": task_family,
        "target_family": target_family,
        "context_profile": context_profile,
        "strategy_name": strategy_row["strategy"] if strategy_row else None,
        "skill_name": run["skill_used"],
        "model_mix": model_mix,
        "tool_mix": tool_mix,
        "retrieval_profile": retrieval_profile,
        "verifier_outcome": verifier_outcome,
        "prediction_profile": prediction_profile,
        "contract_type": run["contract_type"],
        "token_cost_bucket": _bucket_tokens(total_tokens),
        "latency_bucket": _bucket_latency(duration_sec),
        "success": success,
        "rework_required": rework_required,
        "satisfaction_proxy": satisfaction_proxy,
        "evidence_status": gate.evidence_status,
        "learning_outcome": gate.learning_outcome,
        "positive_learning_allowed": gate.positive_learning_allowed,
        "negative_example": gate.negative_example,
        "evidence_gate": gate.as_dict(),
        "source_facts": genome_signature,
    }


def persist_run_genome(
    run_id: int,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict | None:
    """Upsert the run genome snapshot for a run."""
    genome = derive_run_genome(run_id, user_id=user_id, org_id=org_id)
    if not genome:
        return None

    try:
        with UnitOfWork() as uow:
            existing = uow.session.scalars(
                select(RunGenome).where(RunGenome.run_id == run_id)
            ).first()
            if existing:
                target = existing
            else:
                target = RunGenome(run_id=run_id)
                uow.session.add(target)

            target.genome_hash = genome["genome_hash"]
            target.user_id = genome["user_id"]
            target.org_id = genome["org_id"]
            target.visibility = genome["visibility"]
            target.task_family = genome["task_family"]
            target.target_family = genome["target_family"]
            target.context_profile = genome["context_profile"]
            target.strategy_name = genome["strategy_name"]
            target.skill_name = genome["skill_name"]
            target.model_mix = genome["model_mix"]
            target.tool_mix = genome["tool_mix"]
            target.retrieval_profile = genome["retrieval_profile"]
            target.verifier_outcome = genome["verifier_outcome"]
            target.contract_type = genome["contract_type"]
            target.token_cost_bucket = genome["token_cost_bucket"]
            target.latency_bucket = genome["latency_bucket"]
            target.success = genome["success"]
            target.rework_required = genome["rework_required"]
            target.satisfaction_proxy = genome["satisfaction_proxy"]
            target.evidence_status = genome["evidence_status"]
            target.learning_outcome = genome["learning_outcome"]
            target.evidence_gate = genome["evidence_gate"]
            uow.session.flush()

            genome["id"] = target.id
            genome["persisted_at"] = datetime.now(timezone.utc).isoformat()
            return genome
    except Exception as exc:
        logger.debug("Genome persistence failed for run %s: %s", run_id, exc)
        return None
