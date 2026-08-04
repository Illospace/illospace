"""Neutral provider subscription-quota probing for admission boundaries."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace
from typing import Any

from brain.platform.integrations.codex_usage import CodexUsageReading, read_codex_usage
from brain.platform.providers.model_policy import required_openai_auth_mode


DEFAULT_CODEX_QUOTA_SOFT_PERCENT = 75.0
DEFAULT_CODEX_QUOTA_HARD_PERCENT = 90.0


@dataclass(frozen=True, slots=True)
class ProviderQuotaThresholds:
    soft_percent: float
    hard_percent: float

    def to_dict(self) -> dict[str, float]:
        return {
            "soft_percent": self.soft_percent,
            "hard_percent": self.hard_percent,
        }


@dataclass(frozen=True, slots=True)
class ProviderQuotaPreflightResult:
    status: str
    decision: str
    provider: str
    model: str
    usage_status: str | None
    thresholds: ProviderQuotaThresholds
    used_percent: float | None = None
    unknown_reason: str | None = None
    observed_at: str | None = None
    source_path: str | None = None
    limit_id: str | None = None
    plan_type: str | None = None
    last_known_good: dict[str, Any] | None = None
    explicit_request: bool = False
    visible_message: str | None = None

    @property
    def blocked(self) -> bool:
        return self.decision == "blocked"

    @property
    def deferred(self) -> bool:
        return self.decision == "deferred"

    def with_presentation(self, *, visible_message: str) -> ProviderQuotaPreflightResult:
        return replace(self, visible_message=visible_message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "decision": self.decision,
            "provider": self.provider,
            "model": self.model,
            "usage_status": self.usage_status,
            "used_percent": self.used_percent,
            "unknown_reason": self.unknown_reason,
            "observed_at": self.observed_at,
            "source_path": self.source_path,
            "limit_id": self.limit_id,
            "plan_type": self.plan_type,
            "last_known_good": self.last_known_good,
            "explicit_request": self.explicit_request,
            "thresholds": self.thresholds.to_dict(),
            "visible_message": self.visible_message,
        }


def _percent_setting(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or not 0 <= value <= 100:
        return default
    return value


def provider_quota_thresholds() -> ProviderQuotaThresholds:
    soft = _percent_setting(
        "ILLO_CODEX_QUOTA_SOFT_PERCENT",
        DEFAULT_CODEX_QUOTA_SOFT_PERCENT,
    )
    hard = _percent_setting(
        "ILLO_CODEX_QUOTA_HARD_PERCENT",
        DEFAULT_CODEX_QUOTA_HARD_PERCENT,
    )
    if soft >= hard:
        soft = DEFAULT_CODEX_QUOTA_SOFT_PERCENT
        hard = DEFAULT_CODEX_QUOTA_HARD_PERCENT
    return ProviderQuotaThresholds(soft_percent=soft, hard_percent=hard)


def skipped_provider_quota_preflight(
    *,
    provider: str,
    model: str,
    explicit_request: bool,
    thresholds: ProviderQuotaThresholds | None = None,
) -> ProviderQuotaPreflightResult:
    return ProviderQuotaPreflightResult(
        status="skipped",
        decision="admitted",
        provider=provider,
        model=model,
        usage_status=None,
        thresholds=thresholds or provider_quota_thresholds(),
        explicit_request=explicit_request,
    )


def _result_from_usage(
    usage: CodexUsageReading,
    *,
    status: str,
    decision: str,
    provider: str,
    model: str,
    explicit_request: bool,
    thresholds: ProviderQuotaThresholds,
) -> ProviderQuotaPreflightResult:
    return ProviderQuotaPreflightResult(
        status=status,
        decision=decision,
        provider=provider,
        model=model,
        usage_status=usage.status,
        thresholds=thresholds,
        used_percent=usage.used_percent,
        unknown_reason=usage.reason,
        observed_at=usage.observed_at,
        source_path=usage.source_path,
        limit_id=usage.limit_id,
        plan_type=usage.plan_type,
        last_known_good=(
            usage.last_known_good.to_dict() if usage.last_known_good else None
        ),
        explicit_request=explicit_request,
    )


def probe_provider_quota(
    *,
    provider: str,
    model: str,
    explicit_request: bool,
) -> ProviderQuotaPreflightResult:
    """Probe one provider route and apply neutral quota-admission policy."""

    thresholds = provider_quota_thresholds()
    if provider != "openai" or required_openai_auth_mode(model) != "chatgpt":
        return skipped_provider_quota_preflight(
            provider=provider,
            model=model,
            explicit_request=explicit_request,
            thresholds=thresholds,
        )

    usage = read_codex_usage()
    if usage.status == "unknown":
        return _result_from_usage(
            usage,
            status="unknown",
            decision="admitted",
            provider=provider,
            model=model,
            explicit_request=explicit_request,
            thresholds=thresholds,
        )

    used_percent = float(usage.used_percent)
    if used_percent >= thresholds.hard_percent:
        return _result_from_usage(
            usage,
            status="quota_blocked",
            decision="blocked",
            provider=provider,
            model=model,
            explicit_request=explicit_request,
            thresholds=thresholds,
        )
    if used_percent >= thresholds.soft_percent and not explicit_request:
        return _result_from_usage(
            usage,
            status="quota_deferred",
            decision="deferred",
            provider=provider,
            model=model,
            explicit_request=explicit_request,
            thresholds=thresholds,
        )
    return _result_from_usage(
        usage,
        status="passed",
        decision="admitted",
        provider=provider,
        model=model,
        explicit_request=explicit_request,
        thresholds=thresholds,
    )


__all__ = [
    "DEFAULT_CODEX_QUOTA_HARD_PERCENT",
    "DEFAULT_CODEX_QUOTA_SOFT_PERCENT",
    "ProviderQuotaPreflightResult",
    "ProviderQuotaThresholds",
    "probe_provider_quota",
    "provider_quota_thresholds",
    "skipped_provider_quota_preflight",
]
