"""Advisory skill quality scoring from recorded run evidence.

This module is intentionally persistence- and routing-free. It consumes the
L11 ``SkillRunEvidence`` row shape, or equivalent in-memory objects in tests,
and returns a deterministic advisory score for future dashboards and offline
learning jobs.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


SKILL_QUALITY_SCORE_SCHEMA_VERSION = 1
DEFAULT_EVIDENCE_LIMIT = 200
_BASELINE_SCORE = 0.5
_FULL_CONFIDENCE_SAMPLE_SIZE = 30


_SUCCESS_OUTCOMES = frozenset(
    {
        "accepted",
        "complete",
        "completed",
        "good",
        "ok",
        "pass",
        "passed",
        "satisfied",
        "settled_success",
        "success",
        "successful",
        "succeeded",
    }
)
_PARTIAL_OUTCOMES = frozenset(
    {
        "blocked",
        "partial",
        "partially_successful",
        "settled_partial",
        "uncertain",
        "weak",
    }
)
_FAILURE_OUTCOMES = frozenset(
    {
        "bad",
        "canceled",
        "cancelled",
        "error",
        "expired",
        "fail",
        "failed",
        "failure",
        "rejected",
        "settled_failure",
        "superseded",
        "timeout",
        "timed_out",
        "unsatisfied",
    }
)

_PASS_VERIFIERS = frozenset(
    {
        "accepted",
        "complete",
        "completed",
        "ok",
        "pass",
        "passed",
        "satisfied",
        "success",
        "successful",
    }
)
_FAIL_VERIFIERS = frozenset(
    {"bad", "error", "fail", "failed", "failure", "rejected", "unsatisfied"}
)

_POSITIVE_FEEDBACK = frozenset(
    {"accepted", "good", "great", "helpful", "positive", "satisfied", "thumbs_up"}
)
_NEGATIVE_FEEDBACK = frozenset(
    {"bad", "failed", "failure", "incorrect", "negative", "rejected", "wrong"}
)
_CORRECTION_FEEDBACK = frozenset(
    {"actually", "corrected", "correction", "redo", "retry", "rework", "wrong"}
)

_TRUST_LEVEL_SCORES = {
    "illo_core": 1.0,
    "illo-core": 1.0,
    "marketplace": 0.85,
    "public": 0.72,
    "private_local": 0.65,
    "self_hosted": 0.65,
    "self-hosted": 0.65,
    "legacy_db": 0.55,
    "local": 0.55,
    "agent_draft": 0.35,
}


@dataclass(frozen=True, slots=True)
class SkillQualitySignal:
    """One normalized signal contributing to an advisory quality score."""

    name: str
    score: float
    weight: float
    confidence: float
    sample_size: int
    value: Any = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "score": _rounded(_clamp(self.score)),
            "weight": _rounded(max(0.0, self.weight)),
            "confidence": _rounded(_clamp(self.confidence)),
            "sample_size": self.sample_size,
            "value": self.value,
            "details": _payload_value(self.details),
        }


@dataclass(frozen=True, slots=True)
class SkillQualityScore:
    """Versioned advisory quality payload for a skill or bundle evidence slice."""

    score: float
    confidence: float
    sample_size_confidence: float
    evidence_count: int
    signals: Mapping[str, SkillQualitySignal]
    reasons: tuple[str, ...] = field(default_factory=tuple)
    rating: str = "insufficient_data"
    skill_name: str | None = None
    skill_effective_digest: str | None = None
    bundle_namespace: str | None = None
    bundle_name: str | None = None
    bundle_version: str | None = None
    bundle_digest: str | None = None
    task_class: str | None = None
    trust_level: str | None = None
    latest_observed_at: datetime | None = None
    oldest_observed_at: datetime | None = None
    advisory_only: bool = True
    schema_version: int = SKILL_QUALITY_SCORE_SCHEMA_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "advisory_only": self.advisory_only,
            "score": _rounded(_clamp(self.score)),
            "confidence": _rounded(_clamp(self.confidence)),
            "rating": self.rating,
            "skill": {
                "name": self.skill_name,
                "effective_digest": self.skill_effective_digest,
            },
            "bundle": {
                "namespace": self.bundle_namespace,
                "name": self.bundle_name,
                "version": self.bundle_version,
                "digest": self.bundle_digest,
            },
            "task_class": self.task_class,
            "trust_level": self.trust_level,
            "evidence": {
                "count": self.evidence_count,
                "sample_size_confidence": _rounded(_clamp(self.sample_size_confidence)),
                "oldest_observed_at": _isoformat(self.oldest_observed_at),
                "latest_observed_at": _isoformat(self.latest_observed_at),
            },
            "signals": {
                name: signal.to_payload()
                for name, signal in sorted(self.signals.items())
            },
            "reasons": list(dict.fromkeys(self.reasons)),
        }


def score_skill_quality(
    evidence_rows: Iterable[Any],
    *,
    skill_name: str | None = None,
    skill_effective_digest: str | None = None,
    bundle_namespace: str | None = None,
    bundle_name: str | None = None,
    bundle_version: str | None = None,
    bundle_digest: str | None = None,
    task_class: str | None = None,
    trust_level: str | None = None,
    as_of: datetime | None = None,
) -> SkillQualityScore:
    """Aggregate run evidence into a cautious advisory skill quality score.

    ``as_of`` is optional so tests and offline jobs can make recency scoring
    deterministic. When omitted, recency is computed relative to the latest
    evidence timestamp, keeping aggregation pure for a fixed set of rows.
    """
    rows = tuple(evidence_rows or ())
    evidence_count = len(rows)
    if evidence_count == 0:
        signals = _empty_signals(trust_level=trust_level)
        return SkillQualityScore(
            score=_BASELINE_SCORE,
            confidence=0.0,
            sample_size_confidence=0.0,
            evidence_count=0,
            signals=signals,
            reasons=("no skill run evidence is available",),
            rating="insufficient_data",
            skill_name=skill_name,
            skill_effective_digest=skill_effective_digest,
            bundle_namespace=bundle_namespace,
            bundle_name=bundle_name,
            bundle_version=bundle_version,
            bundle_digest=bundle_digest,
            task_class=_clean_text(task_class),
            trust_level=_clean_text(trust_level),
        )

    timestamps = [_datetime_value(_field(row, "created_at")) for row in rows]
    timestamps = [timestamp for timestamp in timestamps if timestamp is not None]
    latest_observed_at = max(timestamps) if timestamps else None
    oldest_observed_at = min(timestamps) if timestamps else None
    recency_as_of = _datetime_value(as_of) or latest_observed_at

    resolved_skill_name = _identity_text(skill_name) or _first_identity(rows, "skill_name")
    resolved_digest = _identity_text(skill_effective_digest) or _first_identity(
        rows, "skill_effective_digest"
    )
    resolved_bundle_namespace = _identity_text(bundle_namespace) or _first_identity(
        rows, "bundle_namespace"
    )
    resolved_bundle_name = _identity_text(bundle_name) or _first_identity(rows, "bundle_name")
    resolved_bundle_version = _identity_text(bundle_version) or _first_identity(
        rows, "bundle_version"
    )
    resolved_bundle_digest = _identity_text(bundle_digest) or _first_identity(
        rows, "bundle_digest"
    )
    resolved_trust_level = _clean_text(trust_level) or _first_clean_text(rows, "trust_level")
    resolved_task_class = _clean_text(task_class)

    sample_size_confidence = _sample_size_confidence(evidence_count)
    signals = {
        "outcome_success_rate": _outcome_signal(rows),
        "verifier_pass_rate": _verifier_signal(rows),
        "user_correction_feedback_rate": _feedback_signal(rows),
        "task_class_fit": _task_class_signal(rows, expected_task_class=resolved_task_class),
        "recency_reliability": _recency_signal(
            rows,
            as_of=recency_as_of,
            latest_observed_at=latest_observed_at,
            oldest_observed_at=oldest_observed_at,
        ),
        "cost_efficiency": _cost_efficiency_signal(rows),
        "trust_level": _trust_signal(resolved_trust_level),
    }

    raw_score = _weighted_score(signals.values())
    score = _BASELINE_SCORE + sample_size_confidence * (raw_score - _BASELINE_SCORE)
    signal_coverage = sum(signal.confidence for signal in signals.values()) / len(signals)
    confidence = sample_size_confidence * (0.4 + 0.6 * signal_coverage)
    reasons = _score_reasons(
        signals,
        evidence_count=evidence_count,
        sample_size_confidence=sample_size_confidence,
    )
    rating = _rating(score=score, confidence=confidence, evidence_count=evidence_count)

    return SkillQualityScore(
        score=score,
        confidence=confidence,
        sample_size_confidence=sample_size_confidence,
        evidence_count=evidence_count,
        signals=signals,
        reasons=tuple(reasons),
        rating=rating,
        skill_name=resolved_skill_name,
        skill_effective_digest=resolved_digest,
        bundle_namespace=resolved_bundle_namespace,
        bundle_name=resolved_bundle_name,
        bundle_version=resolved_bundle_version,
        bundle_digest=resolved_bundle_digest,
        task_class=resolved_task_class,
        trust_level=resolved_trust_level,
        latest_observed_at=latest_observed_at,
        oldest_observed_at=oldest_observed_at,
    )


def score_skill_quality_from_repository(
    repository: Any,
    *,
    skill_effective_digest: str | None = None,
    skill_name: str | None = None,
    limit: int = DEFAULT_EVIDENCE_LIMIT,
    task_class: str | None = None,
    trust_level: str | None = None,
    as_of: datetime | None = None,
) -> SkillQualityScore:
    """Score a skill slice using a repository with ``list_by_skill``.

    The adapter is read-only and advisory. It does not mutate evidence,
    skill projections, or routing state.
    """
    rows = repository.list_by_skill(
        skill_effective_digest=skill_effective_digest,
        skill_name=skill_name,
        limit=limit,
    )
    return score_skill_quality(
        rows,
        skill_name=skill_name,
        skill_effective_digest=skill_effective_digest,
        task_class=task_class,
        trust_level=trust_level,
        as_of=as_of,
    )


def _empty_signals(*, trust_level: str | None) -> dict[str, SkillQualitySignal]:
    return {
        "outcome_success_rate": SkillQualitySignal(
            "outcome_success_rate", _BASELINE_SCORE, 0.28, 0.0, 0
        ),
        "verifier_pass_rate": SkillQualitySignal(
            "verifier_pass_rate", _BASELINE_SCORE, 0.20, 0.0, 0
        ),
        "user_correction_feedback_rate": SkillQualitySignal(
            "user_correction_feedback_rate", _BASELINE_SCORE, 0.14, 0.0, 0
        ),
        "task_class_fit": SkillQualitySignal(
            "task_class_fit", _BASELINE_SCORE, 0.12, 0.0, 0
        ),
        "recency_reliability": SkillQualitySignal(
            "recency_reliability", _BASELINE_SCORE, 0.12, 0.0, 0
        ),
        "cost_efficiency": SkillQualitySignal(
            "cost_efficiency", _BASELINE_SCORE, 0.08, 0.0, 0
        ),
        "trust_level": _trust_signal(trust_level),
    }


def _outcome_signal(rows: Sequence[Any]) -> SkillQualitySignal:
    counts = Counter(_outcome_bucket(_field(row, "outcome_label")) for row in rows)
    known = counts["success"] + counts["partial"] + counts["failure"]
    if known == 0:
        return SkillQualitySignal(
            "outcome_success_rate",
            0.55,
            0.28,
            0.0,
            0,
            value=None,
            details={"counts": dict(counts), "known_rate": 0.0},
        )

    success_units = counts["success"] + (0.5 * counts["partial"])
    raw_success_rate = counts["success"] / known
    score = _bayesian_rate(success_units, known, prior=0.55, strength=8.0)
    return SkillQualitySignal(
        "outcome_success_rate",
        score,
        0.28,
        _sample_size_confidence(known),
        known,
        value=_rounded(raw_success_rate),
        details={
            "counts": {
                "success": counts["success"],
                "partial": counts["partial"],
                "failure": counts["failure"],
                "unknown": counts["unknown"],
            },
            "partial_credit_success_rate": _rounded(success_units / known),
            "known_rate": _rounded(known / len(rows)),
        },
    )


def _verifier_signal(rows: Sequence[Any]) -> SkillQualitySignal:
    counts = Counter(_verifier_bucket(_field(row, "verifier_status")) for row in rows)
    known = counts["passed"] + counts["failed"]
    if known == 0:
        return SkillQualitySignal(
            "verifier_pass_rate",
            0.55,
            0.20,
            0.0,
            0,
            value=None,
            details={"counts": dict(counts), "known_rate": 0.0},
        )

    pass_rate = counts["passed"] / known
    score = _bayesian_rate(counts["passed"], known, prior=0.55, strength=6.0)
    return SkillQualitySignal(
        "verifier_pass_rate",
        score,
        0.20,
        _sample_size_confidence(known),
        known,
        value=_rounded(pass_rate),
        details={
            "counts": {
                "passed": counts["passed"],
                "failed": counts["failed"],
                "unknown": counts["unknown"],
            },
            "known_rate": _rounded(known / len(rows)),
        },
    )


def _feedback_signal(rows: Sequence[Any]) -> SkillQualitySignal:
    counts = Counter(_feedback_bucket(_field(row, "user_feedback")) for row in rows)
    known = counts["positive"] + counts["negative"] + counts["correction"]
    if known == 0:
        return SkillQualitySignal(
            "user_correction_feedback_rate",
            0.58,
            0.14,
            0.0,
            0,
            value=None,
            details={"counts": dict(counts), "feedback_rate": 0.0},
        )

    corrective = counts["negative"] + counts["correction"]
    correction_rate = corrective / known
    positive_rate = counts["positive"] / known
    score = 1.0 - _bayesian_rate(corrective, known, prior=0.18, strength=8.0)
    return SkillQualitySignal(
        "user_correction_feedback_rate",
        score,
        0.14,
        _sample_size_confidence(known),
        known,
        value=_rounded(correction_rate),
        details={
            "counts": {
                "positive": counts["positive"],
                "negative": counts["negative"],
                "correction": counts["correction"],
                "unknown": counts["unknown"],
            },
            "positive_feedback_rate": _rounded(positive_rate),
            "feedback_rate": _rounded(known / len(rows)),
        },
    )


def _task_class_signal(
    rows: Sequence[Any],
    *,
    expected_task_class: str | None,
) -> SkillQualitySignal:
    task_classes = [_clean_text(_field(row, "task_class")) for row in rows]
    known_classes = [task_class for task_class in task_classes if task_class]
    if not known_classes:
        return SkillQualitySignal(
            "task_class_fit",
            0.55,
            0.12,
            0.0,
            0,
            value=None,
            details={"mode": "missing", "known_rate": 0.0, "distribution": {}},
        )

    distribution = Counter(known_classes)
    if expected_task_class:
        matched = distribution[_clean_text(expected_task_class)]
        fit_rate = matched / len(known_classes)
        mode = "expected_task_class"
        dominant_task_class = distribution.most_common(1)[0][0]
    else:
        dominant_task_class, matched = distribution.most_common(1)[0]
        fit_rate = matched / len(known_classes)
        mode = "observed_dominant_task_class"

    score = _bayesian_rate(matched, len(known_classes), prior=0.55, strength=8.0)
    return SkillQualitySignal(
        "task_class_fit",
        score,
        0.12,
        _sample_size_confidence(len(known_classes)),
        len(known_classes),
        value=_rounded(fit_rate),
        details={
            "mode": mode,
            "expected_task_class": expected_task_class,
            "dominant_task_class": dominant_task_class,
            "distribution": dict(sorted(distribution.items())),
            "known_rate": _rounded(len(known_classes) / len(rows)),
        },
    )


def _recency_signal(
    rows: Sequence[Any],
    *,
    as_of: datetime | None,
    latest_observed_at: datetime | None,
    oldest_observed_at: datetime | None,
) -> SkillQualitySignal:
    if latest_observed_at is None or as_of is None:
        return SkillQualitySignal(
            "recency_reliability",
            0.55,
            0.12,
            0.0,
            0,
            value=None,
            details={"latest_observed_at": None, "age_days": None},
        )

    latest = _as_aware_utc(latest_observed_at)
    reference = _as_aware_utc(as_of)
    age_days = max(0.0, (reference - latest).total_seconds() / 86_400)
    if age_days <= 7:
        recency_score = 1.0
    elif age_days <= 30:
        recency_score = 0.85
    elif age_days <= 90:
        recency_score = 0.62
    elif age_days <= 180:
        recency_score = 0.42
    else:
        recency_score = 0.25

    recent_count = 0
    for row in rows:
        created_at = _datetime_value(_field(row, "created_at"))
        if created_at is None:
            continue
        row_age_days = max(
            0.0,
            (reference - _as_aware_utc(created_at)).total_seconds() / 86_400,
        )
        if row_age_days <= 30:
            recent_count += 1

    reliability = 0.7 + (0.3 * min(1.0, recent_count / 10))
    score = recency_score * reliability
    return SkillQualitySignal(
        "recency_reliability",
        score,
        0.12,
        _sample_size_confidence(len(rows)),
        len(rows),
        value=_rounded(score),
        details={
            "oldest_observed_at": _isoformat(oldest_observed_at),
            "latest_observed_at": _isoformat(latest_observed_at),
            "age_days": _rounded(age_days),
            "recent_evidence_count_30d": recent_count,
        },
    )


def _cost_efficiency_signal(rows: Sequence[Any]) -> SkillQualitySignal:
    row_scores: list[float] = []
    costs: list[float] = []
    tokens: list[int] = []
    runtimes: list[int] = []

    for row in rows:
        parts: list[float] = []
        for field_name in ("cost_bucket", "token_bucket", "runtime_bucket"):
            bucket_score = _bucket_efficiency_score(_field(row, field_name))
            if bucket_score is not None:
                parts.append(bucket_score)

        cost = _float_value(_field(row, "cost_usd"))
        if cost is not None:
            costs.append(cost)
            parts.append(_numeric_cost_score(cost))

        total_tokens = _int_value(_field(row, "total_tokens"))
        if total_tokens is not None:
            tokens.append(total_tokens)
            parts.append(_token_score(total_tokens))

        runtime_ms = _int_value(_field(row, "runtime_ms"))
        if runtime_ms is not None:
            runtimes.append(runtime_ms)
            parts.append(_runtime_score(runtime_ms))

        if parts:
            row_scores.append(sum(parts) / len(parts))

    if not row_scores:
        return SkillQualitySignal(
            "cost_efficiency",
            0.60,
            0.08,
            0.0,
            0,
            value=None,
            details={"observed_cost_count": 0},
        )

    score = sum(row_scores) / len(row_scores)
    return SkillQualitySignal(
        "cost_efficiency",
        score,
        0.08,
        _sample_size_confidence(len(row_scores)),
        len(row_scores),
        value=_rounded(score),
        details={
            "observed_cost_count": len(row_scores),
            "average_cost_usd": _rounded(sum(costs) / len(costs)) if costs else None,
            "average_total_tokens": int(sum(tokens) / len(tokens)) if tokens else None,
            "average_runtime_ms": int(sum(runtimes) / len(runtimes)) if runtimes else None,
        },
    )


def _trust_signal(trust_level: str | None) -> SkillQualitySignal:
    normalized = _clean_text(trust_level)
    if not normalized:
        return SkillQualitySignal(
            "trust_level",
            _BASELINE_SCORE,
            0.06,
            0.0,
            0,
            value=None,
            details={"known": False},
        )
    score = _TRUST_LEVEL_SCORES.get(normalized, _BASELINE_SCORE)
    return SkillQualitySignal(
        "trust_level",
        score,
        0.06,
        1.0,
        1,
        value=normalized,
        details={"known": True},
    )


def _score_reasons(
    signals: Mapping[str, SkillQualitySignal],
    *,
    evidence_count: int,
    sample_size_confidence: float,
) -> list[str]:
    reasons: list[str] = []
    if evidence_count < 5:
        reasons.append("low sample size keeps the advisory score close to neutral")
    elif sample_size_confidence < 0.75:
        reasons.append("sample size is still building confidence")

    outcome = signals["outcome_success_rate"]
    outcome_value = outcome.value if isinstance(outcome.value, float) else None
    if outcome_value is not None and outcome.sample_size:
        if outcome_value >= 0.8:
            reasons.append("outcome evidence has a high success rate")
        elif outcome_value <= 0.35:
            reasons.append("outcome evidence has a low success rate")

    verifier = signals["verifier_pass_rate"]
    verifier_value = verifier.value if isinstance(verifier.value, float) else None
    if verifier_value is not None and verifier.sample_size:
        if verifier_value >= 0.8:
            reasons.append("verifier evidence usually passes")
        elif verifier_value <= 0.35:
            reasons.append("verifier evidence often fails")

    feedback = signals["user_correction_feedback_rate"]
    feedback_value = feedback.value if isinstance(feedback.value, float) else None
    if feedback_value is not None and feedback.sample_size and feedback_value >= 0.25:
        reasons.append("user feedback includes corrections or negative signals")

    task_fit = signals["task_class_fit"]
    task_fit_value = task_fit.value if isinstance(task_fit.value, float) else None
    if task_fit_value is not None and task_fit.sample_size and task_fit_value < 0.5:
        reasons.append("task-class evidence is mixed for the requested slice")

    recency = signals["recency_reliability"]
    age_days = recency.details.get("age_days") if isinstance(recency.details, Mapping) else None
    if isinstance(age_days, float) and age_days > 90:
        reasons.append("evidence is stale relative to the scoring window")

    cost = signals["cost_efficiency"]
    if cost.sample_size and cost.score < 0.45:
        reasons.append("cost or runtime evidence is inefficient")

    trust = signals["trust_level"]
    if trust.value == "agent_draft":
        reasons.append("agent-draft trust level limits quality confidence")

    return reasons or ["evidence is balanced with no dominant advisory signal"]


def _rating(*, score: float, confidence: float, evidence_count: int) -> str:
    if evidence_count == 0:
        return "insufficient_data"
    if confidence < 0.35:
        return "learning"
    if score >= 0.75 and confidence >= 0.65:
        return "strong"
    if score >= 0.60:
        return "promising"
    if score <= 0.35 and confidence >= 0.65:
        return "risky"
    if score <= 0.45:
        return "watch"
    return "mixed"


def _weighted_score(signals: Iterable[SkillQualitySignal]) -> float:
    total_weight = 0.0
    weighted = 0.0
    for signal in signals:
        weight = max(0.0, signal.weight)
        total_weight += weight
        weighted += _clamp(signal.score) * weight
    if total_weight <= 0:
        return _BASELINE_SCORE
    return _clamp(weighted / total_weight)


def _outcome_bucket(value: Any) -> str:
    text = _clean_text(value)
    if text in _SUCCESS_OUTCOMES:
        return "success"
    if text in _PARTIAL_OUTCOMES:
        return "partial"
    if text in _FAILURE_OUTCOMES:
        return "failure"
    return "unknown"


def _verifier_bucket(value: Any) -> str:
    text = _clean_text(value)
    if text in _PASS_VERIFIERS:
        return "passed"
    if text in _FAIL_VERIFIERS:
        return "failed"
    return "unknown"


def _feedback_bucket(value: Any) -> str:
    text = _clean_text(value)
    if not text or text in {"missing", "none", "unknown"}:
        return "unknown"
    if text in _CORRECTION_FEEDBACK or any(marker in text for marker in _CORRECTION_FEEDBACK):
        return "correction"
    if text in _NEGATIVE_FEEDBACK or any(marker in text for marker in _NEGATIVE_FEEDBACK):
        return "negative"
    if text in _POSITIVE_FEEDBACK or any(marker in text for marker in _POSITIVE_FEEDBACK):
        return "positive"
    return "unknown"


def _bucket_efficiency_score(value: Any) -> float | None:
    text = _clean_text(value)
    if not text or text in {"missing", "none", "unknown"}:
        return None
    if text in {"tiny", "very_low", "low", "small", "fast"}:
        return 1.0
    if text in {"medium", "normal", "moderate"}:
        return 0.75
    if text in {"large", "high", "slow"}:
        return 0.35
    if text in {"very_high", "xlarge", "huge", "very_slow"}:
        return 0.2
    return None


def _numeric_cost_score(cost_usd: float) -> float:
    if cost_usd <= 0.10:
        return 1.0
    if cost_usd <= 0.50:
        return 0.85
    if cost_usd <= 2.00:
        return 0.65
    if cost_usd <= 10.00:
        return 0.40
    return 0.20


def _token_score(total_tokens: int) -> float:
    if total_tokens <= 8_000:
        return 1.0
    if total_tokens <= 32_000:
        return 0.85
    if total_tokens <= 100_000:
        return 0.60
    if total_tokens <= 200_000:
        return 0.40
    return 0.20


def _runtime_score(runtime_ms: int) -> float:
    if runtime_ms <= 5 * 60 * 1000:
        return 1.0
    if runtime_ms <= 15 * 60 * 1000:
        return 0.75
    if runtime_ms <= 30 * 60 * 1000:
        return 0.55
    return 0.30


def _sample_size_confidence(sample_size: int) -> float:
    if sample_size <= 0:
        return 0.0
    return _clamp((sample_size / _FULL_CONFIDENCE_SAMPLE_SIZE) ** 0.5)


def _bayesian_rate(success_units: float, total: int, *, prior: float, strength: float) -> float:
    if total <= 0:
        return _clamp(prior)
    return _clamp((success_units + (prior * strength)) / (total + strength))


def _first_identity(rows: Sequence[Any], field_name: str) -> str | None:
    for row in rows:
        value = _identity_text(_field(row, field_name))
        if value:
            return value
    return None


def _first_clean_text(rows: Sequence[Any], field_name: str) -> str | None:
    for row in rows:
        value = _clean_text(_field(row, field_name))
        if value:
            return value
    return None


def _field(row: Any, field_name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(field_name)
    return getattr(row, field_name, None)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text.lower() if text else None


def _identity_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _datetime_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _float_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_value(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _payload_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _isoformat(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {key: _payload_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_payload_value(item) for item in value]
    return value


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_aware_utc(value).isoformat()


def _rounded(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    return value


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


__all__ = [
    "DEFAULT_EVIDENCE_LIMIT",
    "SKILL_QUALITY_SCORE_SCHEMA_VERSION",
    "SkillQualityScore",
    "SkillQualitySignal",
    "score_skill_quality",
    "score_skill_quality_from_repository",
]
