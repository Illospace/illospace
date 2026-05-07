"""Evidence-gated graduation for agent-drafted skills.

This module does not mutate skills by itself. It turns repeated successful use,
redacted eval coverage, bundle validation, permission review, and quality scores
into an explicit graduation decision that an admin/night worker can apply later.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from brain.systems.skills.gate import validate_skill_structure


GRADUATION_SCHEMA_VERSION = 1


class SkillGraduationAction(StrEnum):
    KEEP_DRAFT = "keep_draft"
    NEEDS_EVAL = "needs_eval"
    NEEDS_REVIEW = "needs_review"
    READY_FOR_PRIVATE_PROMOTION = "ready_for_private_promotion"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class SkillGraduationPolicy:
    """Thresholds for promoting an agent-drafted skill."""

    min_successful_runs: int = 3
    min_eval_cases: int = 2
    min_quality_score: float = 0.62
    min_quality_confidence: float = 0.45
    min_sample_size_confidence: float = 0.30
    hosted_requires_review: bool = True
    allow_permission_escalation_without_review: bool = False


@dataclass(frozen=True)
class SkillGraduationEvidence:
    """Normalized evidence for one skill graduation candidate."""

    skill_name: str
    source_kind: str = "agent_draft"
    trust_level: str = "agent_draft"
    provisional: bool = True
    description: str | None = None
    procedure: str | None = None
    repeated_successful_runs: int = 0
    similar_task_count: int = 0
    redacted_eval_case_count: int = 0
    bundle_validation_passed: bool = False
    unresolved_permission_escalation: bool = False
    hosted_mode: bool = False
    tenant_review_approved: bool = False
    quality_score_payload: Mapping[str, Any] | None = None
    generated_eval_digests: tuple[str, ...] = ()
    permission_review_notes: tuple[str, ...] = ()

    @classmethod
    def from_sources(
        cls,
        skill: Mapping[str, Any] | Any,
        *,
        quality_score_payload: Mapping[str, Any] | Any | None = None,
        eval_cases: Sequence[Mapping[str, Any] | Any] | None = None,
        repeated_successful_runs: int | None = None,
        similar_task_count: int | None = None,
        bundle_validation_passed: bool | None = None,
        unresolved_permission_escalation: bool | None = None,
        hosted_mode: bool = False,
        tenant_review_approved: bool = False,
    ) -> "SkillGraduationEvidence":
        data = _object_to_dict(skill)
        quality = _quality_payload(quality_score_payload)
        eval_rows = tuple(eval_cases or ())
        redacted_eval_count = sum(1 for item in eval_rows if _is_redacted_eval_case(item))
        digests = tuple(
            digest
            for digest in (_text(_object_to_dict(item).get("eval_digest") or _object_to_dict(item).get("digest")) for item in eval_rows)
            if digest
        )
        success_count = (
            repeated_successful_runs
            if repeated_successful_runs is not None
            else _int(data.get("success_count"), 0)
        )
        return cls(
            skill_name=_text(data.get("name") or data.get("skill_name")) or "",
            source_kind=_text(data.get("source_kind")) or "agent_draft",
            trust_level=_text(data.get("trust_level")) or "agent_draft",
            provisional=_bool(data.get("provisional"), default=True),
            description=_text(data.get("description")),
            procedure=_text(data.get("procedure")),
            repeated_successful_runs=max(0, int(success_count or 0)),
            similar_task_count=max(0, int(similar_task_count if similar_task_count is not None else success_count or 0)),
            redacted_eval_case_count=redacted_eval_count,
            bundle_validation_passed=bool(
                bundle_validation_passed
                if bundle_validation_passed is not None
                else data.get("bundle_validation_passed", False)
            ),
            unresolved_permission_escalation=bool(
                unresolved_permission_escalation
                if unresolved_permission_escalation is not None
                else data.get("unresolved_permission_escalation", False)
            ),
            hosted_mode=bool(hosted_mode),
            tenant_review_approved=bool(tenant_review_approved),
            quality_score_payload=quality or None,
            generated_eval_digests=digests,
        )


@dataclass(frozen=True)
class SkillGraduationDecision:
    """Advisory decision for one candidate skill."""

    action: SkillGraduationAction
    skill_name: str
    eligible: bool
    reasons: tuple[str, ...]
    blockers: tuple[str, ...] = ()
    required_review: bool = False
    promotion_payload: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = GRADUATION_SCHEMA_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "skill_name": self.skill_name,
            "action": self.action.value,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "blockers": list(self.blockers),
            "required_review": self.required_review,
            "promotion_payload": dict(self.promotion_payload),
            "evidence": dict(self.evidence),
        }


def evaluate_skill_graduation(
    evidence: SkillGraduationEvidence | Mapping[str, Any] | Any,
    *,
    policy: SkillGraduationPolicy | None = None,
) -> SkillGraduationDecision:
    """Return an advisory graduation decision for an agent-drafted skill."""
    policy = policy or SkillGraduationPolicy()
    candidate = evidence if isinstance(evidence, SkillGraduationEvidence) else SkillGraduationEvidence.from_sources(evidence)
    reasons: list[str] = []
    blockers: list[str] = []

    if candidate.source_kind != "agent_draft" or candidate.trust_level != "agent_draft":
        reasons.append("skill is not an agent draft; graduation gate is advisory only")
        return _decision(
            SkillGraduationAction.KEEP_DRAFT,
            candidate,
            eligible=False,
            reasons=reasons,
            blockers=blockers,
            policy=policy,
        )

    structure_violations = validate_skill_structure(
        candidate.skill_name,
        candidate.description,
        candidate.procedure,
        strict=False,
    )
    if structure_violations:
        blockers.extend(f"structure: {violation}" for violation in structure_violations)

    if candidate.repeated_successful_runs < policy.min_successful_runs:
        blockers.append("not enough repeated successful runs")
    if candidate.similar_task_count < policy.min_successful_runs:
        blockers.append("not enough similar task evidence")
    if candidate.redacted_eval_case_count < policy.min_eval_cases:
        blockers.append("not enough redacted eval cases")
    if not candidate.bundle_validation_passed:
        blockers.append("skill bundle validation has not passed")
    if candidate.unresolved_permission_escalation:
        blockers.append("unresolved permission escalation")

    quality = _quality_metrics(candidate.quality_score_payload)
    if quality["score"] < policy.min_quality_score:
        blockers.append("quality score below graduation threshold")
    if quality["confidence"] < policy.min_quality_confidence:
        blockers.append("quality confidence below graduation threshold")
    if quality["sample_size_confidence"] < policy.min_sample_size_confidence:
        blockers.append("quality sample size confidence below graduation threshold")

    if blockers:
        action = (
            SkillGraduationAction.NEEDS_EVAL
            if any("eval" in blocker or "quality" in blocker for blocker in blockers)
            else SkillGraduationAction.BLOCKED
        )
        return _decision(action, candidate, eligible=False, reasons=reasons, blockers=blockers, policy=policy)

    requires_review = bool(candidate.hosted_mode and policy.hosted_requires_review and not candidate.tenant_review_approved)
    if requires_review:
        reasons.append("hosted mode requires tenant/admin review before promotion")
        return _decision(
            SkillGraduationAction.NEEDS_REVIEW,
            candidate,
            eligible=False,
            reasons=reasons,
            blockers=(),
            policy=policy,
            required_review=True,
        )

    reasons.append("graduation evidence passed all gates")
    return _decision(
        SkillGraduationAction.READY_FOR_PRIVATE_PROMOTION,
        candidate,
        eligible=True,
        reasons=reasons,
        blockers=(),
        policy=policy,
    )


def build_skill_graduation_update(
    decision: SkillGraduationDecision,
    *,
    approved_by: str | None = None,
) -> dict[str, Any]:
    """Return fields a caller may apply after reviewing a ready decision."""
    if decision.action != SkillGraduationAction.READY_FOR_PRIVATE_PROMOTION or not decision.eligible:
        raise ValueError("skill graduation update requires a ready promotion decision")
    return {
        "source_kind": "private_local",
        "trust_level": "private_local",
        "provisional": False,
        "review_status": "approved",
        "graduation_schema_version": decision.schema_version,
        "graduation_reason": "; ".join(decision.reasons),
        "graduation_evidence": dict(decision.evidence),
        "graduated_by": approved_by,
    }


def _decision(
    action: SkillGraduationAction,
    candidate: SkillGraduationEvidence,
    *,
    eligible: bool,
    reasons: Sequence[str],
    blockers: Sequence[str],
    policy: SkillGraduationPolicy,
    required_review: bool = False,
) -> SkillGraduationDecision:
    quality = _quality_metrics(candidate.quality_score_payload)
    evidence_payload = {
        "source_kind": candidate.source_kind,
        "trust_level": candidate.trust_level,
        "provisional": candidate.provisional,
        "repeated_successful_runs": candidate.repeated_successful_runs,
        "similar_task_count": candidate.similar_task_count,
        "redacted_eval_case_count": candidate.redacted_eval_case_count,
        "bundle_validation_passed": candidate.bundle_validation_passed,
        "unresolved_permission_escalation": candidate.unresolved_permission_escalation,
        "hosted_mode": candidate.hosted_mode,
        "tenant_review_approved": candidate.tenant_review_approved,
        "quality": quality,
        "generated_eval_digests": list(candidate.generated_eval_digests),
    }
    promotion_payload: dict[str, Any] = {}
    if action == SkillGraduationAction.READY_FOR_PRIVATE_PROMOTION:
        promotion_payload = {
            "source_kind": "private_local",
            "trust_level": "private_local",
            "provisional": False,
            "permission_escalation": False,
        }
    return SkillGraduationDecision(
        action=action,
        skill_name=candidate.skill_name,
        eligible=eligible,
        reasons=tuple(dict.fromkeys(reasons)),
        blockers=tuple(dict.fromkeys(blockers)),
        required_review=required_review,
        promotion_payload=promotion_payload,
        evidence={
            **evidence_payload,
            "policy": {
                "min_successful_runs": policy.min_successful_runs,
                "min_eval_cases": policy.min_eval_cases,
                "min_quality_score": policy.min_quality_score,
                "min_quality_confidence": policy.min_quality_confidence,
                "min_sample_size_confidence": policy.min_sample_size_confidence,
                "hosted_requires_review": policy.hosted_requires_review,
            },
        },
    )


def _is_redacted_eval_case(value: Any) -> bool:
    data = _object_to_dict(value)
    mode = _text(data.get("mode") or data.get("redaction_mode"))
    if mode in {"hosted_eval", "external", "community"}:
        return True
    policy = data.get("privacy_policy")
    if isinstance(policy, Mapping):
        return not bool(policy.get("include_raw_memory_content")) and not bool(policy.get("include_tenant_identifiers"))
    return False


def _quality_payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "to_payload"):
        return _object_to_dict(value.to_payload())
    return _object_to_dict(value)


def _quality_metrics(value: Mapping[str, Any] | None) -> dict[str, float]:
    payload = value or {}
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), Mapping) else {}
    return {
        "score": _float(payload.get("score"), 0.5),
        "confidence": _float(payload.get("confidence"), 0.0),
        "sample_size_confidence": _float(evidence.get("sample_size_confidence"), 0.0),
    }


def _object_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return {}


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


__all__ = [
    "GRADUATION_SCHEMA_VERSION",
    "SkillGraduationAction",
    "SkillGraduationDecision",
    "SkillGraduationEvidence",
    "SkillGraduationPolicy",
    "build_skill_graduation_update",
    "evaluate_skill_graduation",
]
