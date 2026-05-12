"""Deterministic after-run learning queue.

The queue records local-only learning shells after a run has finished.
It intentionally avoids LLM calls, remote export, and broad scans so run
completion can hand off useful evidence without changing the user response path.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import logging
from typing import Any

from brain.kernel.common.coercion import as_mapping as _shared_as_mapping
from brain.kernel.common.coercion import int_or_none as _shared_int_or_none
from brain.kernel.common.coercion import optional_text as _shared_optional_text
from brain.systems.learning.budget import (
    BudgetDecisionAction,
    BudgetLane,
    LearningBudgetDecision,
    LearningBudgetEntry,
    LearningBudgetLedger,
    LearningBudgetPolicy,
    ProviderLocation,
    should_run_learning_task,
)

logger = logging.getLogger("learning.after_run_queue")

AFTER_RUN_QUEUE_SCHEMA_VERSION = 1


class AfterRunLearningJobType(StrEnum):
    TRAJECTORY_EVAL_CASE = "trajectory_eval_case_capture"
    CONTEXT_USEFULNESS = "context_usefulness_shell"
    SKILL_RUN_EVIDENCE = "skill_run_evidence_shell"


class AfterRunLearningJobStatus(StrEnum):
    RECORDED = "recorded"
    DEFERRED = "deferred"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class AfterRunSkillReference:
    """Skill identity needed to record L11 skill quality evidence."""

    skill_name: str
    skill_effective_digest: str | None = None
    skill_id: int | None = None
    bundle_namespace: str | None = None
    bundle_name: str | None = None
    bundle_version: str | None = None
    bundle_digest: str | None = None
    task_class: str | None = None
    tool_risk_class: str | None = None
    action_risk_class: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "skill_name", str(self.skill_name or "").strip())
        if self.skill_effective_digest is not None:
            object.__setattr__(
                self,
                "skill_effective_digest",
                str(self.skill_effective_digest).strip()[:96] or None,
            )


@dataclass(frozen=True)
class AfterRunLearningSource:
    """Prepared after-run facts from an already-completed run."""

    run_id: int
    trace_id: str | None = None
    user_id: str | None = None
    org_id: str | None = None
    visibility: str = "private"
    trajectory: Mapping[str, Any] | None = None
    eval_case: Mapping[str, Any] | None = None
    runtime_metadata: Mapping[str, Any] = field(default_factory=dict)
    skill: AfterRunSkillReference | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", int(self.run_id))
        object.__setattr__(self, "visibility", str(self.visibility or "private"))
        object.__setattr__(self, "trajectory", _mapping(self.trajectory))
        object.__setattr__(self, "eval_case", _mapping(self.eval_case))
        object.__setattr__(self, "runtime_metadata", _mapping(self.runtime_metadata))


@dataclass(frozen=True)
class AfterRunLearningJob:
    job_type: AfterRunLearningJobType
    status: AfterRunLearningJobStatus
    digest: str
    reason: str
    budget_decision: LearningBudgetDecision | None = None
    target_ref: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "job_type": self.job_type.value,
            "status": self.status.value,
            "digest": self.digest,
            "reason": self.reason,
            "target_ref": self.target_ref,
            "budget_decision": (
                self.budget_decision.to_payload() if self.budget_decision else None
            ),
            "payload": dict(self.payload or {}),
        }


@dataclass(frozen=True)
class AfterRunLearningQueueResult:
    run_id: int
    jobs: tuple[AfterRunLearningJob, ...]
    ledger: LearningBudgetLedger

    @property
    def recorded_count(self) -> int:
        return sum(1 for job in self.jobs if job.status == AfterRunLearningJobStatus.RECORDED)

    @property
    def deferred_count(self) -> int:
        return sum(1 for job in self.jobs if job.status == AfterRunLearningJobStatus.DEFERRED)

    @property
    def skipped_count(self) -> int:
        return sum(1 for job in self.jobs if job.status == AfterRunLearningJobStatus.SKIPPED)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": AFTER_RUN_QUEUE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "recorded_count": self.recorded_count,
            "deferred_count": self.deferred_count,
            "skipped_count": self.skipped_count,
            "jobs": [job.to_payload() for job in self.jobs],
            "ledger": self.ledger.to_payload(),
        }


class AfterRunLearningQueueService:
    """Budgeted local queue writer for deterministic after-run learning."""

    def __init__(
        self,
        *,
        policy: LearningBudgetPolicy | None = None,
        ledger: LearningBudgetLedger | None = None,
    ) -> None:
        self.policy = policy or LearningBudgetPolicy.from_env()
        self.ledger = ledger or LearningBudgetLedger()

    def queue(
        self,
        source: AfterRunLearningSource,
        *,
        learning_signals: Any,
        trajectory_eval_cases: Any | None = None,
        skill_run_evidence: Any | None = None,
    ) -> AfterRunLearningQueueResult:
        """Record idempotent after-run jobs for the prepared source."""
        ledger = self.ledger
        jobs: list[AfterRunLearningJob] = []

        if source.eval_case:
            job, ledger = self._queue_trajectory_eval_case(
                source,
                ledger=ledger,
                learning_signals=learning_signals,
                trajectory_eval_cases=trajectory_eval_cases,
            )
            jobs.append(job)

        context_payload = _context_usefulness_payload(source)
        if context_payload:
            job, ledger = self._queue_context_usefulness(
                source,
                context_payload=context_payload,
                ledger=ledger,
                learning_signals=learning_signals,
            )
            jobs.append(job)

        if source.skill and source.skill.skill_name:
            job, ledger = self._queue_skill_run_evidence(
                source,
                ledger=ledger,
                learning_signals=learning_signals,
                skill_run_evidence=skill_run_evidence,
            )
            jobs.append(job)

        return AfterRunLearningQueueResult(
            run_id=source.run_id,
            jobs=tuple(jobs),
            ledger=ledger,
        )

    def _budget(
        self,
        source: AfterRunLearningSource,
        *,
        job_type: AfterRunLearningJobType,
        estimated_tokens: int,
        ledger: LearningBudgetLedger,
        sample_key: str,
    ) -> tuple[LearningBudgetDecision, LearningBudgetLedger]:
        decision = should_run_learning_task(
            lane=BudgetLane.AFTER_RUN,
            task_type=job_type.value,
            estimated_tokens=estimated_tokens,
            model_tier="metadata",
            provider_location=ProviderLocation.LOCAL,
            provider="local",
            blocks_user_latency=False,
            org_id=source.org_id,
            user_id=source.user_id,
            sample_key=sample_key,
            policy=self.policy,
            ledger=ledger,
        )
        if not decision.allowed:
            return decision, ledger
        entry = LearningBudgetEntry(
            lane=BudgetLane.AFTER_RUN,
            task_type=job_type.value,
            cost=decision.cost_estimate,
        )
        return decision, ledger.append(entry)

    def _queue_trajectory_eval_case(
        self,
        source: AfterRunLearningSource,
        *,
        ledger: LearningBudgetLedger,
        learning_signals: Any,
        trajectory_eval_cases: Any | None,
    ) -> tuple[AfterRunLearningJob, LearningBudgetLedger]:
        job_type = AfterRunLearningJobType.TRAJECTORY_EVAL_CASE
        eval_case = dict(source.eval_case or {})
        eval_digest = str(eval_case.get("digest") or _stable_digest("eval_case", eval_case))
        digest = _job_digest(source, job_type, eval_digest)
        decision, ledger = self._budget(
            source,
            job_type=job_type,
            estimated_tokens=300,
            ledger=ledger,
            sample_key=digest,
        )
        if not decision.allowed:
            status = _status_from_budget(decision)
            payload = _job_marker_payload(
                source,
                job_type=job_type,
                status=status,
                budget_decision=decision,
                extra={"eval_digest": eval_digest},
            )
            _record_learning_signal(
                learning_signals,
                source,
                job_type=job_type,
                signal_digest=digest,
                status=status,
                payload=payload,
                evidence=_queue_evidence(source, decision=decision),
            )
            return (
                AfterRunLearningJob(
                    job_type=job_type,
                    status=status,
                    digest=digest,
                    reason=decision.reason,
                    budget_decision=decision,
                    payload=payload,
                ),
                ledger,
            )

        if trajectory_eval_cases is None:
            payload = _job_marker_payload(
                source,
                job_type=job_type,
                status=AfterRunLearningJobStatus.SKIPPED,
                budget_decision=decision,
                extra={"eval_digest": eval_digest, "reason": "trajectory eval repository unavailable"},
            )
            _record_learning_signal(
                learning_signals,
                source,
                job_type=job_type,
                signal_digest=digest,
                status=AfterRunLearningJobStatus.SKIPPED,
                payload=payload,
                evidence=_queue_evidence(source, decision=decision),
            )
            return (
                AfterRunLearningJob(
                    job_type=job_type,
                    status=AfterRunLearningJobStatus.SKIPPED,
                    digest=digest,
                    reason="trajectory eval repository unavailable",
                    budget_decision=decision,
                    payload=payload,
                ),
                ledger,
            )

        row = trajectory_eval_cases.upsert_eval_case(
            eval_digest=eval_digest,
            payload=eval_case,
            schema_version=int(eval_case.get("schema_version") or 1),
            redaction_mode=str(eval_case.get("redaction_mode") or "eval"),
            status="active",
            source_run_id=source.run_id,
            trace_id=source.trace_id or _text(eval_case.get("trace_id")),
            trajectory_digest=_trajectory_digest(source),
            context_pack_digest=_context_pack_digest(source),
            skill_effective_digest=_skill_effective_digest(source),
            user_id=source.user_id,
            org_id=source.org_id,
            visibility=source.visibility,
            quality=dict(_mapping(eval_case.get("quality"))),
        )
        payload = _job_marker_payload(
            source,
            job_type=job_type,
            status=AfterRunLearningJobStatus.RECORDED,
            budget_decision=decision,
            extra={"eval_digest": eval_digest, "target_id": getattr(row, "id", None)},
        )
        _record_learning_signal(
            learning_signals,
            source,
            job_type=job_type,
            signal_digest=digest,
            status=AfterRunLearningJobStatus.RECORDED,
            payload=payload,
            evidence=_queue_evidence(source, decision=decision),
        )
        return (
            AfterRunLearningJob(
                job_type=job_type,
                status=AfterRunLearningJobStatus.RECORDED,
                digest=digest,
                reason=decision.reason,
                budget_decision=decision,
                target_ref=eval_digest,
                payload=payload,
            ),
            ledger,
        )

    def _queue_context_usefulness(
        self,
        source: AfterRunLearningSource,
        *,
        context_payload: dict[str, Any],
        ledger: LearningBudgetLedger,
        learning_signals: Any,
    ) -> tuple[AfterRunLearningJob, LearningBudgetLedger]:
        job_type = AfterRunLearningJobType.CONTEXT_USEFULNESS
        digest = _job_digest(source, job_type, _context_pack_digest(source) or "no-context-pack")
        decision, ledger = self._budget(
            source,
            job_type=job_type,
            estimated_tokens=240,
            ledger=ledger,
            sample_key=digest,
        )
        status = AfterRunLearningJobStatus.RECORDED if decision.allowed else _status_from_budget(decision)
        payload = _job_marker_payload(
            source,
            job_type=job_type,
            status=status,
            budget_decision=decision,
            extra={"context": context_payload},
        )
        _record_learning_signal(
            learning_signals,
            source,
            job_type=job_type,
            signal_digest=digest,
            status=status,
            payload=payload,
            evidence=_queue_evidence(source, decision=decision),
        )
        return (
            AfterRunLearningJob(
                job_type=job_type,
                status=status,
                digest=digest,
                reason=decision.reason,
                budget_decision=decision,
                target_ref=_context_pack_digest(source),
                payload=payload,
            ),
            ledger,
        )

    def _queue_skill_run_evidence(
        self,
        source: AfterRunLearningSource,
        *,
        ledger: LearningBudgetLedger,
        learning_signals: Any,
        skill_run_evidence: Any | None,
    ) -> tuple[AfterRunLearningJob, LearningBudgetLedger]:
        job_type = AfterRunLearningJobType.SKILL_RUN_EVIDENCE
        skill = source.skill
        assert skill is not None
        digest_seed = skill.skill_effective_digest or skill.skill_name
        digest = _job_digest(source, job_type, digest_seed)
        if not skill.skill_effective_digest:
            payload = _job_marker_payload(
                source,
                job_type=job_type,
                status=AfterRunLearningJobStatus.SKIPPED,
                budget_decision=None,
                extra={"reason": "skill effective digest unavailable", "skill_name": skill.skill_name},
            )
            _record_learning_signal(
                learning_signals,
                source,
                job_type=job_type,
                signal_digest=digest,
                status=AfterRunLearningJobStatus.SKIPPED,
                payload=payload,
                evidence=_queue_evidence(source, decision=None),
            )
            return (
                AfterRunLearningJob(
                    job_type=job_type,
                    status=AfterRunLearningJobStatus.SKIPPED,
                    digest=digest,
                    reason="skill effective digest unavailable",
                    payload=payload,
                ),
                ledger,
            )

        decision, ledger = self._budget(
            source,
            job_type=job_type,
            estimated_tokens=80,
            ledger=ledger,
            sample_key=digest,
        )
        if not decision.allowed:
            status = _status_from_budget(decision)
            payload = _job_marker_payload(
                source,
                job_type=job_type,
                status=status,
                budget_decision=decision,
                extra={"skill_name": skill.skill_name},
            )
            _record_learning_signal(
                learning_signals,
                source,
                job_type=job_type,
                signal_digest=digest,
                status=status,
                payload=payload,
                evidence=_queue_evidence(source, decision=decision),
            )
            return (
                AfterRunLearningJob(
                    job_type=job_type,
                    status=status,
                    digest=digest,
                    reason=decision.reason,
                    budget_decision=decision,
                    payload=payload,
                ),
                ledger,
            )

        if skill_run_evidence is None:
            payload = _job_marker_payload(
                source,
                job_type=job_type,
                status=AfterRunLearningJobStatus.SKIPPED,
                budget_decision=decision,
                extra={"reason": "skill run evidence repository unavailable"},
            )
            _record_learning_signal(
                learning_signals,
                source,
                job_type=job_type,
                signal_digest=digest,
                status=AfterRunLearningJobStatus.SKIPPED,
                payload=payload,
                evidence=_queue_evidence(source, decision=decision),
            )
            return (
                AfterRunLearningJob(
                    job_type=job_type,
                    status=AfterRunLearningJobStatus.SKIPPED,
                    digest=digest,
                    reason="skill run evidence repository unavailable",
                    budget_decision=decision,
                    payload=payload,
                ),
                ledger,
            )

        outcome = _outcome_label(source)
        row = skill_run_evidence.record_evidence_idempotent(
            skill_id=skill.skill_id,
            skill_name=skill.skill_name,
            skill_effective_digest=skill.skill_effective_digest,
            bundle_namespace=skill.bundle_namespace,
            bundle_name=skill.bundle_name,
            bundle_version=skill.bundle_version,
            bundle_digest=skill.bundle_digest,
            run_id=source.run_id,
            trace_id=source.trace_id,
            task_class=skill.task_class or _task_class(source),
            outcome_label=_text(outcome.get("outcome_class")) or None,
            verifier_status=_text(outcome.get("verifier_signal")) or None,
            user_feedback=_text(outcome.get("user_feedback_signal")) or None,
            token_bucket=_token_bucket(_tokens_total(source)),
            total_tokens=_tokens_total(source),
            cost_bucket=_cost_bucket(_cost_usd(source)),
            cost_usd=_cost_usd(source),
            runtime_bucket=_runtime_bucket(_runtime_ms(source)),
            runtime_ms=_runtime_ms(source),
            tool_risk_class=skill.tool_risk_class,
            action_risk_class=skill.action_risk_class,
            evidence_source="after_run_learning_queue",
            notes="deterministic local after-run shell",
            org_id=source.org_id,
            user_id=source.user_id,
        )
        payload = _job_marker_payload(
            source,
            job_type=job_type,
            status=AfterRunLearningJobStatus.RECORDED,
            budget_decision=decision,
            extra={
                "skill_name": skill.skill_name,
                "skill_effective_digest": skill.skill_effective_digest,
                "target_id": getattr(row, "id", None),
            },
        )
        _record_learning_signal(
            learning_signals,
            source,
            job_type=job_type,
            signal_digest=digest,
            status=AfterRunLearningJobStatus.RECORDED,
            payload=payload,
            evidence=_queue_evidence(source, decision=decision),
        )
        return (
            AfterRunLearningJob(
                job_type=job_type,
                status=AfterRunLearningJobStatus.RECORDED,
                digest=digest,
                reason=decision.reason,
                budget_decision=decision,
                target_ref=skill.skill_effective_digest,
                payload=payload,
            ),
            ledger,
        )


def queue_after_run_learning_for_run(
    run_id: int,
    *,
    policy: LearningBudgetPolicy | None = None,
    ledger: LearningBudgetLedger | None = None,
) -> AfterRunLearningQueueResult | None:
    """Legacy no-op: after-run learning persistence has been removed."""
    logger.debug("after-run learning queue disabled for run %s", run_id)
    return None


def build_eval_case_from_trajectory(trajectory: Mapping[str, Any]) -> dict[str, Any]:
    """Build the compact eval-case payload from an already-built trajectory."""
    trajectory = _mapping(trajectory)
    context = _mapping(trajectory.get("context"))
    quality_signals = _mapping(trajectory.get("quality_signals"))
    summary = _mapping(quality_signals.get("summary"))
    outcome_label = _outcome_label_from_mapping(trajectory)
    payload = {
        "schema_version": 1,
        "trajectory_digest": trajectory.get("digest"),
        "run_id": trajectory.get("run_id"),
        "trace_id": trajectory.get("trace_id"),
        "input": trajectory.get("input_envelope") or {},
        "context_digest": context.get("context_pack_digest"),
        "context_sections": [
            section.get("name")
            for section in context.get("rendered_sections") or []
            if isinstance(section, Mapping)
        ],
        "expected_output": trajectory.get("final_output") or {},
        "tool_calls": trajectory.get("tool_calls") or [],
        "verifier_summary": trajectory.get("verifier_summary") or {},
        "quality": {
            "outcome_kind": summary.get("outcome_kind"),
            "settlement_state": summary.get("settlement_state"),
            "verifier_status": summary.get("verifier_status"),
            "tokens_total": summary.get("tokens_total"),
            "outcome_label": outcome_label,
        },
        "learning_signals": {
            "memory_write_count": len(trajectory.get("memory_writes") or []),
            "feedback": trajectory.get("user_feedback") or {},
            "outcome_label": outcome_label,
        },
    }
    payload["digest"] = _compact_digest(payload)
    return payload


def _mapping(value: Any) -> dict[str, Any]:
    return _shared_as_mapping(value)


def _text(value: Any) -> str | None:
    return _shared_optional_text(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _stable_digest(*parts: Any) -> str:
    raw = json.dumps(_jsonable(parts), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _compact_digest(payload: Mapping[str, Any], *, length: int = 24) -> str:
    return _stable_digest(payload)[:length]


def _job_digest(
    source: AfterRunLearningSource,
    job_type: AfterRunLearningJobType,
    discriminator: Any,
) -> str:
    return _stable_digest(
        {
            "schema_version": AFTER_RUN_QUEUE_SCHEMA_VERSION,
            "job_type": job_type.value,
            "run_id": source.run_id,
            "trajectory_digest": _trajectory_digest(source),
            "discriminator": discriminator,
        }
    )


def _trajectory_digest(source: AfterRunLearningSource) -> str | None:
    return (
        _text(source.eval_case.get("trajectory_digest"))
        or _text(source.trajectory.get("digest"))
        or _text(source.runtime_metadata.get("trajectory_digest"))
    )


def _context_pack_digest(source: AfterRunLearningSource) -> str | None:
    context = _mapping(source.trajectory.get("context"))
    context_pack = _mapping(source.trajectory.get("context_pack"))
    return (
        _text(source.eval_case.get("context_digest"))
        or _text(context.get("context_pack_digest"))
        or _text(context_pack.get("digest"))
        or _text(source.runtime_metadata.get("context_pack_digest"))
    )


def _outcome_label(source: AfterRunLearningSource) -> dict[str, Any]:
    return _outcome_label_from_mapping(source.trajectory) or _outcome_label_from_mapping(source.eval_case)


def _outcome_label_from_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    payload = _mapping(payload)
    if isinstance(payload.get("outcome_label"), Mapping):
        return dict(payload["outcome_label"])
    quality = _mapping(payload.get("quality"))
    if isinstance(quality.get("outcome_label"), Mapping):
        return dict(quality["outcome_label"])
    learning = _mapping(payload.get("learning_signals"))
    if isinstance(learning.get("outcome_label"), Mapping):
        return dict(learning["outcome_label"])
    return {}


def _skill_effective_digest(source: AfterRunLearningSource) -> str | None:
    return source.skill.skill_effective_digest if source.skill else None


def _label_confidence(source: AfterRunLearningSource) -> float | None:
    value = _outcome_label(source).get("label_confidence")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status_from_budget(decision: LearningBudgetDecision) -> AfterRunLearningJobStatus:
    if decision.action == BudgetDecisionAction.SKIP:
        return AfterRunLearningJobStatus.SKIPPED
    return AfterRunLearningJobStatus.DEFERRED


def _job_marker_payload(
    source: AfterRunLearningSource,
    *,
    job_type: AfterRunLearningJobType,
    status: AfterRunLearningJobStatus,
    budget_decision: LearningBudgetDecision | None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": AFTER_RUN_QUEUE_SCHEMA_VERSION,
        "job_type": job_type.value,
        "status": status.value,
        "run_id": source.run_id,
        "trace_id": source.trace_id,
        "trajectory_digest": _trajectory_digest(source),
        "context_pack_digest": _context_pack_digest(source),
        "skill_effective_digest": _skill_effective_digest(source),
        "local_only": True,
        "budget": budget_decision.to_payload() if budget_decision else None,
        **dict(extra or {}),
    }


def _queue_evidence(
    source: AfterRunLearningSource,
    *,
    decision: LearningBudgetDecision | None,
) -> dict[str, Any]:
    return {
        "source": "after_run_learning_queue",
        "source_run_id": source.run_id,
        "trajectory_digest": _trajectory_digest(source),
        "context_pack_digest": _context_pack_digest(source),
        "budget_action": decision.action.value if decision else None,
        "remote_export": False,
    }


def _record_learning_signal(
    learning_signals: Any,
    source: AfterRunLearningSource,
    *,
    job_type: AfterRunLearningJobType,
    signal_digest: str,
    status: AfterRunLearningJobStatus,
    payload: dict[str, Any],
    evidence: dict[str, Any],
) -> Any:
    outcome = _outcome_label(source)
    return learning_signals.record_signal(
        signal_digest=signal_digest,
        signal_type=job_type.value,
        status=status.value,
        review_status="unreviewed",
        source_run_id=source.run_id,
        trace_id=source.trace_id,
        trajectory_digest=_trajectory_digest(source),
        context_pack_digest=_context_pack_digest(source),
        skill_effective_digest=_skill_effective_digest(source),
        user_id=source.user_id,
        org_id=source.org_id,
        visibility=source.visibility,
        outcome_label=_text(outcome.get("outcome_class")),
        label_confidence=_label_confidence(source),
        payload=payload,
        evidence=evidence,
    )


def _context_usefulness_payload(source: AfterRunLearningSource) -> dict[str, Any]:
    from brain.systems.learning.context_signals import build_context_usefulness_payload

    return build_context_usefulness_payload(
        trajectory=source.trajectory,
        eval_case=source.eval_case,
        runtime_metadata=source.runtime_metadata,
        run_id=source.run_id,
        trace_id=source.trace_id,
    )


def _skill_reference_from_trajectory(
    trajectory: Mapping[str, Any],
    *,
    fallback_skill_name: str | None = None,
    skill_repo: Any | None = None,
) -> AfterRunSkillReference | None:
    selected: dict[str, Any] = {}
    context_pack = _mapping(trajectory.get("context_pack"))
    sections = _mapping(context_pack.get("sections"))
    selected_skills = _mapping(sections.get("selected_skills"))
    content = _mapping(selected_skills.get("content"))
    if isinstance(content.get("selected"), Mapping):
        selected = dict(content["selected"])
    skill_record = _mapping(selected.get("skill_record"))
    name = _text(selected.get("name")) or _text(skill_record.get("name")) or _text(fallback_skill_name)
    if not name:
        return None

    resolved = None
    effective_digest = _text(skill_record.get("effective_digest")) or _text(skill_record.get("bundle_digest"))
    if skill_repo is not None and not effective_digest:
        try:
            resolved = skill_repo.get_by_name(name)
            if resolved is not None:
                effective_digest = _text(getattr(resolved, "effective_digest", None)) or _text(
                    getattr(resolved, "bundle_digest", None)
                )
        except Exception as exc:
            logger.debug("skill lookup skipped for after-run queue skill=%s: %s", name, exc)

    return AfterRunSkillReference(
        skill_id=getattr(resolved, "id", None),
        skill_name=name,
        skill_effective_digest=effective_digest,
        bundle_namespace=_text(skill_record.get("bundle_namespace")) or getattr(resolved, "bundle_namespace", None),
        bundle_name=_text(skill_record.get("bundle_name")) or getattr(resolved, "bundle_name", None),
        bundle_version=_text(skill_record.get("skill_version")) or getattr(resolved, "version", None),
        bundle_digest=_text(skill_record.get("bundle_digest")) or getattr(resolved, "bundle_digest", None),
        task_class=_text(_mapping(trajectory.get("input_envelope")).get("event")),
        tool_risk_class=_tool_risk_class(trajectory),
        action_risk_class=_action_risk_class(trajectory),
    )


def _task_class(source: AfterRunLearningSource) -> str | None:
    if source.skill and source.skill.task_class:
        return source.skill.task_class
    return _text(_mapping(source.trajectory.get("input_envelope")).get("event"))


def _tokens_total(source: AfterRunLearningSource) -> int | None:
    quality = _mapping(source.eval_case.get("quality"))
    summary = _mapping(_mapping(source.trajectory.get("quality_signals")).get("summary"))
    return _int_or_none(
        quality.get("tokens_total")
        or summary.get("tokens_total")
        or source.runtime_metadata.get("tokens_total")
    )


def _runtime_ms(source: AfterRunLearningSource) -> int | None:
    lease = _mapping(source.trajectory.get("lease"))
    return _int_or_none(
        lease.get("total_duration_ms")
        or source.runtime_metadata.get("total_duration_ms")
        or source.runtime_metadata.get("runtime_ms")
    )


def _cost_usd(source: AfterRunLearningSource) -> float | None:
    summary = _mapping(_mapping(source.trajectory.get("quality_signals")).get("summary"))
    value = (
        summary.get("estimated_cost")
        or source.runtime_metadata.get("estimated_cost")
        or source.runtime_metadata.get("cost_usd")
    )
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    return _shared_int_or_none(value)


def _token_bucket(tokens: int | None) -> str | None:
    if tokens is None:
        return None
    if tokens < 8_000:
        return "small"
    if tokens < 64_000:
        return "medium"
    if tokens < 200_000:
        return "large"
    return "xlarge"


def _runtime_bucket(runtime_ms: int | None) -> str | None:
    if runtime_ms is None:
        return None
    if runtime_ms < 5 * 60 * 1000:
        return "fast"
    if runtime_ms < 30 * 60 * 1000:
        return "normal"
    return "slow"


def _cost_bucket(cost_usd: float | None) -> str | None:
    if cost_usd is None:
        return None
    if cost_usd < 0.05:
        return "small"
    if cost_usd < 0.50:
        return "medium"
    if cost_usd < 2.00:
        return "large"
    return "xlarge"


def _tool_risk_class(trajectory: Mapping[str, Any]) -> str | None:
    risks = {
        _text(action.get("risk"))
        for action in trajectory.get("action_manifests") or []
        if isinstance(action, Mapping)
    }
    risks.discard(None)
    if not risks:
        return None
    if "high" in risks:
        return "high"
    if "medium" in risks:
        return "medium"
    return "low"


def _action_risk_class(trajectory: Mapping[str, Any]) -> str | None:
    actions = [action for action in trajectory.get("action_manifests") or [] if isinstance(action, Mapping)]
    if not actions:
        return None
    if any(action.get("approval_required") for action in actions):
        return "approval_required"
    if any(_text(action.get("outcome_status")) not in {None, "completed", "success"} for action in actions):
        return "non_success_action"
    return "audit_only"


__all__ = [
    "AFTER_RUN_QUEUE_SCHEMA_VERSION",
    "AfterRunLearningJob",
    "AfterRunLearningJobStatus",
    "AfterRunLearningJobType",
    "AfterRunLearningQueueResult",
    "AfterRunLearningQueueService",
    "AfterRunLearningSource",
    "AfterRunSkillReference",
    "build_eval_case_from_trajectory",
    "queue_after_run_learning_for_run",
]
