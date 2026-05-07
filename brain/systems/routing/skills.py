"""Advisory skill routing policy with quality gates.

The policy is deliberately small and deterministic. It can annotate or rerank
``brain_skills`` recommendation cards when explicitly enabled, but it preserves
baseline ordering when quality evidence is sparse or when trust rules would make
an automatic promotion unsafe.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from brain.kernel.common.env import env_flag as _shared_env_flag
from brain.kernel.common.env import env_float as _shared_env_float


_ACTIVE_FLAG = "SKILL_QUALITY_ROUTING_ENABLED"
SKILL_ROUTING_POLICY_VERSION = "skill-quality-routing-v1"
_GLOBAL_ACTIVE_FLAG = "LEARNING_POLICY_SKILL_QUALITY_ROUTING_ENABLED"
_GLOBAL_DISABLED_FLAG = "LEARNING_POLICY_SKILL_QUALITY_ROUTING_DISABLED"
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_TRUST_RANK = {
    "illo_core": 5,
    "illo-core": 5,
    "private_local": 4,
    "self_hosted": 4,
    "self-hosted": 4,
    "local": 3,
    "legacy_db": 3,
    "marketplace": 2,
    "public": 1,
    "agent_draft": 0,
}
_PUBLIC_TRUST_LEVELS = {"public", "marketplace", "agent_draft"}
_TRUSTED_PRIVATE_LEVELS = {"illo_core", "illo-core", "private_local", "self_hosted", "self-hosted", "local"}


@dataclass(frozen=True)
class SkillRoutingQualityPolicy:
    """Conservative gates for quality-aware skill routing."""

    active_enabled: bool = False
    global_enabled: bool = True
    min_quality_confidence: float = 0.45
    min_sample_size_confidence: float = 0.35
    min_quality_delta_to_rerank: float = 0.08
    max_quality_bonus: float = 0.20
    max_quality_penalty: float = 0.22
    allow_public_over_trusted_private: bool = False
    allow_untrusted_auto_select: bool = False
    preserve_baseline_on_low_confidence: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "global_enabled", bool(self.global_enabled))
        object.__setattr__(self, "active_enabled", bool(self.active_enabled and self.global_enabled))

    @classmethod
    def from_env(cls) -> "SkillRoutingQualityPolicy":
        global_enabled = _learning_policy_skill_routing_enabled()
        return cls(
            active_enabled=_env_flag(_ACTIVE_FLAG, default=False),
            global_enabled=global_enabled,
            min_quality_confidence=_env_float("SKILL_QUALITY_MIN_CONFIDENCE", 0.45),
            min_sample_size_confidence=_env_float("SKILL_QUALITY_MIN_SAMPLE_CONFIDENCE", 0.35),
            min_quality_delta_to_rerank=_env_float("SKILL_QUALITY_MIN_DELTA_TO_RERANK", 0.08),
            allow_public_over_trusted_private=_env_flag(
                "SKILL_QUALITY_ALLOW_PUBLIC_OVER_TRUSTED_PRIVATE",
                default=False,
            ),
            allow_untrusted_auto_select=_env_flag("SKILL_QUALITY_ALLOW_UNTRUSTED_AUTO_SELECT", default=False),
        )


@dataclass(frozen=True)
class SkillRoutingCandidate:
    """One skill recommendation with optional quality metadata."""

    name: str | None
    baseline_rank: int
    baseline_score: float
    effective_digest: str | None = None
    trust_level: str | None = None
    task_class: str | None = None
    quality_payload: Mapping[str, Any] | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def quality_score(self) -> float | None:
        return _quality_score(self.quality_payload)

    @property
    def quality_confidence(self) -> float:
        return _quality_confidence(self.quality_payload)

    @property
    def sample_confidence(self) -> float:
        evidence = _as_mapping((self.quality_payload or {}).get("evidence"))
        return _clamp(_float(evidence.get("sample_size_confidence"), 0.0))

    @property
    def quality_ready(self) -> bool:
        return self.quality_score is not None


@dataclass(frozen=True)
class SkillRoutingDecision:
    """Routing decision payload for one ordered recommendation list."""

    active_enabled: bool
    selected_skill: Mapping[str, Any] | None
    recommendations: tuple[Mapping[str, Any], ...]
    reasons: tuple[str, ...]
    policy: Mapping[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "active_enabled": self.active_enabled,
            "selected_skill": dict(self.selected_skill or {}),
            "recommended_skills": [dict(item) for item in self.recommendations],
            "reasons": list(self.reasons),
            "policy": dict(self.policy),
        }


def route_skills_with_quality(
    recommendations: Sequence[Mapping[str, Any]] | None,
    *,
    quality_by_digest: Mapping[str, Mapping[str, Any]] | None = None,
    quality_by_name: Mapping[str, Mapping[str, Any]] | None = None,
    task_class: str | None = None,
    policy: SkillRoutingQualityPolicy | None = None,
) -> SkillRoutingDecision:
    """Return quality-annotated skill recommendations.

    When ``policy.active_enabled`` is false, baseline ordering is preserved and
    quality metadata is advisory only.
    """
    policy = policy or SkillRoutingQualityPolicy.from_env()
    candidates = _build_candidates(
        recommendations or (),
        quality_by_digest=quality_by_digest or {},
        quality_by_name=quality_by_name or {},
        task_class=task_class,
    )
    if not candidates:
        return SkillRoutingDecision(
            active_enabled=policy.active_enabled,
            selected_skill=None,
            recommendations=(),
            reasons=("no skill recommendations available",),
            policy=_policy_payload(policy),
        )

    annotated = [_annotate_candidate(candidate, policy=policy) for candidate in candidates]
    reasons: list[str] = []
    if not policy.active_enabled:
        if not policy.global_enabled:
            reasons.append("skill quality routing disabled by learning policy kill switch; baseline order preserved")
        else:
            reasons.append("skill quality routing disabled; baseline order preserved")
        ordered = annotated
    else:
        ordered = _active_order(candidates, annotated=annotated, policy=policy, reasons=reasons)

    selected = ordered[0] if ordered else None
    return SkillRoutingDecision(
        active_enabled=policy.active_enabled,
        selected_skill=selected,
        recommendations=tuple(ordered),
        reasons=tuple(dict.fromkeys(reasons)),
        policy=_policy_payload(policy),
    )


def apply_skill_quality_routing_to_plan(
    plan: Mapping[str, Any],
    *,
    quality_by_digest: Mapping[str, Mapping[str, Any]] | None = None,
    quality_by_name: Mapping[str, Mapping[str, Any]] | None = None,
    task_class: str | None = None,
    policy: SkillRoutingQualityPolicy | None = None,
) -> dict[str, Any]:
    """Return a copy of a ``brain_skills`` plan with quality routing metadata."""
    updated = dict(plan or {})
    decision = route_skills_with_quality(
        updated.get("recommended_skills") or [],
        quality_by_digest=quality_by_digest,
        quality_by_name=quality_by_name,
        task_class=task_class,
        policy=policy,
    )
    updated["recommended_skills"] = [dict(item) for item in decision.recommendations]
    updated["skill_quality_routing"] = {
        "active_enabled": decision.active_enabled,
        "selected_skill": dict(decision.selected_skill or {}),
        "reasons": list(decision.reasons),
        "policy": dict(decision.policy),
    }
    return updated


def skill_routing_policy_payload(policy: SkillRoutingQualityPolicy | None = None) -> dict[str, Any]:
    """Return compact versioned routing policy metadata for hot-path hints."""
    return {
        "policy_version": SKILL_ROUTING_POLICY_VERSION,
        **_policy_payload(policy or SkillRoutingQualityPolicy()),
    }


def _build_candidates(
    recommendations: Sequence[Mapping[str, Any]],
    *,
    quality_by_digest: Mapping[str, Mapping[str, Any]],
    quality_by_name: Mapping[str, Mapping[str, Any]],
    task_class: str | None,
) -> list[SkillRoutingCandidate]:
    candidates: list[SkillRoutingCandidate] = []
    for index, item in enumerate(recommendations, start=1):
        if not isinstance(item, Mapping):
            continue
        digest = _text(item.get("effective_digest") or item.get("skill_effective_digest"))
        name = _text(item.get("name") or item.get("skill_name"))
        quality = _as_mapping(item.get("quality"))
        if not quality and digest:
            quality = _as_mapping(quality_by_digest.get(digest))
        if not quality and name:
            quality = _as_mapping(quality_by_name.get(name))
        candidates.append(
            SkillRoutingCandidate(
                name=name,
                baseline_rank=int(item.get("rank") or index),
                baseline_score=_float(item.get("match_score") or item.get("score"), 0.0),
                effective_digest=digest,
                trust_level=_text(item.get("trust_level")),
                task_class=_text(item.get("task_class") or task_class),
                quality_payload=quality or None,
                raw=dict(item),
            )
        )
    return candidates


def _annotate_candidate(
    candidate: SkillRoutingCandidate,
    *,
    policy: SkillRoutingQualityPolicy,
) -> dict[str, Any]:
    quality_score = candidate.quality_score
    quality_confidence = candidate.quality_confidence
    sample_confidence = candidate.sample_confidence
    gate = _quality_gate(candidate, policy=policy)
    bonus = _quality_adjustment(candidate, policy=policy) if gate["eligible_for_rerank"] else 0.0
    final_score = _clamp(candidate.baseline_score + bonus)
    payload = dict(candidate.raw)
    payload.setdefault("rank", candidate.baseline_rank)
    payload["quality_routing"] = {
        "quality_score": quality_score,
        "quality_confidence": quality_confidence,
        "sample_size_confidence": sample_confidence,
        "baseline_score": round(candidate.baseline_score, 4),
        "quality_adjustment": round(bonus, 4),
        "final_score": round(final_score, 4),
        "eligible_for_rerank": gate["eligible_for_rerank"],
        "gate_reasons": gate["reasons"],
        "advisory_only": not policy.active_enabled,
    }
    if candidate.quality_payload:
        payload["quality"] = _safe_quality_summary(candidate.quality_payload)
    return payload


def _active_order(
    candidates: Sequence[SkillRoutingCandidate],
    *,
    annotated: Sequence[Mapping[str, Any]],
    policy: SkillRoutingQualityPolicy,
    reasons: list[str],
) -> list[Mapping[str, Any]]:
    baseline = list(annotated)
    if len(baseline) <= 1:
        reasons.append("single recommendation; no rerank needed")
        return baseline

    trusted_baseline_top = _trust_rank(_text(baseline[0].get("trust_level")))
    scored: list[tuple[float, int, Mapping[str, Any], SkillRoutingCandidate]] = []
    for candidate, payload in zip(candidates, annotated, strict=False):
        qr = _as_mapping(payload.get("quality_routing"))
        final_score = _float(qr.get("final_score"), candidate.baseline_score)
        if not qr.get("eligible_for_rerank"):
            final_score = candidate.baseline_score
        if _would_violate_trust_top(candidate, trusted_baseline_top, policy=policy):
            reasons.append(f"{candidate.name or candidate.effective_digest} blocked by hosted trust gate")
            final_score = min(final_score, candidate.baseline_score)
        scored.append((final_score, -candidate.baseline_rank, payload, candidate))

    ordered = [payload for _score, _rank, payload, _candidate in sorted(scored, key=lambda item: (-item[0], item[1]))]
    if ordered and ordered[0].get("name") != baseline[0].get("name"):
        baseline_top_score = _float(_as_mapping(baseline[0].get("quality_routing")).get("final_score"), 0.0)
        new_top_score = _float(_as_mapping(ordered[0].get("quality_routing")).get("final_score"), 0.0)
        if new_top_score - baseline_top_score < policy.min_quality_delta_to_rerank:
            reasons.append("quality delta below rerank threshold; baseline top preserved")
            return baseline
        reasons.append("skill quality routing changed recommendation order")
    else:
        reasons.append("quality gates preserved baseline order")

    reranked: list[Mapping[str, Any]] = []
    for rank, payload in enumerate(ordered, start=1):
        updated = dict(payload)
        updated["quality_rank"] = rank
        reranked.append(updated)
    return reranked


def _quality_gate(candidate: SkillRoutingCandidate, *, policy: SkillRoutingQualityPolicy) -> dict[str, Any]:
    reasons: list[str] = []
    if not candidate.quality_ready:
        reasons.append("no quality score available")
    if candidate.quality_confidence < policy.min_quality_confidence:
        reasons.append("quality confidence below threshold")
    if candidate.sample_confidence < policy.min_sample_size_confidence:
        reasons.append("sample size confidence below threshold")
    trust_level = _text(candidate.trust_level)
    if trust_level in _PUBLIC_TRUST_LEVELS and not policy.allow_untrusted_auto_select:
        reasons.append("untrusted or public skill remains gated")
    eligible = not reasons
    return {"eligible_for_rerank": eligible, "reasons": reasons or ["quality evidence eligible"]}


def _quality_adjustment(candidate: SkillRoutingCandidate, *, policy: SkillRoutingQualityPolicy) -> float:
    score = candidate.quality_score
    if score is None:
        return 0.0
    centered = score - 0.5
    if centered >= 0:
        return min(policy.max_quality_bonus, centered * 0.4)
    return max(-policy.max_quality_penalty, centered * 0.45)


def _would_violate_trust_top(
    candidate: SkillRoutingCandidate,
    baseline_top_trust_rank: int,
    *,
    policy: SkillRoutingQualityPolicy,
) -> bool:
    trust = _text(candidate.trust_level)
    if policy.allow_public_over_trusted_private:
        return False
    if trust in _PUBLIC_TRUST_LEVELS and baseline_top_trust_rank >= _trust_rank("private_local"):
        return True
    return False


def _safe_quality_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _as_mapping(payload.get("evidence"))
    return {
        "schema_version": payload.get("schema_version"),
        "advisory_only": payload.get("advisory_only", True),
        "score": _quality_score(payload),
        "confidence": _quality_confidence(payload),
        "rating": payload.get("rating"),
        "evidence": {
            "count": evidence.get("count"),
            "sample_size_confidence": evidence.get("sample_size_confidence"),
        },
        "reasons": list(payload.get("reasons") or [])[:5],
    }


def _quality_score(payload: Mapping[str, Any] | None) -> float | None:
    if not payload:
        return None
    if payload.get("score") is None:
        return None
    return _clamp(_float(payload.get("score"), 0.5))


def _quality_confidence(payload: Mapping[str, Any] | None) -> float:
    if not payload:
        return 0.0
    return _clamp(_float(payload.get("confidence"), 0.0))


def _policy_payload(policy: SkillRoutingQualityPolicy) -> dict[str, Any]:
    return {
        "active_flag": _ACTIVE_FLAG,
        "global_active_flag": _GLOBAL_ACTIVE_FLAG,
        "global_disabled_flag": _GLOBAL_DISABLED_FLAG,
        "global_enabled": policy.global_enabled,
        "active_enabled": policy.active_enabled,
        "min_quality_confidence": policy.min_quality_confidence,
        "min_sample_size_confidence": policy.min_sample_size_confidence,
        "min_quality_delta_to_rerank": policy.min_quality_delta_to_rerank,
        "allow_public_over_trusted_private": policy.allow_public_over_trusted_private,
        "allow_untrusted_auto_select": policy.allow_untrusted_auto_select,
    }


def _env_flag(name: str, *, default: bool) -> bool:
    return _shared_env_flag(name, default=default, true_only=True, true_values=_TRUE_VALUES)


def _env_float(name: str, default: float) -> float:
    return _clamp(_shared_env_float(name, default))


def _learning_policy_skill_routing_enabled() -> bool:
    try:
        from brain.systems.learning.policy import build_learning_policy_from_env

        return build_learning_policy_from_env().skill_quality_routing_enabled
    except Exception:
        return True


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _trust_rank(value: str | None) -> int:
    return _TRUST_RANK.get(str(value or "").strip().lower(), 2)


__all__ = [
    "SKILL_ROUTING_POLICY_VERSION",
    "SkillRoutingCandidate",
    "SkillRoutingDecision",
    "SkillRoutingQualityPolicy",
    "apply_skill_quality_routing_to_plan",
    "route_skills_with_quality",
    "skill_routing_policy_payload",
]
