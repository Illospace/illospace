"""Pure read models for the Cortex learning observatory.

The observatory is intentionally read-only and deterministic. Callers pass
already-loaded rows, score payloads, or planner payloads; this module only
normalizes and summarizes them for admin inspection.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
import json
from typing import Any


LEARNING_OBSERVATORY_SCHEMA_VERSION = 1

CONTROL_PAUSE_LEARNING = "pause_learning"
CONTROL_ROLLBACK_POLICY_UPDATE = "rollback_policy_update"
CONTROL_APPROVE_SKILL_GRADUATION = "approve_skill_graduation"
CONTROL_EXPORT_REDACTED_EVAL_ARTIFACT = "export_redacted_eval_artifact"

LEARNING_OBSERVATORY_CONTROL_KEYS = (
    CONTROL_PAUSE_LEARNING,
    CONTROL_ROLLBACK_POLICY_UPDATE,
    CONTROL_APPROVE_SKILL_GRADUATION,
    CONTROL_EXPORT_REDACTED_EVAL_ARTIFACT,
)

_SUCCESS_LABELS = frozenset({
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
})
_PARTIAL_LABELS = frozenset({
    "blocked",
    "partial",
    "partially_successful",
    "settled_partial",
    "uncertain",
    "weak",
})
_FAILURE_LABELS = frozenset({
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
    "timeout",
    "timed_out",
    "unsatisfied",
})
_PENDING_REVIEW_STATUSES = frozenset({
    "needs_review",
    "pending",
    "proposed",
    "recommended",
    "shadow",
    "unreviewed",
})
_ROLLBACKABLE_POLICY_STATUSES = frozenset({
    "active",
    "applied",
    "recommended",
    "shadow",
})


@dataclass(frozen=True, slots=True)
class CountBucket:
    key: str
    count: int
    share: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "count": self.count,
            "share": self.share,
        }


@dataclass(frozen=True, slots=True)
class RecentOutcomeLabel:
    source_ref: str | None
    label: str
    confidence: float | None = None
    created_at: str | None = None
    signal_type: str | None = None
    status: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return _drop_none({
            "source_ref": self.source_ref,
            "label": self.label,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "signal_type": self.signal_type,
            "status": self.status,
        })


@dataclass(frozen=True, slots=True)
class OutcomeLabelReadModel:
    total_count: int = 0
    by_label: tuple[CountBucket, ...] = ()
    average_confidence: float | None = None
    recent: tuple[RecentOutcomeLabel, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "total_count": self.total_count,
            "by_label": [bucket.to_payload() for bucket in self.by_label],
            "average_confidence": self.average_confidence,
            "recent": [item.to_payload() for item in self.recent],
        }


@dataclass(frozen=True, slots=True)
class SkillQualityObservation:
    skill_name: str
    skill_effective_digest: str | None
    score: float | None
    confidence: float | None
    rating: str | None
    evidence_count: int
    observed_at: str | None = None


@dataclass(frozen=True, slots=True)
class SkillQualityTrend:
    skill_name: str
    skill_effective_digest: str | None
    current_score: float | None
    previous_score: float | None
    delta: float | None
    direction: str
    confidence: float | None
    rating: str | None
    evidence_count: int
    observation_count: int
    latest_observed_at: str | None = None

    @property
    def graduation_candidate(self) -> bool:
        return (
            self.current_score is not None
            and self.current_score >= 0.8
            and (self.confidence or 0.0) >= 0.5
            and (self.rating or "") not in {"weak", "insufficient_data"}
        )

    def to_payload(self) -> dict[str, Any]:
        return _drop_none({
            "skill_name": self.skill_name,
            "skill_effective_digest": self.skill_effective_digest,
            "current_score": self.current_score,
            "previous_score": self.previous_score,
            "delta": self.delta,
            "direction": self.direction,
            "confidence": self.confidence,
            "rating": self.rating,
            "evidence_count": self.evidence_count,
            "observation_count": self.observation_count,
            "latest_observed_at": self.latest_observed_at,
            "graduation_candidate": self.graduation_candidate,
        })


@dataclass(frozen=True, slots=True)
class SkillQualityReadModel:
    skill_count: int = 0
    average_current_score: float | None = None
    improving_count: int = 0
    declining_count: int = 0
    weak_count: int = 0
    graduation_candidate_count: int = 0
    trends: tuple[SkillQualityTrend, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "skill_count": self.skill_count,
            "average_current_score": self.average_current_score,
            "improving_count": self.improving_count,
            "declining_count": self.declining_count,
            "weak_count": self.weak_count,
            "graduation_candidate_count": self.graduation_candidate_count,
            "trends": [trend.to_payload() for trend in self.trends],
        }


@dataclass(frozen=True, slots=True)
class ContextUsefulnessPoint:
    source_ref: str | None
    created_at: str | None
    label_count: int
    useful_count: int
    unused_count: int
    missed_count: int
    over_budget_count: int
    usefulness_rate: float | None
    estimated_tokens: int = 0
    budget_tokens: int = 0
    cognitive_miss_count: int = 0

    def to_payload(self) -> dict[str, Any]:
        return _drop_none({
            "source_ref": self.source_ref,
            "created_at": self.created_at,
            "label_count": self.label_count,
            "useful_count": self.useful_count,
            "unused_count": self.unused_count,
            "missed_count": self.missed_count,
            "over_budget_count": self.over_budget_count,
            "usefulness_rate": self.usefulness_rate,
            "estimated_tokens": self.estimated_tokens,
            "budget_tokens": self.budget_tokens,
            "cognitive_miss_count": self.cognitive_miss_count,
        })


@dataclass(frozen=True, slots=True)
class ContextUsefulnessReadModel:
    total_label_count: int = 0
    by_label: tuple[CountBucket, ...] = ()
    usefulness_rate: float | None = None
    estimated_tokens: int = 0
    budget_tokens: int = 0
    over_budget_count: int = 0
    cognitive_miss_count: int = 0
    trend_points: tuple[ContextUsefulnessPoint, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "total_label_count": self.total_label_count,
            "by_label": [bucket.to_payload() for bucket in self.by_label],
            "usefulness_rate": self.usefulness_rate,
            "estimated_tokens": self.estimated_tokens,
            "budget_tokens": self.budget_tokens,
            "over_budget_count": self.over_budget_count,
            "cognitive_miss_count": self.cognitive_miss_count,
            "trend_points": [point.to_payload() for point in self.trend_points],
        }


@dataclass(frozen=True, slots=True)
class StaleConflictReadModel:
    stale_count: int = 0
    conflict_count: int = 0
    resolved_count: int = 0
    pending_count: int = 0
    by_status: tuple[CountBucket, ...] = ()
    recent: tuple[dict[str, Any], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "stale_count": self.stale_count,
            "conflict_count": self.conflict_count,
            "resolved_count": self.resolved_count,
            "pending_count": self.pending_count,
            "by_status": [bucket.to_payload() for bucket in self.by_status],
            "recent": [dict(item) for item in self.recent],
        }


@dataclass(frozen=True, slots=True)
class NightBudgetReadModel:
    budget_tokens: int = 0
    spent_tokens: int = 0
    remaining_tokens: int = 0
    utilization: float = 0.0
    allowed_count: int = 0
    deferred_count: int = 0
    skipped_count: int = 0
    by_work_type: tuple[CountBucket, ...] = ()
    by_tenant: tuple[CountBucket, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "budget_tokens": self.budget_tokens,
            "spent_tokens": self.spent_tokens,
            "remaining_tokens": self.remaining_tokens,
            "utilization": self.utilization,
            "allowed_count": self.allowed_count,
            "deferred_count": self.deferred_count,
            "skipped_count": self.skipped_count,
            "by_work_type": [bucket.to_payload() for bucket in self.by_work_type],
            "by_tenant": [bucket.to_payload() for bucket in self.by_tenant],
        }


@dataclass(frozen=True, slots=True)
class PolicyCandidateItem:
    candidate_ref: str | None
    candidate_type: str
    status: str
    review_status: str | None
    policy_key: str | None = None
    version: int | None = None
    created_at: str | None = None
    applied_at: str | None = None
    rolled_back_at: str | None = None

    @property
    def pending_review(self) -> bool:
        return (
            self.review_status in _PENDING_REVIEW_STATUSES
            or self.status in _PENDING_REVIEW_STATUSES
        )

    @property
    def rollbackable(self) -> bool:
        return (
            self.rolled_back_at is None
            and (
                self.applied_at is not None
                or self.status in _ROLLBACKABLE_POLICY_STATUSES
            )
        )

    def to_payload(self) -> dict[str, Any]:
        return _drop_none({
            "candidate_ref": self.candidate_ref,
            "candidate_type": self.candidate_type,
            "status": self.status,
            "review_status": self.review_status,
            "policy_key": self.policy_key,
            "version": self.version,
            "created_at": self.created_at,
            "applied_at": self.applied_at,
            "rolled_back_at": self.rolled_back_at,
            "pending_review": self.pending_review,
            "rollbackable": self.rollbackable,
        })


@dataclass(frozen=True, slots=True)
class PolicyCandidateReadModel:
    total_count: int = 0
    pending_review_count: int = 0
    rollbackable_count: int = 0
    by_status: tuple[CountBucket, ...] = ()
    by_type: tuple[CountBucket, ...] = ()
    by_review_status: tuple[CountBucket, ...] = ()
    recent: tuple[PolicyCandidateItem, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "total_count": self.total_count,
            "pending_review_count": self.pending_review_count,
            "rollbackable_count": self.rollbackable_count,
            "by_status": [bucket.to_payload() for bucket in self.by_status],
            "by_type": [bucket.to_payload() for bucket in self.by_type],
            "by_review_status": [bucket.to_payload() for bucket in self.by_review_status],
            "recent": [item.to_payload() for item in self.recent],
        }


@dataclass(frozen=True, slots=True)
class LearningControlCapability:
    key: str
    label: str
    available: bool
    reason: str
    target_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "available": self.available,
            "reason": self.reason,
            "target_count": self.target_count,
            "read_only_metadata": True,
            "mutation_endpoint": None,
            "metadata": _payload_value(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class LearningObservatoryReadModel:
    outcomes: OutcomeLabelReadModel
    skill_quality: SkillQualityReadModel
    context_usefulness: ContextUsefulnessReadModel
    stale_conflicts: StaleConflictReadModel
    night_budget: NightBudgetReadModel
    policy_candidates: PolicyCandidateReadModel
    controls: tuple[LearningControlCapability, ...]
    generated_at: str | None = None
    scope: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": LEARNING_OBSERVATORY_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "scope": _payload_value(self.scope),
            "summary": {
                "outcome_label_count": self.outcomes.total_count,
                "skill_count": self.skill_quality.skill_count,
                "context_label_count": self.context_usefulness.total_label_count,
                "stale_count": self.stale_conflicts.stale_count,
                "conflict_count": self.stale_conflicts.conflict_count,
                "night_budget_utilization": self.night_budget.utilization,
                "policy_candidate_count": self.policy_candidates.total_count,
            },
            "observatory": {
                "recent_outcomes": self.outcomes.to_payload(),
                "skill_quality": self.skill_quality.to_payload(),
                "context_usefulness": self.context_usefulness.to_payload(),
                "stale_conflicts": self.stale_conflicts.to_payload(),
                "night_budget": self.night_budget.to_payload(),
                "policy_candidates": self.policy_candidates.to_payload(),
            },
            "controls": [control.to_payload() for control in self.controls],
            "warnings": list(self.warnings),
        }


def build_learning_observatory_read_model(
    *,
    outcome_sources: Iterable[Any] = (),
    skill_quality_sources: Iterable[Any] = (),
    context_sources: Iterable[Any] = (),
    stale_conflict_sources: Iterable[Any] = (),
    night_budget_source: Any | None = None,
    policy_sources: Iterable[Any] = (),
    generated_at: datetime | date | str | None = None,
    scope: Mapping[str, Any] | None = None,
    warnings: Iterable[str] = (),
    recent_limit: int = 10,
) -> LearningObservatoryReadModel:
    """Build the top-level observatory from already-loaded read sources."""
    outcomes = summarize_outcome_labels(outcome_sources, limit=recent_limit)
    skill_quality = summarize_skill_quality_trends(skill_quality_sources, limit=recent_limit)
    context_usefulness = summarize_context_usefulness_trends(context_sources, limit=recent_limit)
    stale_conflicts = summarize_stale_conflict_resolution(stale_conflict_sources, limit=recent_limit)
    night_budget = summarize_night_budget_usage(night_budget_source)
    policy_candidates = summarize_policy_candidate_status(policy_sources, limit=recent_limit)
    controls = default_learning_observatory_controls(
        policy_candidates=policy_candidates,
        skill_quality=skill_quality,
        exportable_eval_count=outcomes.total_count,
    )
    return LearningObservatoryReadModel(
        outcomes=outcomes,
        skill_quality=skill_quality,
        context_usefulness=context_usefulness,
        stale_conflicts=stale_conflicts,
        night_budget=night_budget,
        policy_candidates=policy_candidates,
        controls=controls,
        generated_at=_iso(generated_at),
        scope=dict(scope or {}),
        warnings=tuple(str(item) for item in warnings if str(item or "").strip()),
    )


def summarize_outcome_labels(
    sources: Iterable[Any],
    *,
    limit: int = 10,
) -> OutcomeLabelReadModel:
    rows: list[RecentOutcomeLabel] = []
    confidences: list[float] = []
    counts: Counter[str] = Counter()
    for source in sources or ():
        label, confidence = _extract_outcome_label(source)
        if not label:
            continue
        counts[label] += 1
        if confidence is not None:
            confidences.append(confidence)
        rows.append(RecentOutcomeLabel(
            source_ref=_source_ref(source),
            label=label,
            confidence=confidence,
            created_at=_created_at(source),
            signal_type=_clean_text(_field(source, "signal_type")),
            status=_clean_text(_field(source, "status")),
        ))

    rows.sort(key=lambda item: (_sort_time(item.created_at), item.source_ref or ""), reverse=True)
    average = _rounded(sum(confidences) / len(confidences)) if confidences else None
    return OutcomeLabelReadModel(
        total_count=sum(counts.values()),
        by_label=_count_buckets(counts),
        average_confidence=average,
        recent=tuple(rows[:max(0, int(limit))]),
    )


def summarize_skill_quality_trends(
    sources: Iterable[Any],
    *,
    limit: int = 10,
) -> SkillQualityReadModel:
    observations_by_skill: dict[tuple[str, str], list[SkillQualityObservation]] = defaultdict(list)
    for source in sources or ():
        observation = _skill_quality_observation(source)
        if observation is None:
            continue
        key = (observation.skill_name, observation.skill_effective_digest or "")
        observations_by_skill[key].append(observation)

    trends: list[SkillQualityTrend] = []
    for observations in observations_by_skill.values():
        observations.sort(key=lambda item: (_sort_time(item.observed_at), item.skill_name))
        latest = observations[-1]
        previous = observations[-2] if len(observations) > 1 else None
        delta = None
        direction = "unknown"
        if latest.score is not None and previous and previous.score is not None:
            delta = _rounded(latest.score - previous.score)
            if delta > 0.015:
                direction = "improving"
            elif delta < -0.015:
                direction = "declining"
            else:
                direction = "stable"
        elif latest.score is not None:
            direction = "stable"
        trends.append(SkillQualityTrend(
            skill_name=latest.skill_name,
            skill_effective_digest=latest.skill_effective_digest,
            current_score=latest.score,
            previous_score=previous.score if previous else None,
            delta=delta,
            direction=direction,
            confidence=latest.confidence,
            rating=latest.rating,
            evidence_count=latest.evidence_count,
            observation_count=len(observations),
            latest_observed_at=latest.observed_at,
        ))

    trends.sort(key=lambda item: (
        1.0 if item.current_score is None else item.current_score,
        item.skill_name,
        item.skill_effective_digest or "",
    ))
    current_scores = [trend.current_score for trend in trends if trend.current_score is not None]
    average = _rounded(sum(current_scores) / len(current_scores)) if current_scores else None
    return SkillQualityReadModel(
        skill_count=len(trends),
        average_current_score=average,
        improving_count=sum(1 for trend in trends if trend.direction == "improving"),
        declining_count=sum(1 for trend in trends if trend.direction == "declining"),
        weak_count=sum(1 for trend in trends if _is_weak_skill_trend(trend)),
        graduation_candidate_count=sum(1 for trend in trends if trend.graduation_candidate),
        trends=tuple(trends[:max(0, int(limit))]),
    )


def summarize_context_usefulness_trends(
    sources: Iterable[Any],
    *,
    limit: int = 10,
) -> ContextUsefulnessReadModel:
    points: list[ContextUsefulnessPoint] = []
    total_counts: Counter[str] = Counter()
    estimated_tokens = 0
    budget_tokens = 0
    cognitive_miss_count = 0
    for source in sources or ():
        payload = _context_payload(source)
        if not payload:
            continue
        labels = [
            item for item in payload.get("labels") or ()
            if isinstance(item, Mapping)
        ]
        summary = _mapping(payload.get("summary"))
        counts = Counter({str(key): int(value or 0) for key, value in _mapping(summary.get("counts")).items()})
        if not counts:
            counts.update(str(item.get("label") or "unknown") for item in labels)
        token_totals = _context_token_totals(labels)
        point_label_count = sum(counts.values())
        point = ContextUsefulnessPoint(
            source_ref=_source_ref(source) or _source_ref(payload),
            created_at=_created_at(source) or _created_at(payload),
            label_count=point_label_count,
            useful_count=counts.get("useful", 0),
            unused_count=counts.get("unused", 0),
            missed_count=counts.get("missed", 0),
            over_budget_count=counts.get("over_budget", 0),
            usefulness_rate=_usefulness_rate(counts),
            estimated_tokens=token_totals["estimated_tokens"],
            budget_tokens=token_totals["budget_tokens"],
            cognitive_miss_count=_int(_mapping(payload.get("context")).get("cognitive_miss_count"))
            or _int(summary.get("cognitive_miss_count"))
            or 0,
        )
        points.append(point)
        total_counts.update(counts)
        estimated_tokens += point.estimated_tokens
        budget_tokens += point.budget_tokens
        cognitive_miss_count += point.cognitive_miss_count

    points.sort(key=lambda item: (_sort_time(item.created_at), item.source_ref or ""), reverse=True)
    return ContextUsefulnessReadModel(
        total_label_count=sum(total_counts.values()),
        by_label=_count_buckets(total_counts),
        usefulness_rate=_usefulness_rate(total_counts),
        estimated_tokens=estimated_tokens,
        budget_tokens=budget_tokens,
        over_budget_count=total_counts.get("over_budget", 0),
        cognitive_miss_count=cognitive_miss_count,
        trend_points=tuple(points[:max(0, int(limit))]),
    )


def summarize_stale_conflict_resolution(
    sources: Iterable[Any],
    *,
    limit: int = 10,
) -> StaleConflictReadModel:
    stale_count = 0
    conflict_count = 0
    resolved_count = 0
    pending_count = 0
    status_counts: Counter[str] = Counter()
    recent: list[dict[str, Any]] = []

    for source in sources or ():
        explicit = _explicit_stale_conflict_counts(source)
        status = _status(source)
        if explicit is not None:
            stale_count += explicit["stale_count"]
            conflict_count += explicit["conflict_count"]
            resolved_count += explicit["resolved_count"]
            pending_count += explicit["pending_count"]
        else:
            stale = _looks_stale(source)
            conflict = _looks_conflicted(source)
            resolved = _looks_resolved(source)
            pending = (stale or conflict) and not resolved
            stale_count += int(stale)
            conflict_count += int(conflict)
            resolved_count += int(resolved and (stale or conflict))
            pending_count += int(pending)
        status_counts[status] += 1
        recent.append(_drop_none({
            "source_ref": _source_ref(source),
            "status": status,
            "truth_status": _clean_text(_field(source, "truth_status")),
            "created_at": _created_at(source),
            "stale": _looks_stale(source),
            "conflict": _looks_conflicted(source),
            "resolved": _looks_resolved(source),
        }))

    recent.sort(key=lambda item: (_sort_time(item.get("created_at")), item.get("source_ref") or ""), reverse=True)
    return StaleConflictReadModel(
        stale_count=stale_count,
        conflict_count=conflict_count,
        resolved_count=resolved_count,
        pending_count=pending_count,
        by_status=_count_buckets(status_counts),
        recent=tuple(recent[:max(0, int(limit))]),
    )


def summarize_night_budget_usage(source: Any | None) -> NightBudgetReadModel:
    payload = _payload_mapping(source)
    if not payload:
        return NightBudgetReadModel()

    items = [item for item in payload.get("items") or () if isinstance(item, Mapping)]
    budget_tokens = _int(payload.get("budget_tokens")) or 0
    spent_tokens = _int(payload.get("spent_tokens"))
    if spent_tokens is None:
        spent_tokens = _sum_allowed_item_tokens(items)
    remaining_tokens = _int(payload.get("remaining_tokens"))
    if remaining_tokens is None:
        remaining_tokens = max(0, budget_tokens - spent_tokens)
    allowed_count = _int(payload.get("allowed_count"))
    deferred_count = _int(payload.get("deferred_count"))
    skipped_count = _int(payload.get("skipped_count"))
    item_actions = Counter(_item_decision_action(item) for item in items)
    if allowed_count is None:
        allowed_count = item_actions.get("allow", 0)
    if deferred_count is None:
        deferred_count = item_actions.get("defer", 0)
    if skipped_count is None:
        skipped_count = item_actions.get("skip", 0)

    by_work_type = _counter_from_mapping(payload.get("spent_by_work_type"))
    by_tenant = _counter_from_mapping(payload.get("spent_by_tenant"))
    if not by_work_type:
        for item in items:
            if _item_decision_action(item) != "allow":
                continue
            work_type = _clean_text(_mapping(_mapping(item).get("candidate")).get("work_type")) or "unknown"
            by_work_type[work_type] += _item_estimated_tokens(item)
    if not by_tenant:
        for item in items:
            if _item_decision_action(item) != "allow":
                continue
            by_tenant[_clean_text(_mapping(item).get("tenant_key")) or "unknown"] += _item_estimated_tokens(item)

    return NightBudgetReadModel(
        budget_tokens=budget_tokens,
        spent_tokens=spent_tokens,
        remaining_tokens=remaining_tokens,
        utilization=_rounded(spent_tokens / budget_tokens) if budget_tokens > 0 else 0.0,
        allowed_count=allowed_count or 0,
        deferred_count=deferred_count or 0,
        skipped_count=skipped_count or 0,
        by_work_type=_count_buckets(by_work_type, total=sum(by_work_type.values()) or None),
        by_tenant=_count_buckets(by_tenant, total=sum(by_tenant.values()) or None),
    )


def summarize_policy_candidate_status(
    sources: Iterable[Any],
    *,
    limit: int = 10,
) -> PolicyCandidateReadModel:
    items: list[PolicyCandidateItem] = []
    for source in sources or ():
        item = _policy_candidate_item(source)
        if item is not None:
            items.append(item)

    by_status = Counter(item.status for item in items)
    by_type = Counter(item.candidate_type for item in items)
    by_review = Counter(item.review_status or "unknown" for item in items)
    items.sort(key=lambda item: (_sort_time(item.created_at), item.candidate_ref or ""), reverse=True)
    return PolicyCandidateReadModel(
        total_count=len(items),
        pending_review_count=sum(1 for item in items if item.pending_review),
        rollbackable_count=sum(1 for item in items if item.rollbackable),
        by_status=_count_buckets(by_status),
        by_type=_count_buckets(by_type),
        by_review_status=_count_buckets(by_review),
        recent=tuple(items[:max(0, int(limit))]),
    )


def default_learning_observatory_controls(
    *,
    policy_candidates: PolicyCandidateReadModel,
    skill_quality: SkillQualityReadModel,
    exportable_eval_count: int,
) -> tuple[LearningControlCapability, ...]:
    """Return capability metadata only; these are not mutating endpoints."""
    return (
        LearningControlCapability(
            key=CONTROL_PAUSE_LEARNING,
            label="Pause learning",
            available=True,
            reason="Admins can pause learning from the control plane.",
            target_count=1,
            metadata={"scope": "learning_system"},
        ),
        LearningControlCapability(
            key=CONTROL_ROLLBACK_POLICY_UPDATE,
            label="Rollback policy update",
            available=policy_candidates.rollbackable_count > 0,
            reason="Rollbackable policy candidates are present."
            if policy_candidates.rollbackable_count > 0
            else "No active or applied policy candidates are visible.",
            target_count=policy_candidates.rollbackable_count,
            metadata={"candidate_statuses": ["active", "applied", "recommended", "shadow"]},
        ),
        LearningControlCapability(
            key=CONTROL_APPROVE_SKILL_GRADUATION,
            label="Approve skill graduation",
            available=skill_quality.graduation_candidate_count > 0,
            reason="High-confidence skill quality candidates are present."
            if skill_quality.graduation_candidate_count > 0
            else "No skill quality trend meets the graduation threshold.",
            target_count=skill_quality.graduation_candidate_count,
            metadata={"min_score": 0.8, "min_confidence": 0.5},
        ),
        LearningControlCapability(
            key=CONTROL_EXPORT_REDACTED_EVAL_ARTIFACT,
            label="Export redacted eval artifact",
            available=exportable_eval_count > 0,
            reason="Redacted eval artifacts can be exported from visible outcome evidence."
            if exportable_eval_count > 0
            else "No outcome evidence is visible for redacted eval export.",
            target_count=max(0, int(exportable_eval_count or 0)),
            metadata={"default_mode": "community", "redaction": "strict"},
        ),
    )


def _extract_outcome_label(source: Any) -> tuple[str | None, float | None]:
    direct = _field(source, "outcome_label")
    label, confidence = _label_from_value(direct)
    if label:
        confidence = confidence if confidence is not None else _float(_field(source, "label_confidence"))
        return label, confidence

    for mapping in (
        _mapping(_field(source, "quality")),
        _mapping(_field(source, "learning_signals")),
        _mapping(_field(source, "payload")),
        _mapping(_mapping(_field(source, "payload")).get("quality")),
        _mapping(_mapping(_field(source, "payload")).get("learning_signals")),
    ):
        label, confidence = _label_from_value(mapping.get("outcome_label"))
        if label:
            return label, confidence
    return None, None


def _label_from_value(value: Any) -> tuple[str | None, float | None]:
    if isinstance(value, Mapping):
        label = (
            _clean_text(value.get("outcome_class"))
            or _clean_text(value.get("label"))
            or _clean_text(value.get("outcome"))
        )
        return _normalized_key(label), _float(value.get("label_confidence") or value.get("confidence"))
    label = _normalized_key(value)
    return label, None


def _skill_quality_observation(source: Any) -> SkillQualityObservation | None:
    payload = _payload_mapping(source)
    if not payload:
        payload = {
            "skill_name": _field(source, "skill_name"),
            "skill_effective_digest": _field(source, "skill_effective_digest"),
            "outcome_label": _field(source, "outcome_label"),
            "confidence": _field(source, "label_confidence"),
            "created_at": _field(source, "created_at"),
        }

    skill = _mapping(payload.get("skill"))
    evidence = _mapping(payload.get("evidence"))
    skill_name = (
        _clean_text(skill.get("name"))
        or _clean_text(payload.get("skill_name"))
        or _clean_text(_field(source, "skill_name"))
        or "unknown"
    )
    digest = (
        _clean_text(skill.get("effective_digest"))
        or _clean_text(payload.get("skill_effective_digest"))
        or _clean_text(_field(source, "skill_effective_digest"))
    )
    score = _float(payload.get("score"))
    if score is None:
        label, _confidence = _extract_outcome_label(payload)
        score = _score_from_outcome_label(label)
    if score is None and not skill_name:
        return None
    confidence = _float(payload.get("confidence"))
    if confidence is None:
        confidence = _float(_field(source, "label_confidence"))
    return SkillQualityObservation(
        skill_name=skill_name,
        skill_effective_digest=digest,
        score=_rounded(_clamp(score)) if score is not None else None,
        confidence=_rounded(_clamp(confidence)) if confidence is not None else None,
        rating=_clean_text(payload.get("rating")),
        evidence_count=_int(evidence.get("count")) or _int(payload.get("evidence_count")) or 1,
        observed_at=(
            _iso(evidence.get("latest_observed_at"))
            or _iso(payload.get("latest_observed_at"))
            or _created_at(source)
            or _created_at(payload)
        ),
    )


def _context_payload(source: Any) -> dict[str, Any]:
    for payload in (_payload_mapping(source), _mapping(_field(source, "payload"))):
        if payload.get("labels") or payload.get("summary"):
            return payload
        nested = _mapping(payload.get("context"))
        if nested.get("labels") or nested.get("summary"):
            return nested
    return {}


def _context_token_totals(labels: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    estimated = 0
    budget = 0
    for label in labels:
        evidence = _mapping(label.get("evidence"))
        estimated += _int(evidence.get("estimated_tokens")) or 0
        budget += _int(evidence.get("budget_tokens")) or 0
    return {"estimated_tokens": estimated, "budget_tokens": budget}


def _usefulness_rate(counts: Counter[str]) -> float | None:
    denominator = sum(counts.get(key, 0) for key in ("useful", "unused", "missed", "over_budget"))
    if denominator <= 0:
        return None
    return _rounded(counts.get("useful", 0) / denominator)


def _explicit_stale_conflict_counts(source: Any) -> dict[str, int] | None:
    keys = {
        "stale_count": ("stale_count", "stale_total"),
        "conflict_count": ("conflict_count", "conflict_total"),
        "resolved_count": ("resolved_count", "resolution_count"),
        "pending_count": ("pending_count", "unresolved_count"),
    }
    values: dict[str, int] = {}
    found = False
    for target, aliases in keys.items():
        for alias in aliases:
            value = _int(_field(source, alias))
            if value is not None:
                values[target] = value
                found = True
                break
        values.setdefault(target, 0)
    return values if found else None


def _looks_stale(source: Any) -> bool:
    status = " ".join(filter(None, [
        _status(source),
        _clean_text(_field(source, "truth_status")),
        _clean_text(_field(source, "freshness_status")),
    ]))
    if "stale" in status:
        return True
    if _field(source, "stale_at") is not None:
        return True
    score = _float(_field(source, "staleness_score"))
    return bool(score is not None and score >= 0.5)


def _looks_conflicted(source: Any) -> bool:
    status = " ".join(filter(None, [
        _status(source),
        _clean_text(_field(source, "truth_status")),
        _clean_text(_field(source, "candidate_type")),
        _clean_text(_field(source, "work_type")),
    ]))
    if any(token in status for token in ("conflict", "contradict", "memory_conflict_resolution")):
        return True
    severity = _float(_field(source, "conflict_severity"))
    return bool(severity is not None and severity > 0)


def _looks_resolved(source: Any) -> bool:
    status = _status(source)
    if status in {"active", "applied", "complete", "completed", "recorded", "resolved", "succeeded"}:
        return True
    if _field(source, "resolved_at") is not None or _field(source, "applied_at") is not None:
        return True
    return False


def _policy_candidate_item(source: Any) -> PolicyCandidateItem | None:
    payload = _mapping(_field(source, "policy_payload"))
    candidate_type = (
        _clean_text(_field(source, "candidate_type"))
        or _clean_text(_field(source, "promotion_type"))
        or _clean_text(payload.get("candidate_type"))
        or "unknown"
    )
    status = _status(source)
    return PolicyCandidateItem(
        candidate_ref=_source_ref(source),
        candidate_type=candidate_type,
        status=status,
        review_status=_normalized_key(_field(source, "review_status")),
        policy_key=_clean_text(payload.get("policy_key")),
        version=_int(_field(source, "version")),
        created_at=_created_at(source),
        applied_at=_iso(_field(source, "applied_at") or _field(source, "activated_at")),
        rolled_back_at=_iso(_field(source, "rolled_back_at")),
    )


def _payload_mapping(source: Any) -> dict[str, Any]:
    if source is None:
        return {}
    if isinstance(source, Mapping):
        return dict(source)
    to_payload = getattr(source, "to_payload", None)
    if callable(to_payload):
        try:
            payload = to_payload()
        except TypeError:
            payload = None
        if isinstance(payload, Mapping):
            return dict(payload)
    return {}


def _field(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _payload_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _payload_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_payload_value(item) for item in value]
    if isinstance(value, list):
        return [_payload_value(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _counter_from_mapping(value: Any) -> Counter[str]:
    counter: Counter[str] = Counter()
    for key, count in _mapping(value).items():
        counter[_clean_text(key) or "unknown"] += _int(count) or 0
    return counter


def _count_buckets(counter: Counter[str], *, total: int | None = None) -> tuple[CountBucket, ...]:
    resolved_total = total if total is not None else sum(counter.values())
    buckets = [
        CountBucket(
            key=str(key),
            count=int(count),
            share=_rounded(count / resolved_total) if resolved_total else 0.0,
        )
        for key, count in counter.items()
        if count
    ]
    buckets.sort(key=lambda item: (-item.count, item.key))
    return tuple(buckets)


def _source_ref(source: Any) -> str | None:
    for key in (
        "trace_id",
        "source_ref",
        "candidate_digest",
        "signal_digest",
        "eval_digest",
        "policy_key",
    ):
        value = _clean_text(_field(source, key))
        if value:
            return value
    for key in ("run_id", "source_run_id"):
        value = _field(source, key)
        if value is not None:
            return f"run:{value}"
    value = _field(source, "id")
    if value is not None:
        return str(value)
    return None


def _created_at(source: Any) -> str | None:
    for key in ("created_at", "observed_at", "timestamp", "latest_observed_at"):
        value = _field(source, key)
        text = _iso(value)
        if text:
            return text
    return None


def _status(source: Any) -> str:
    return _normalized_key(_field(source, "status")) or "unknown"


def _sort_time(value: Any) -> str:
    return _iso(value) or ""


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalized_key(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    return text.lower().replace("-", "_").replace(" ", "_")


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _clamp(value: float | None, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value or 0.0)))


def _rounded(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def _drop_none(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _score_from_outcome_label(label: str | None) -> float | None:
    if label in _SUCCESS_LABELS:
        return 1.0
    if label in _PARTIAL_LABELS:
        return 0.5
    if label in _FAILURE_LABELS:
        return 0.0
    return None


def _is_weak_skill_trend(trend: SkillQualityTrend) -> bool:
    if trend.current_score is not None and trend.current_score < 0.6:
        return True
    return (trend.rating or "") in {"insufficient_data", "weak"}


def _sum_allowed_item_tokens(items: Iterable[Mapping[str, Any]]) -> int:
    return sum(_item_estimated_tokens(item) for item in items if _item_decision_action(item) == "allow")


def _item_estimated_tokens(item: Mapping[str, Any]) -> int:
    candidate = _mapping(item.get("candidate"))
    decision = _mapping(item.get("decision"))
    return (
        _int(candidate.get("estimated_tokens"))
        or _int(decision.get("would_spend_tokens"))
        or _int(_mapping(decision.get("cost_estimate")).get("estimated_tokens"))
        or 0
    )


def _item_decision_action(item: Mapping[str, Any]) -> str:
    decision = _mapping(item.get("decision"))
    action = _normalized_key(decision.get("action"))
    if action:
        return action
    if item.get("allowed") is True:
        return "allow"
    return "defer"


__all__ = [
    "CONTROL_APPROVE_SKILL_GRADUATION",
    "CONTROL_EXPORT_REDACTED_EVAL_ARTIFACT",
    "CONTROL_PAUSE_LEARNING",
    "CONTROL_ROLLBACK_POLICY_UPDATE",
    "CountBucket",
    "ContextUsefulnessPoint",
    "ContextUsefulnessReadModel",
    "LEARNING_OBSERVATORY_CONTROL_KEYS",
    "LEARNING_OBSERVATORY_SCHEMA_VERSION",
    "LearningControlCapability",
    "LearningObservatoryReadModel",
    "NightBudgetReadModel",
    "OutcomeLabelReadModel",
    "PolicyCandidateItem",
    "PolicyCandidateReadModel",
    "RecentOutcomeLabel",
    "SkillQualityReadModel",
    "SkillQualityTrend",
    "StaleConflictReadModel",
    "build_learning_observatory_read_model",
    "default_learning_observatory_controls",
    "summarize_context_usefulness_trends",
    "summarize_night_budget_usage",
    "summarize_outcome_labels",
    "summarize_policy_candidate_status",
    "summarize_skill_quality_trends",
    "summarize_stale_conflict_resolution",
]
