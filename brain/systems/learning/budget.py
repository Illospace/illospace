"""Deterministic learning budget policy and in-memory ledger.

This module is intentionally additive and persistence-free. It gives future
background cognition a governable cost contract before any worker wiring starts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import os
from typing import Mapping


class BudgetLane(StrEnum):
    HOT_PATH = "hot_path"
    AFTER_RUN = "after_run"
    NIGHT = "night"
    TENANT_DAILY = "tenant_daily"


class BudgetDecisionAction(StrEnum):
    ALLOW = "allow"
    DEFER = "defer"
    SKIP = "skip"


class ProviderLocation(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


HOT_PATH_SAFE_TASK_TYPES = frozenset({
    "metadata",
    "embedding",
    "top_k",
    "top-k",
    "topk",
})


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(0.0, min(1.0, float(str(raw).strip())))
    except (TypeError, ValueError):
        return default


def _deployment_mode(env: Mapping[str, str]) -> str:
    return str(
        env.get("LEARNING_BUDGET_DEPLOYMENT_MODE")
        or env.get("ILLO_DEPLOYMENT_MODE")
        or env.get("DEPLOYMENT_MODE")
        or "hosted"
    ).strip().lower()


def _coerce_lane(value: BudgetLane | str) -> BudgetLane:
    if isinstance(value, BudgetLane):
        return value
    return BudgetLane(str(value).strip().lower())


def _coerce_provider_location(value: ProviderLocation | str) -> ProviderLocation:
    if isinstance(value, ProviderLocation):
        return value
    return ProviderLocation(str(value).strip().lower())


@dataclass(frozen=True)
class LearningCostEstimate:
    """Audit payload for one learning operation estimate."""

    estimated_tokens: int
    model_tier: str = "unknown"
    provider_location: ProviderLocation | str = ProviderLocation.LOCAL
    provider: str | None = None
    elapsed_ms: int = 0
    blocks_user_latency: bool = False
    org_id: str | None = None
    user_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "estimated_tokens", max(0, int(self.estimated_tokens or 0)))
        object.__setattr__(self, "elapsed_ms", max(0, int(self.elapsed_ms or 0)))
        object.__setattr__(self, "model_tier", str(self.model_tier or "unknown"))
        object.__setattr__(self, "provider_location", _coerce_provider_location(self.provider_location))

    @property
    def scope(self) -> dict[str, str | None]:
        return {"org_id": self.org_id, "user_id": self.user_id}

    def to_payload(self) -> dict[str, object]:
        return {
            "estimated_tokens": self.estimated_tokens,
            "model_tier": self.model_tier,
            "provider_location": str(self.provider_location),
            "provider": self.provider,
            "elapsed_ms": self.elapsed_ms,
            "blocks_user_latency": self.blocks_user_latency,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class LearningBudgetEntry:
    lane: BudgetLane | str
    task_type: str
    cost: LearningCostEstimate
    priority: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "lane", _coerce_lane(self.lane))
        object.__setattr__(self, "task_type", str(self.task_type or "").strip().lower())
        object.__setattr__(self, "priority", int(self.priority or 0))

    def to_payload(self) -> dict[str, object]:
        return {
            "lane": str(self.lane),
            "task_type": self.task_type,
            "priority": self.priority,
            "cost": self.cost.to_payload(),
        }


@dataclass(frozen=True)
class LearningBudgetLedger:
    """In-memory spend ledger for deterministic policy tests and future adapters."""

    entries: tuple[LearningBudgetEntry, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries or ()))

    def spent_tokens(self, lane: BudgetLane | str | None = None, *, org_id: str | None = None, user_id: str | None = None) -> int:
        resolved_lane = _coerce_lane(lane) if lane is not None else None
        total = 0
        for entry in self.entries:
            if resolved_lane is not None and entry.lane != resolved_lane:
                continue
            if org_id is not None and entry.cost.org_id != org_id:
                continue
            if user_id is not None and entry.cost.user_id != user_id:
                continue
            total += entry.cost.estimated_tokens
        return total

    def append(self, entry: LearningBudgetEntry) -> "LearningBudgetLedger":
        return LearningBudgetLedger(entries=(*self.entries, entry))

    def to_payload(self) -> dict[str, object]:
        return {
            "entries": [entry.to_payload() for entry in self.entries],
            "spent_tokens": self.spent_tokens(),
        }


@dataclass(frozen=True)
class LearningBudgetPolicy:
    enabled: bool = True
    deployment_mode: str = "hosted"
    lane_token_limits: Mapping[BudgetLane | str, int] = field(default_factory=lambda: {
        BudgetLane.HOT_PATH: 1_500,
        BudgetLane.AFTER_RUN: 20_000,
        BudgetLane.NIGHT: 100_000,
        BudgetLane.TENANT_DAILY: 250_000,
    })
    allow_remote_provider: bool = True
    allow_hot_path_generation: bool = False
    after_run_sample_rate: float = 1.0
    hot_path_max_elapsed_ms: int = 75
    night_min_priority: int = 0

    def __post_init__(self) -> None:
        limits = {
            _coerce_lane(lane): max(0, int(limit or 0))
            for lane, limit in dict(self.lane_token_limits).items()
        }
        for lane in BudgetLane:
            limits.setdefault(lane, 0)
        object.__setattr__(self, "lane_token_limits", limits)
        object.__setattr__(self, "deployment_mode", str(self.deployment_mode or "hosted").strip().lower())
        object.__setattr__(self, "after_run_sample_rate", max(0.0, min(1.0, float(self.after_run_sample_rate))))
        object.__setattr__(self, "hot_path_max_elapsed_ms", max(0, int(self.hot_path_max_elapsed_ms)))
        object.__setattr__(self, "night_min_priority", int(self.night_min_priority))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LearningBudgetPolicy":
        env = env or os.environ
        deployment_mode = _deployment_mode(env)
        default_allow_remote = deployment_mode not in {"self_hosted", "self-hosted", "local"}
        return cls(
            enabled=_env_bool(env, "LEARNING_BUDGET_ENABLED", True),
            deployment_mode=deployment_mode,
            lane_token_limits={
                BudgetLane.HOT_PATH: _env_int(env, "LEARNING_BUDGET_HOT_PATH_TOKENS", 1_500),
                BudgetLane.AFTER_RUN: _env_int(env, "LEARNING_BUDGET_AFTER_RUN_TOKENS", 20_000),
                BudgetLane.NIGHT: _env_int(env, "LEARNING_BUDGET_NIGHT_TOKENS", 100_000),
                BudgetLane.TENANT_DAILY: _env_int(env, "LEARNING_BUDGET_TENANT_DAILY_TOKENS", 250_000),
            },
            allow_remote_provider=_env_bool(env, "LEARNING_BUDGET_ALLOW_REMOTE", default_allow_remote),
            allow_hot_path_generation=_env_bool(env, "LEARNING_BUDGET_ALLOW_HOT_PATH_GENERATION", False),
            after_run_sample_rate=_env_float(env, "LEARNING_BUDGET_AFTER_RUN_SAMPLE_RATE", 1.0),
            hot_path_max_elapsed_ms=_env_int(env, "LEARNING_BUDGET_HOT_PATH_MAX_ELAPSED_MS", 75),
            night_min_priority=_env_int(env, "LEARNING_BUDGET_NIGHT_MIN_PRIORITY", 0),
        )

    def limit_for(self, lane: BudgetLane | str) -> int:
        return int(self.lane_token_limits[_coerce_lane(lane)])

    def to_payload(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "deployment_mode": self.deployment_mode,
            "lane_token_limits": {str(lane): limit for lane, limit in self.lane_token_limits.items()},
            "allow_remote_provider": self.allow_remote_provider,
            "allow_hot_path_generation": self.allow_hot_path_generation,
            "after_run_sample_rate": self.after_run_sample_rate,
            "hot_path_max_elapsed_ms": self.hot_path_max_elapsed_ms,
            "night_min_priority": self.night_min_priority,
            "hot_path_safe_task_types": sorted(HOT_PATH_SAFE_TASK_TYPES),
        }


@dataclass(frozen=True)
class LearningBudgetDecision:
    action: BudgetDecisionAction | str
    reason: str
    cost_estimate: LearningCostEstimate
    lane: BudgetLane | str
    remaining_tokens: int
    would_spend_tokens: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", BudgetDecisionAction(str(self.action)))
        object.__setattr__(self, "lane", _coerce_lane(self.lane))
        object.__setattr__(self, "remaining_tokens", max(0, int(self.remaining_tokens or 0)))
        object.__setattr__(self, "would_spend_tokens", max(0, int(self.would_spend_tokens or 0)))

    @property
    def allowed(self) -> bool:
        return self.action == BudgetDecisionAction.ALLOW

    def to_payload(self) -> dict[str, object]:
        return {
            "action": str(self.action),
            "allowed": self.allowed,
            "reason": self.reason,
            "lane": str(self.lane),
            "remaining_tokens": self.remaining_tokens,
            "would_spend_tokens": self.would_spend_tokens,
            "cost_estimate": self.cost_estimate.to_payload(),
        }


def _sample_allows(sample_key: str, sample_rate: float) -> bool:
    if sample_rate >= 1.0:
        return True
    if sample_rate <= 0.0:
        return False
    digest = hashlib.sha256(sample_key.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket < sample_rate


def _decision(
    action: BudgetDecisionAction,
    reason: str,
    *,
    lane: BudgetLane,
    cost: LearningCostEstimate,
    remaining_tokens: int,
) -> LearningBudgetDecision:
    return LearningBudgetDecision(
        action=action,
        reason=reason,
        lane=lane,
        cost_estimate=cost,
        remaining_tokens=remaining_tokens,
        would_spend_tokens=cost.estimated_tokens,
    )


def should_run_learning_task(
    *,
    lane: BudgetLane | str,
    task_type: str,
    estimated_tokens: int,
    model_tier: str = "unknown",
    provider_location: ProviderLocation | str = ProviderLocation.LOCAL,
    provider: str | None = None,
    elapsed_ms: int = 0,
    blocks_user_latency: bool = False,
    org_id: str | None = None,
    user_id: str | None = None,
    priority: int = 0,
    explicit_hot_path_allow: bool = False,
    sample_key: str | None = None,
    policy: LearningBudgetPolicy | None = None,
    ledger: LearningBudgetLedger | None = None,
) -> LearningBudgetDecision:
    """Return allow/defer/skip for a prospective learning operation.

    Budget denial is expected control flow. Callers should treat ``defer`` and
    ``skip`` as governable scheduling outcomes, not exceptions.
    """
    policy = policy or LearningBudgetPolicy.from_env()
    ledger = ledger or LearningBudgetLedger()
    resolved_lane = _coerce_lane(lane)
    normalized_task = str(task_type or "").strip().lower()
    resolved_priority = int(priority or 0)
    cost = LearningCostEstimate(
        estimated_tokens=estimated_tokens,
        model_tier=model_tier,
        provider_location=provider_location,
        provider=provider,
        elapsed_ms=elapsed_ms,
        blocks_user_latency=blocks_user_latency,
        org_id=org_id,
        user_id=user_id,
    )

    lane_remaining = max(0, policy.limit_for(resolved_lane) - ledger.spent_tokens(resolved_lane, org_id=org_id, user_id=user_id))
    tenant_remaining = max(0, policy.limit_for(BudgetLane.TENANT_DAILY) - ledger.spent_tokens(org_id=org_id, user_id=user_id))
    remaining = min(lane_remaining, tenant_remaining)

    if not policy.enabled:
        return _decision(BudgetDecisionAction.ALLOW, "learning budget policy disabled", lane=resolved_lane, cost=cost, remaining_tokens=remaining)

    if cost.provider_location == ProviderLocation.REMOTE and not policy.allow_remote_provider:
        return _decision(BudgetDecisionAction.DEFER, "remote learning provider disabled by policy", lane=resolved_lane, cost=cost, remaining_tokens=remaining)

    if tenant_remaining <= 0 or cost.estimated_tokens > tenant_remaining:
        return _decision(BudgetDecisionAction.SKIP, "tenant daily learning budget exhausted", lane=resolved_lane, cost=cost, remaining_tokens=tenant_remaining)

    if resolved_lane == BudgetLane.HOT_PATH:
        safe_task = normalized_task in HOT_PATH_SAFE_TASK_TYPES
        explicitly_allowed = explicit_hot_path_allow or policy.allow_hot_path_generation
        if not safe_task and not explicitly_allowed:
            return _decision(BudgetDecisionAction.DEFER, "hot path only allows metadata, embedding, and top-k learning by default", lane=resolved_lane, cost=cost, remaining_tokens=remaining)
        if cost.blocks_user_latency and cost.elapsed_ms > policy.hot_path_max_elapsed_ms:
            return _decision(BudgetDecisionAction.DEFER, "hot path learning would exceed user-latency budget", lane=resolved_lane, cost=cost, remaining_tokens=remaining)
        if cost.estimated_tokens > lane_remaining:
            return _decision(BudgetDecisionAction.DEFER, "hot path learning lane budget exhausted", lane=resolved_lane, cost=cost, remaining_tokens=lane_remaining)
        return _decision(BudgetDecisionAction.ALLOW, "hot path learning within policy", lane=resolved_lane, cost=cost, remaining_tokens=remaining)

    if resolved_lane == BudgetLane.AFTER_RUN:
        key = sample_key or f"{org_id}:{user_id}:{normalized_task}:{cost.estimated_tokens}:{cost.model_tier}"
        if not _sample_allows(key, policy.after_run_sample_rate):
            return _decision(BudgetDecisionAction.DEFER, "after-run learning deferred by deterministic sample policy", lane=resolved_lane, cost=cost, remaining_tokens=remaining)
        if cost.estimated_tokens > lane_remaining:
            return _decision(BudgetDecisionAction.DEFER, "after-run learning lane budget exhausted", lane=resolved_lane, cost=cost, remaining_tokens=lane_remaining)
        return _decision(BudgetDecisionAction.ALLOW, "after-run learning within policy", lane=resolved_lane, cost=cost, remaining_tokens=remaining)

    if resolved_lane == BudgetLane.NIGHT:
        if resolved_priority < policy.night_min_priority:
            return _decision(BudgetDecisionAction.DEFER, "night learning deferred below priority floor", lane=resolved_lane, cost=cost, remaining_tokens=remaining)
        if cost.estimated_tokens > lane_remaining:
            return _decision(BudgetDecisionAction.SKIP, "night learning lane budget exhausted", lane=resolved_lane, cost=cost, remaining_tokens=lane_remaining)
        return _decision(BudgetDecisionAction.ALLOW, "night learning spends remaining budget by priority", lane=resolved_lane, cost=cost, remaining_tokens=remaining)

    if cost.estimated_tokens > lane_remaining:
        return _decision(BudgetDecisionAction.SKIP, "learning lane budget exhausted", lane=resolved_lane, cost=cost, remaining_tokens=lane_remaining)
    return _decision(BudgetDecisionAction.ALLOW, "learning task within budget", lane=resolved_lane, cost=cost, remaining_tokens=remaining)


__all__ = [
    "BudgetDecisionAction",
    "BudgetLane",
    "HOT_PATH_SAFE_TASK_TYPES",
    "LearningBudgetDecision",
    "LearningBudgetEntry",
    "LearningBudgetLedger",
    "LearningBudgetPolicy",
    "LearningCostEstimate",
    "ProviderLocation",
    "should_run_learning_task",
]
