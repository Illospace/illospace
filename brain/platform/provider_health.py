"""Operation-aware provider health and degradation policy."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Literal

ProviderOperation = Literal["scout", "coordinator", "worker", "memory_extraction", "verifier"]

PROVIDER_OPERATIONS: tuple[ProviderOperation, ...] = (
    "scout",
    "coordinator",
    "worker",
    "memory_extraction",
    "verifier",
)


@dataclass(frozen=True)
class DegradationPolicy:
    """How an operation should behave when its provider/model is unhealthy."""

    operation_type: ProviderOperation
    fallback_tiers: tuple[str, ...]
    fail_open: bool
    fail_closed_for_high_risk: bool = False
    unavailable_after_failures: int = 3
    outage_window_seconds: int = 300


@dataclass
class ProviderModelHealth:
    operation_type: ProviderOperation
    provider: str
    model: str
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_latency_ms: int | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error_type: str | None = None
    last_error: str | None = None
    degraded_events: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def health_status(self, policy: DegradationPolicy, *, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        if self.failures <= 0:
            return "healthy"
        if self.last_failure_at is None:
            return "degraded"
        within_window = now - self.last_failure_at <= timedelta(seconds=policy.outage_window_seconds)
        if within_window and self.consecutive_failures >= policy.unavailable_after_failures:
            return "unavailable"
        return "degraded"

    def to_dict(self, policy: DegradationPolicy) -> dict[str, Any]:
        status = self.health_status(policy)
        return {
            "operation_type": self.operation_type,
            "provider": self.provider,
            "model": self.model,
            "status": status,
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_latency_ms": self.last_latency_ms,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_failure_at": self.last_failure_at.isoformat() if self.last_failure_at else None,
            "last_error_type": self.last_error_type,
            "last_error": self.last_error,
            "degraded_events": self.degraded_events,
            "policy": policy_to_dict(policy),
            "metadata": deepcopy(self.metadata),
        }


_POLICIES: dict[ProviderOperation, DegradationPolicy] = {
    "scout": DegradationPolicy(
        operation_type="scout",
        fallback_tiers=("full_pipeline",),
        fail_open=True,
        unavailable_after_failures=1,
    ),
    "coordinator": DegradationPolicy(
        operation_type="coordinator",
        fallback_tiers=("retry_same_model", "degrade_optional_betas", "operator_visible_failure"),
        fail_open=False,
    ),
    "worker": DegradationPolicy(
        operation_type="worker",
        fallback_tiers=("retry_same_model", "native_worker_fallback", "operator_visible_failure"),
        fail_open=False,
    ),
    "memory_extraction": DegradationPolicy(
        operation_type="memory_extraction",
        fallback_tiers=("llm_retry_later", "skip_noncritical_harvest"),
        fail_open=True,
        unavailable_after_failures=1,
    ),
    "verifier": DegradationPolicy(
        operation_type="verifier",
        fallback_tiers=("deterministic_verifiers", "fail_closed_high_risk"),
        fail_open=False,
        fail_closed_for_high_risk=True,
        unavailable_after_failures=1,
    ),
}

_HEALTH: dict[tuple[ProviderOperation, str, str], ProviderModelHealth] = {}
_LOCK = RLock()


def _operation(operation_type: str | None) -> ProviderOperation:
    if operation_type in PROVIDER_OPERATIONS:
        return operation_type  # type: ignore[return-value]
    return "coordinator"


def _key(operation_type: str | None, provider: str | None, model: str | None) -> tuple[ProviderOperation, str, str]:
    return (_operation(operation_type), (provider or "unknown").strip().lower() or "unknown", (model or "unknown").strip() or "unknown")


def get_degradation_policy(operation_type: str | None) -> DegradationPolicy:
    return _POLICIES[_operation(operation_type)]


def policy_to_dict(policy: DegradationPolicy) -> dict[str, Any]:
    return {
        "operation_type": policy.operation_type,
        "fallback_tiers": list(policy.fallback_tiers),
        "fail_open": policy.fail_open,
        "fail_closed_for_high_risk": policy.fail_closed_for_high_risk,
        "unavailable_after_failures": policy.unavailable_after_failures,
        "outage_window_seconds": policy.outage_window_seconds,
    }


def record_provider_success(
    *,
    operation_type: str | None,
    provider: str | None,
    model: str | None,
    latency_ms: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> ProviderModelHealth:
    key = _key(operation_type, provider, model)
    now = datetime.now(timezone.utc)
    with _LOCK:
        health = _HEALTH.get(key)
        if health is None:
            health = ProviderModelHealth(operation_type=key[0], provider=key[1], model=key[2])
            _HEALTH[key] = health
        health.attempts += 1
        health.successes += 1
        health.consecutive_failures = 0
        health.last_latency_ms = latency_ms
        health.last_success_at = now
        if metadata:
            health.metadata.update(metadata)
        return deepcopy(health)


def record_provider_failure(
    *,
    operation_type: str | None,
    provider: str | None,
    model: str | None,
    exc: Exception | str,
    latency_ms: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> ProviderModelHealth:
    key = _key(operation_type, provider, model)
    now = datetime.now(timezone.utc)
    error_text = str(exc)
    error_type = type(exc).__name__ if isinstance(exc, Exception) else "ProviderUnavailable"
    with _LOCK:
        health = _HEALTH.get(key)
        if health is None:
            health = ProviderModelHealth(operation_type=key[0], provider=key[1], model=key[2])
            _HEALTH[key] = health
        health.attempts += 1
        health.failures += 1
        health.consecutive_failures += 1
        health.last_latency_ms = latency_ms
        health.last_failure_at = now
        health.last_error_type = error_type
        health.last_error = error_text[:500]
        health.degraded_events += 1
        if metadata:
            health.metadata.update(metadata)
        return deepcopy(health)


def provider_health_snapshot() -> dict[str, Any]:
    with _LOCK:
        entries = [deepcopy(health) for health in _HEALTH.values()]

    operations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    summary: dict[str, dict[str, int]] = {
        op: {"healthy": 0, "degraded": 0, "unavailable": 0}
        for op in PROVIDER_OPERATIONS
    }
    for health in entries:
        policy = get_degradation_policy(health.operation_type)
        payload = health.to_dict(policy)
        operations[health.operation_type].append(payload)
        summary[health.operation_type][payload["status"]] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "operations": {op: operations.get(op, []) for op in PROVIDER_OPERATIONS},
        "summary": summary,
        "policies": {op: policy_to_dict(policy) for op, policy in _POLICIES.items()},
    }


def reset_provider_health() -> None:
    """Test helper for clearing process-local health evidence."""
    with _LOCK:
        _HEALTH.clear()


def classify_degradation_reason(exc: Exception | str) -> str:
    text = str(exc).lower()
    if any(term in text for term in ("timeout", "timed out", "deadline")):
        return "provider_timeout"
    if any(term in text for term in ("401", "403", "auth", "api key", "unauthorized")):
        return "provider_auth_unavailable"
    if any(term in text for term in ("429", "rate limit", "overloaded", "529")):
        return "provider_rate_limited"
    return "provider_unavailable"


_HIGH_RISK_CONTRACT_TYPES = {
    "pr",
    "created_pr",
    "pr_review",
    "existing_pr_review",
    "commit",
    "file",
    "document",
    "code_change",
    "deployment",
    "cycle_create",
    "cycle_update",
    "domain_update",
    "workspace_app",
}


def is_high_risk_run(run: Any) -> bool:
    contract_type = str(getattr(run, "contract_type", "") or "").strip().lower()
    if contract_type in _HIGH_RISK_CONTRACT_TYPES:
        return True
    requirements = getattr(run, "contract_requirements", None) or {}
    if isinstance(requirements, dict):
        traceability = requirements.get("traceability") or {}
        if any(bool(traceability.get(key)) for key in ("require_pr", "require_merge", "require_branch")):
            return True
    target_status = str(getattr(run, "target_status", "") or "").strip().lower()
    return target_status in {"must_verify", "high_risk"}


def verifier_unavailability_for_run(run: Any) -> dict[str, Any]:
    policy = get_degradation_policy("verifier")
    high_risk = is_high_risk_run(run)
    snapshot = provider_health_snapshot()
    unavailable = [
        entry
        for entry in snapshot["operations"].get("verifier", [])
        if entry.get("status") == "unavailable"
    ]
    return {
        "operation_type": "verifier",
        "high_risk": high_risk,
        "fail_closed": bool(high_risk and policy.fail_closed_for_high_risk and unavailable),
        "reason": "verifier_provider_unavailable" if unavailable else None,
        "unavailable": unavailable,
        "policy": policy_to_dict(policy),
    }
