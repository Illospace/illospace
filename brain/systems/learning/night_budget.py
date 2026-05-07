"""Deterministic planner for bounded nightly learning work.

The planner is intentionally persistence-free. Callers can feed it rows already
loaded from whatever source they own, and the result is an advisory plan:
allowed items may run, denied items are normal backlog/defer outcomes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
from math import floor
from typing import Any, Mapping, Sequence

from brain.systems.learning.budget import (
    BudgetDecisionAction,
    BudgetLane,
    LearningBudgetDecision,
    LearningBudgetEntry,
    LearningBudgetLedger,
    LearningBudgetPolicy,
    LearningCostEstimate,
    ProviderLocation,
    should_run_learning_task,
)


class NightWorkType(StrEnum):
    MEMORY_CONFLICT_RESOLUTION = "memory_conflict_resolution"
    REPO_SUMMARY_REFRESH = "repo_summary_refresh"
    SKILL_EVAL = "skill_eval"
    CONTEXT_POLICY_EVAL = "context_policy_eval"
    REFLECTION_DREAM = "reflection_dream"


NIGHT_WORK_TYPE_ORDER: tuple[NightWorkType, ...] = (
    NightWorkType.MEMORY_CONFLICT_RESOLUTION,
    NightWorkType.SKILL_EVAL,
    NightWorkType.CONTEXT_POLICY_EVAL,
    NightWorkType.REPO_SUMMARY_REFRESH,
    NightWorkType.REFLECTION_DREAM,
)

DEFAULT_NIGHT_WORK_TYPE_WEIGHTS: Mapping[NightWorkType, int] = {
    NightWorkType.MEMORY_CONFLICT_RESOLUTION: 25,
    NightWorkType.SKILL_EVAL: 25,
    NightWorkType.CONTEXT_POLICY_EVAL: 20,
    NightWorkType.REPO_SUMMARY_REFRESH: 15,
    NightWorkType.REFLECTION_DREAM: 15,
}

DEFAULT_NIGHT_WORK_TYPE_TOKEN_ESTIMATES: Mapping[NightWorkType, int] = {
    NightWorkType.MEMORY_CONFLICT_RESOLUTION: 2_500,
    NightWorkType.REPO_SUMMARY_REFRESH: 1_500,
    NightWorkType.SKILL_EVAL: 3_500,
    NightWorkType.CONTEXT_POLICY_EVAL: 2_500,
    NightWorkType.REFLECTION_DREAM: 5_000,
}

WORK_TYPE_ALIASES: Mapping[str, NightWorkType] = {
    "memory": NightWorkType.MEMORY_CONFLICT_RESOLUTION,
    "memory_conflict": NightWorkType.MEMORY_CONFLICT_RESOLUTION,
    "memory_conflicts": NightWorkType.MEMORY_CONFLICT_RESOLUTION,
    "memory_conflict_resolution": NightWorkType.MEMORY_CONFLICT_RESOLUTION,
    "repo": NightWorkType.REPO_SUMMARY_REFRESH,
    "repository": NightWorkType.REPO_SUMMARY_REFRESH,
    "repo_summary": NightWorkType.REPO_SUMMARY_REFRESH,
    "repo_summary_refresh": NightWorkType.REPO_SUMMARY_REFRESH,
    "skill": NightWorkType.SKILL_EVAL,
    "skill_eval": NightWorkType.SKILL_EVAL,
    "skill_evals": NightWorkType.SKILL_EVAL,
    "context": NightWorkType.CONTEXT_POLICY_EVAL,
    "context_policy": NightWorkType.CONTEXT_POLICY_EVAL,
    "context_policy_eval": NightWorkType.CONTEXT_POLICY_EVAL,
    "reflection": NightWorkType.REFLECTION_DREAM,
    "dream": NightWorkType.REFLECTION_DREAM,
    "reflection_dream": NightWorkType.REFLECTION_DREAM,
    "dream_work": NightWorkType.REFLECTION_DREAM,
}

SIGNAL_KEYS = frozenset({
    "access_count",
    "actual_quality",
    "brain_recall_used",
    "confidence",
    "conflict_severity",
    "context_missed_memory_count",
    "cognitive_miss_count",
    "days_since_refresh",
    "run_priority",
    "run_status",
    "failed_run_count",
    "failed_run_priority",
    "failure_count",
    "failure_rate",
    "impact_score",
    "label_confidence",
    "memory_access_count",
    "memory_staleness_score",
    "missed_memory_signals",
    "quality_uncertainty",
    "repo_run_count",
    "repo_staleness_days",
    "review_status",
    "skill_confidence",
    "skill_failure_count",
    "skill_traffic_count",
    "skill_use_count",
    "staleness_score",
    "status",
    "traffic_count",
    "truth_status",
    "uncertainty",
    "unresolved_count",
    "use_count",
}
)


def _clean_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _coerce_work_type(value: NightWorkType | str) -> NightWorkType:
    if isinstance(value, NightWorkType):
        return value
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    alias = WORK_TYPE_ALIASES.get(normalized)
    if alias is not None:
        return alias
    return NightWorkType(normalized)


def _infer_work_type(mapping: Mapping[str, Any]) -> NightWorkType:
    haystack = " ".join(
        str(mapping.get(key) or "")
        for key in ("kind", "candidate_type", "source_kind", "subject_type", "task_type", "name")
    ).lower()
    if "memory" in haystack or "contradiction" in haystack or "conflict" in haystack:
        return NightWorkType.MEMORY_CONFLICT_RESOLUTION
    if "repo" in haystack or "repository" in haystack or "summary" in haystack:
        return NightWorkType.REPO_SUMMARY_REFRESH
    if "skill" in haystack:
        return NightWorkType.SKILL_EVAL
    if "context" in haystack or "policy" in haystack or "missed_memory" in haystack:
        return NightWorkType.CONTEXT_POLICY_EVAL
    return NightWorkType.REFLECTION_DREAM


def _coerce_float(mapping: Mapping[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key not in mapping:
            continue
        value = mapping.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _coerce_bool(mapping: Mapping[str, Any], key: str, default: bool | None = None) -> bool | None:
    if key not in mapping:
        return default
    value = mapping.get(key)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _score_0_to_1(value: float) -> float:
    if value > 1.0:
        return _clamp(value / 100.0, 0.0, 1.0)
    return _clamp(value, 0.0, 1.0)


def _stable_digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class NightBudgetCandidate:
    """One possible unit of nighttime learning work."""

    candidate_id: str
    work_type: NightWorkType | str
    estimated_tokens: int | None = None
    org_id: str | None = None
    user_id: str | None = None
    subject_ref: str | None = None
    description: str = ""
    impact_score: float = 0.0
    signals: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        work_type = _coerce_work_type(self.work_type)
        estimated_tokens = self.estimated_tokens
        if estimated_tokens is None:
            estimated_tokens = DEFAULT_NIGHT_WORK_TYPE_TOKEN_ESTIMATES[work_type]
        object.__setattr__(self, "candidate_id", str(self.candidate_id or "").strip())
        object.__setattr__(self, "work_type", work_type)
        object.__setattr__(self, "estimated_tokens", max(0, int(estimated_tokens or 0)))
        object.__setattr__(self, "org_id", _clean_text(self.org_id))
        object.__setattr__(self, "user_id", _clean_text(self.user_id))
        object.__setattr__(self, "subject_ref", _clean_text(self.subject_ref))
        object.__setattr__(self, "description", str(self.description or ""))
        object.__setattr__(self, "impact_score", float(self.impact_score or 0.0))
        object.__setattr__(self, "signals", dict(self.signals or {}))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
        if not self.candidate_id:
            digest = _stable_digest({
                "work_type": str(work_type),
                "org_id": self.org_id,
                "user_id": self.user_id,
                "subject_ref": self.subject_ref,
                "signals": self.signals,
            })
            object.__setattr__(self, "candidate_id", f"{work_type}:{digest}")

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Any],
        *,
        default_work_type: NightWorkType | str | None = None,
    ) -> "NightBudgetCandidate":
        work_type = mapping.get("work_type") or mapping.get("task_type") or default_work_type
        if work_type is None:
            work_type = _infer_work_type(mapping)
        resolved_work_type = _coerce_work_type(work_type)
        signals: dict[str, Any] = {}
        raw_signals = mapping.get("signals")
        if isinstance(raw_signals, Mapping):
            signals.update(raw_signals)
        for key in SIGNAL_KEYS:
            if key in mapping:
                signals[key] = mapping[key]
        metadata = mapping.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        candidate_id = (
            mapping.get("candidate_id")
            or mapping.get("id")
            or mapping.get("key")
            or mapping.get("subject_ref")
            or mapping.get("run_id")
            or mapping.get("memory_id")
            or mapping.get("skill_name")
        )
        subject_ref = (
            mapping.get("subject_ref")
            or mapping.get("run_id")
            or mapping.get("memory_id")
            or mapping.get("skill_name")
            or mapping.get("context_key")
            or mapping.get("repo")
        )
        estimated_tokens = (
            mapping.get("estimated_tokens")
            or mapping.get("token_estimate")
            or mapping.get("tokens")
            or DEFAULT_NIGHT_WORK_TYPE_TOKEN_ESTIMATES[resolved_work_type]
        )
        return cls(
            candidate_id=str(candidate_id or ""),
            work_type=resolved_work_type,
            estimated_tokens=int(estimated_tokens or 0),
            org_id=_clean_text(mapping.get("org_id")),
            user_id=_clean_text(mapping.get("user_id")),
            subject_ref=_clean_text(subject_ref),
            description=str(mapping.get("description") or mapping.get("name") or ""),
            impact_score=_coerce_float(mapping, "impact_score", default=0.0),
            signals=signals,
            metadata=metadata,
        )

    @property
    def tenant_key(self) -> str:
        if self.org_id:
            return f"org:{self.org_id}"
        if self.user_id:
            return f"user:{self.user_id}"
        return "global"

    @property
    def budget_scope(self) -> tuple[str | None, str | None]:
        if self.org_id:
            return self.org_id, None
        return None, self.user_id

    def priority_score(self) -> float:
        signals = {**self.signals, "impact_score": self.impact_score}
        score = _clamp(_coerce_float(signals, "impact_score", default=0.0), 0.0, 100.0)

        failed_runs = _coerce_float(signals, "failed_run_count", default=0.0)
        run_priority = _coerce_float(
            signals,
            "failed_run_priority",
            "run_priority",
            default=0.0,
        )
        status = str(signals.get("run_status") or signals.get("status") or "").lower()
        if status in {"blocked", "error", "failed", "retryable"}:
            failed_runs = max(failed_runs, 1.0)
        score += min(85.0, failed_runs * 18.0 + run_priority * 3.0)

        missed_memory = _coerce_float(
            signals,
            "missed_memory_signals",
            "context_missed_memory_count",
            "cognitive_miss_count",
            default=0.0,
        )
        if missed_memory:
            score += min(70.0, missed_memory * 16.0)

        if self.work_type == NightWorkType.MEMORY_CONFLICT_RESOLUTION:
            access_count = _coerce_float(signals, "memory_access_count", "access_count", default=0.0)
            staleness = _score_0_to_1(
                _coerce_float(signals, "memory_staleness_score", "staleness_score", default=0.0)
            )
            conflict_severity = _score_0_to_1(
                _coerce_float(signals, "conflict_severity", default=0.0)
            )
            truth_status = str(signals.get("truth_status") or "").lower()
            score += min(55.0, access_count * (0.5 + staleness) * 2.0)
            score += staleness * 30.0 + conflict_severity * 35.0
            if truth_status in {"conflict", "contradicted", "stale"}:
                score += 20.0

        elif self.work_type == NightWorkType.SKILL_EVAL:
            traffic = _coerce_float(
                signals,
                "skill_traffic_count",
                "skill_use_count",
                "traffic_count",
                "use_count",
                default=0.0,
            )
            confidence_raw = _coerce_float(
                signals,
                "skill_confidence",
                "confidence",
                "label_confidence",
                default=-1.0,
            )
            if confidence_raw >= 0.0:
                uncertainty = 1.0 - _score_0_to_1(confidence_raw)
            else:
                uncertainty = _score_0_to_1(
                    _coerce_float(signals, "quality_uncertainty", "uncertainty", default=0.5)
                )
            failure_count = _coerce_float(signals, "skill_failure_count", "failure_count", default=0.0)
            failure_rate = _score_0_to_1(
                _coerce_float(
                    signals,
                    "failure_rate",
                    default=(failure_count / traffic) if traffic > 0 else 0.0,
                )
            )
            score += min(60.0, traffic * (0.15 + uncertainty * 0.45))
            score += uncertainty * 30.0 + failure_rate * 35.0

        elif self.work_type == NightWorkType.CONTEXT_POLICY_EVAL:
            recall_used = _coerce_bool(signals, "brain_recall_used")
            unresolved = _coerce_float(signals, "unresolved_count", default=0.0)
            score += min(85.0, missed_memory * 22.0 + unresolved * 7.0)
            if recall_used is False:
                score += 12.0

        elif self.work_type == NightWorkType.REPO_SUMMARY_REFRESH:
            stale_days = _coerce_float(
                signals,
                "repo_staleness_days",
                "days_since_refresh",
                default=0.0,
            )
            traffic = _coerce_float(signals, "repo_run_count", "traffic_count", default=0.0)
            score += min(45.0, stale_days * 2.0)
            score += min(25.0, traffic * 0.4)

        elif self.work_type == NightWorkType.REFLECTION_DREAM:
            unresolved = _coerce_float(signals, "unresolved_count", default=0.0)
            score += min(45.0, unresolved * 8.0)
            score += min(40.0, failed_runs * 10.0 + missed_memory * 8.0)

        review_status = str(signals.get("review_status") or "").lower()
        if review_status in {"needs_review", "unreviewed", "uncertain"}:
            score += 8.0
        return round(max(0.0, score), 4)

    def to_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "work_type": str(self.work_type),
            "estimated_tokens": self.estimated_tokens,
            "org_id": self.org_id,
            "user_id": self.user_id,
            "tenant_key": self.tenant_key,
            "subject_ref": self.subject_ref,
            "description": self.description,
            "impact_score": self.impact_score,
            "priority_score": self.priority_score(),
            "signals": dict(self.signals),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class NightBudgetSettings:
    work_type_weights: Mapping[NightWorkType | str, int] = field(
        default_factory=lambda: dict(DEFAULT_NIGHT_WORK_TYPE_WEIGHTS)
    )
    reserve_tokens: int = 0
    work_type_overflow_priority: int = 85

    def __post_init__(self) -> None:
        weights = {
            _coerce_work_type(work_type): max(0, int(weight or 0))
            for work_type, weight in dict(self.work_type_weights).items()
        }
        for work_type in NightWorkType:
            weights.setdefault(work_type, 0)
        object.__setattr__(self, "work_type_weights", weights)
        object.__setattr__(self, "reserve_tokens", max(0, int(self.reserve_tokens or 0)))
        object.__setattr__(
            self,
            "work_type_overflow_priority",
            max(0, int(self.work_type_overflow_priority or 0)),
        )


@dataclass(frozen=True)
class NightBudgetPlanItem:
    candidate: NightBudgetCandidate
    decision: LearningBudgetDecision
    priority_score: float
    tenant_key: str
    sequence_no: int | None = None
    work_type_budget_tokens: int = 0
    tenant_budget_tokens: int = 0
    borrowed_from_work_type_slack: bool = False

    @property
    def allowed(self) -> bool:
        return self.decision.allowed

    def to_payload(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_payload(),
            "decision": self.decision.to_payload(),
            "priority_score": self.priority_score,
            "tenant_key": self.tenant_key,
            "sequence_no": self.sequence_no,
            "work_type_budget_tokens": self.work_type_budget_tokens,
            "tenant_budget_tokens": self.tenant_budget_tokens,
            "borrowed_from_work_type_slack": self.borrowed_from_work_type_slack,
        }


@dataclass(frozen=True)
class NightBudgetPlan:
    items: tuple[NightBudgetPlanItem, ...]
    budget_tokens: int
    budget_by_tenant: Mapping[str, int]
    budget_by_tenant_work_type: Mapping[str, Mapping[NightWorkType, int]]
    spent_by_tenant: Mapping[str, int]
    spent_by_work_type: Mapping[NightWorkType, int]
    remaining_tokens: int

    @property
    def allowed(self) -> tuple[NightBudgetPlanItem, ...]:
        return tuple(item for item in self.items if item.allowed)

    @property
    def deferred(self) -> tuple[NightBudgetPlanItem, ...]:
        return tuple(item for item in self.items if not item.allowed)

    @property
    def spent_tokens(self) -> int:
        return sum(item.candidate.estimated_tokens for item in self.allowed)

    def to_payload(self) -> dict[str, Any]:
        return {
            "budget_tokens": self.budget_tokens,
            "spent_tokens": self.spent_tokens,
            "remaining_tokens": self.remaining_tokens,
            "allowed_count": len(self.allowed),
            "deferred_count": len(self.deferred),
            "budget_by_tenant": dict(self.budget_by_tenant),
            "budget_by_tenant_work_type": {
                tenant: {str(work_type): tokens for work_type, tokens in budgets.items()}
                for tenant, budgets in self.budget_by_tenant_work_type.items()
            },
            "spent_by_tenant": dict(self.spent_by_tenant),
            "spent_by_work_type": {
                str(work_type): tokens for work_type, tokens in self.spent_by_work_type.items()
            },
            "items": [item.to_payload() for item in self.items],
        }


def _normalize_candidates(
    candidates: Sequence[NightBudgetCandidate | Mapping[str, Any]],
) -> tuple[NightBudgetCandidate, ...]:
    normalized: list[NightBudgetCandidate] = []
    for candidate in candidates:
        if isinstance(candidate, NightBudgetCandidate):
            normalized.append(candidate)
        elif isinstance(candidate, Mapping):
            normalized.append(NightBudgetCandidate.from_mapping(candidate))
        else:
            raise TypeError(f"Unsupported night budget candidate: {type(candidate)!r}")
    return tuple(normalized)


def _work_type_sort_index(work_type: NightWorkType) -> int:
    try:
        return NIGHT_WORK_TYPE_ORDER.index(work_type)
    except ValueError:
        return len(NIGHT_WORK_TYPE_ORDER)


def _candidate_sort_key(candidate: NightBudgetCandidate) -> tuple[float, int, str, int, str]:
    return (
        -candidate.priority_score(),
        candidate.estimated_tokens,
        candidate.tenant_key,
        _work_type_sort_index(candidate.work_type),
        candidate.candidate_id,
    )


def _allocate_capped_budget(
    *,
    total: int,
    keys: Sequence[str | NightWorkType],
    weights: Mapping[str | NightWorkType, float],
    demands: Mapping[str | NightWorkType, int],
) -> dict[str | NightWorkType, int]:
    allocations = {key: 0 for key in keys}
    target_total = min(max(0, int(total or 0)), sum(max(0, int(demands.get(key, 0))) for key in keys))
    if target_total <= 0:
        return allocations

    weight_map = {key: max(0.0, float(weights.get(key, 0.0))) for key in keys}
    if sum(weight_map.values()) <= 0:
        weight_map = {key: 1.0 for key in keys}
    total_weight = sum(weight_map.values())

    raw_shares: dict[str | NightWorkType, float] = {}
    for key in keys:
        demand = max(0, int(demands.get(key, 0)))
        raw_share = target_total * (weight_map[key] / total_weight)
        raw_shares[key] = raw_share
        allocations[key] = min(demand, int(floor(raw_share)))

    remaining = target_total - sum(allocations.values())
    while remaining > 0:
        eligible = [
            key
            for key in keys
            if allocations[key] < max(0, int(demands.get(key, 0)))
        ]
        if not eligible:
            break
        eligible.sort(
            key=lambda key: (
                -(raw_shares.get(key, 0.0) - floor(raw_shares.get(key, 0.0))),
                -weight_map.get(key, 0.0),
                str(key),
            )
        )
        progressed = False
        for key in eligible:
            if remaining <= 0:
                break
            if allocations[key] >= max(0, int(demands.get(key, 0))):
                continue
            allocations[key] += 1
            remaining -= 1
            progressed = True
        if not progressed:
            break

    return allocations


def _budget_decision(
    action: BudgetDecisionAction,
    reason: str,
    *,
    candidate: NightBudgetCandidate,
    remaining_tokens: int,
) -> LearningBudgetDecision:
    org_id, user_id = candidate.budget_scope
    return LearningBudgetDecision(
        action=action,
        reason=reason,
        cost_estimate=LearningCostEstimate(
            estimated_tokens=candidate.estimated_tokens,
            model_tier="night_budget",
            provider_location=ProviderLocation.LOCAL,
            org_id=org_id,
            user_id=user_id,
        ),
        lane=BudgetLane.NIGHT,
        remaining_tokens=max(0, int(remaining_tokens or 0)),
        would_spend_tokens=candidate.estimated_tokens,
    )


def _allow_with_reason(
    decision: LearningBudgetDecision,
    reason: str,
) -> LearningBudgetDecision:
    return LearningBudgetDecision(
        action=decision.action,
        reason=reason,
        cost_estimate=decision.cost_estimate,
        lane=decision.lane,
        remaining_tokens=decision.remaining_tokens,
        would_spend_tokens=decision.would_spend_tokens,
    )


def build_night_budget_plan(
    candidates: Sequence[NightBudgetCandidate | Mapping[str, Any]],
    *,
    policy: LearningBudgetPolicy | None = None,
    ledger: LearningBudgetLedger | None = None,
    settings: NightBudgetSettings | None = None,
) -> NightBudgetPlan:
    """Return a deterministic allow/defer plan for nightly learning work."""
    policy = policy or LearningBudgetPolicy.from_env()
    ledger = ledger or LearningBudgetLedger()
    settings = settings or NightBudgetSettings()
    normalized = _normalize_candidates(candidates)

    available_budget = max(
        0,
        policy.limit_for(BudgetLane.NIGHT)
        - ledger.spent_tokens(BudgetLane.NIGHT)
        - settings.reserve_tokens,
    )
    if not normalized:
        return NightBudgetPlan(
            items=(),
            budget_tokens=available_budget,
            budget_by_tenant={},
            budget_by_tenant_work_type={},
            spent_by_tenant={},
            spent_by_work_type={},
            remaining_tokens=available_budget,
        )

    tenants = sorted({candidate.tenant_key for candidate in normalized})
    tenant_demands = {
        tenant: sum(candidate.estimated_tokens for candidate in normalized if candidate.tenant_key == tenant)
        for tenant in tenants
    }
    tenant_weights = {
        tenant: sum(
            max(1.0, candidate.priority_score()) * max(1, candidate.estimated_tokens)
            for candidate in normalized
            if candidate.tenant_key == tenant
        )
        for tenant in tenants
    }
    budget_by_tenant = _allocate_capped_budget(
        total=available_budget,
        keys=tenants,
        weights=tenant_weights,
        demands=tenant_demands,
    )

    budget_by_tenant_work_type: dict[str, dict[NightWorkType, int]] = {}
    for tenant in tenants:
        tenant_candidates = [candidate for candidate in normalized if candidate.tenant_key == tenant]
        work_types = [work_type for work_type in NIGHT_WORK_TYPE_ORDER if any(c.work_type == work_type for c in tenant_candidates)]
        type_demands = {
            work_type: sum(candidate.estimated_tokens for candidate in tenant_candidates if candidate.work_type == work_type)
            for work_type in work_types
        }
        type_weights = {}
        for work_type in work_types:
            type_candidates = [candidate for candidate in tenant_candidates if candidate.work_type == work_type]
            priority_boost = sum(candidate.priority_score() for candidate in type_candidates) / max(1, len(type_candidates))
            type_weights[work_type] = float(settings.work_type_weights[work_type]) * (1.0 + priority_boost / 100.0)
        budget_by_tenant_work_type[tenant] = _allocate_capped_budget(
            total=budget_by_tenant[tenant],
            keys=work_types,
            weights=type_weights,
            demands=type_demands,
        )

    tenant_remaining = {tenant: int(budget_by_tenant.get(tenant, 0)) for tenant in tenants}
    work_type_remaining = {
        (tenant, work_type): int(tokens)
        for tenant, work_budgets in budget_by_tenant_work_type.items()
        for work_type, tokens in work_budgets.items()
    }
    global_remaining = int(available_budget)
    spent_by_tenant = {tenant: 0 for tenant in tenants}
    spent_by_work_type = {work_type: 0 for work_type in NightWorkType}
    running_ledger = ledger
    items: list[NightBudgetPlanItem] = []
    sequence_no = 1

    for candidate in sorted(normalized, key=_candidate_sort_key):
        score = candidate.priority_score()
        tenant = candidate.tenant_key
        work_type = candidate.work_type
        cost = candidate.estimated_tokens
        tenant_budget = int(budget_by_tenant.get(tenant, 0))
        type_budget = int(budget_by_tenant_work_type.get(tenant, {}).get(work_type, 0))

        if cost > global_remaining:
            items.append(
                NightBudgetPlanItem(
                    candidate=candidate,
                    decision=_budget_decision(
                        BudgetDecisionAction.DEFER,
                        "night global learning budget exhausted",
                        candidate=candidate,
                        remaining_tokens=global_remaining,
                    ),
                    priority_score=score,
                    tenant_key=tenant,
                    work_type_budget_tokens=type_budget,
                    tenant_budget_tokens=tenant_budget,
                )
            )
            continue

        if cost > tenant_remaining.get(tenant, 0):
            items.append(
                NightBudgetPlanItem(
                    candidate=candidate,
                    decision=_budget_decision(
                        BudgetDecisionAction.DEFER,
                        "tenant night learning budget exhausted",
                        candidate=candidate,
                        remaining_tokens=tenant_remaining.get(tenant, 0),
                    ),
                    priority_score=score,
                    tenant_key=tenant,
                    work_type_budget_tokens=type_budget,
                    tenant_budget_tokens=tenant_budget,
                )
            )
            continue

        type_key = (tenant, work_type)
        type_remaining = work_type_remaining.get(type_key, 0)
        borrowed = False
        if cost > type_remaining:
            if score < settings.work_type_overflow_priority:
                items.append(
                    NightBudgetPlanItem(
                        candidate=candidate,
                        decision=_budget_decision(
                            BudgetDecisionAction.DEFER,
                            f"{work_type} night learning budget exhausted",
                            candidate=candidate,
                            remaining_tokens=type_remaining,
                        ),
                        priority_score=score,
                        tenant_key=tenant,
                        work_type_budget_tokens=type_budget,
                        tenant_budget_tokens=tenant_budget,
                    )
                )
                continue
            borrowed = True

        org_id, user_id = candidate.budget_scope
        decision = should_run_learning_task(
            lane=BudgetLane.NIGHT,
            task_type=str(work_type),
            estimated_tokens=cost,
            model_tier="night_budget",
            provider_location=ProviderLocation.LOCAL,
            org_id=org_id,
            user_id=user_id,
            priority=int(round(score)),
            policy=policy,
            ledger=running_ledger,
        )
        if borrowed and decision.allowed:
            decision = _allow_with_reason(
                decision,
                "high-priority night work borrowed unused work-type slack",
            )
        if not decision.allowed:
            items.append(
                NightBudgetPlanItem(
                    candidate=candidate,
                    decision=decision,
                    priority_score=score,
                    tenant_key=tenant,
                    work_type_budget_tokens=type_budget,
                    tenant_budget_tokens=tenant_budget,
                )
            )
            continue

        items.append(
            NightBudgetPlanItem(
                candidate=candidate,
                decision=decision,
                priority_score=score,
                tenant_key=tenant,
                sequence_no=sequence_no,
                work_type_budget_tokens=type_budget,
                tenant_budget_tokens=tenant_budget,
                borrowed_from_work_type_slack=borrowed,
            )
        )
        sequence_no += 1
        running_ledger = running_ledger.append(
            LearningBudgetEntry(
                lane=BudgetLane.NIGHT,
                task_type=str(work_type),
                priority=int(round(score)),
                cost=decision.cost_estimate,
            )
        )
        global_remaining -= cost
        tenant_remaining[tenant] = max(0, tenant_remaining.get(tenant, 0) - cost)
        work_type_remaining[type_key] = max(0, type_remaining - cost)
        spent_by_tenant[tenant] = spent_by_tenant.get(tenant, 0) + cost
        spent_by_work_type[work_type] = spent_by_work_type.get(work_type, 0) + cost

    return NightBudgetPlan(
        items=tuple(items),
        budget_tokens=available_budget,
        budget_by_tenant={str(key): int(value) for key, value in budget_by_tenant.items()},
        budget_by_tenant_work_type=budget_by_tenant_work_type,
        spent_by_tenant={tenant: tokens for tenant, tokens in spent_by_tenant.items() if tokens > 0},
        spent_by_work_type={work_type: tokens for work_type, tokens in spent_by_work_type.items() if tokens > 0},
        remaining_tokens=global_remaining,
    )


__all__ = [
    "DEFAULT_NIGHT_WORK_TYPE_TOKEN_ESTIMATES",
    "DEFAULT_NIGHT_WORK_TYPE_WEIGHTS",
    "NIGHT_WORK_TYPE_ORDER",
    "NightBudgetCandidate",
    "NightBudgetPlan",
    "NightBudgetPlanItem",
    "NightBudgetSettings",
    "NightWorkType",
    "build_night_budget_plan",
]
