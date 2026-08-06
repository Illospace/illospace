"""Neutral provider subscription-quota probing for admission boundaries."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace
from typing import Any, Self, TypeAlias

from brain.platform.integrations.codex_usage import (
    CodexKnownUsage,
    CodexUnknownUsageReading,
    CodexUsageReading,
    read_codex_usage,
)
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


@dataclass(frozen=True, slots=True, kw_only=True)
class _ProviderQuotaPreflightResult:
    provider: str
    model: str
    thresholds: ProviderQuotaThresholds
    explicit_request: bool = False
    visible_message: str | None = None

    def with_presentation(self, *, visible_message: str) -> Self:
        return replace(self, visible_message=visible_message)

    def _to_dict(
        self,
        *,
        status: str,
        decision: str,
        usage_fields: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "status": status,
            "decision": decision,
            "provider": self.provider,
            "model": self.model,
            **usage_fields,
            "explicit_request": self.explicit_request,
            "thresholds": self.thresholds.to_dict(),
            "visible_message": self.visible_message,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderQuotaSkippedPreflightResult(_ProviderQuotaPreflightResult):
    def to_dict(self) -> dict[str, Any]:
        return self._to_dict(
            status="skipped",
            decision="admitted",
            usage_fields={
                "usage_status": None,
                "used_percent": None,
                "unknown_reason": None,
                "observed_at": None,
                "source_path": None,
                "limit_id": None,
                "plan_type": None,
                "last_known_good": None,
            },
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class _ProviderQuotaKnownPreflightResult(_ProviderQuotaPreflightResult):
    usage: CodexKnownUsage

    def _known_to_dict(self, *, status: str, decision: str) -> dict[str, Any]:
        return self._to_dict(
            status=status,
            decision=decision,
            usage_fields=_provider_quota_usage_fields(self.usage),
        )


class ProviderQuotaPassedPreflightResult(_ProviderQuotaKnownPreflightResult):
    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        return self._known_to_dict(status="passed", decision="admitted")


class ProviderQuotaBlockedPreflightResult(_ProviderQuotaKnownPreflightResult):
    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        return self._known_to_dict(status="quota_blocked", decision="blocked")


class ProviderQuotaDeferredPreflightResult(_ProviderQuotaKnownPreflightResult):
    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        return self._known_to_dict(status="quota_deferred", decision="deferred")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderQuotaUnknownPreflightResult(_ProviderQuotaPreflightResult):
    usage: CodexUnknownUsageReading

    def to_dict(self) -> dict[str, Any]:
        return self._to_dict(
            status="unknown",
            decision="admitted",
            usage_fields=_provider_quota_usage_fields(self.usage),
        )


ProviderQuotaPreflightResult: TypeAlias = (
    ProviderQuotaSkippedPreflightResult
    | ProviderQuotaPassedPreflightResult
    | ProviderQuotaBlockedPreflightResult
    | ProviderQuotaDeferredPreflightResult
    | ProviderQuotaUnknownPreflightResult
)


def _provider_quota_usage_fields(
    usage: CodexUsageReading,
) -> dict[str, Any]:
    reading = usage.to_dict()
    return {
        "usage_status": reading["status"],
        "used_percent": reading["used_percent"],
        "unknown_reason": reading["reason"],
        "observed_at": reading["observed_at"],
        "source_path": reading["source_path"],
        "limit_id": reading["limit_id"],
        "plan_type": reading["plan_type"],
        "last_known_good": reading["last_known_good"],
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
) -> ProviderQuotaSkippedPreflightResult:
    return ProviderQuotaSkippedPreflightResult(
        provider=provider,
        model=model,
        thresholds=thresholds or provider_quota_thresholds(),
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

    usage: CodexUsageReading = read_codex_usage()
    if isinstance(usage, CodexUnknownUsageReading):
        return ProviderQuotaUnknownPreflightResult(
            provider=provider,
            model=model,
            thresholds=thresholds,
            usage=usage,
            explicit_request=explicit_request,
        )

    if usage.used_percent >= thresholds.hard_percent:
        return ProviderQuotaBlockedPreflightResult(
            provider=provider,
            model=model,
            explicit_request=explicit_request,
            thresholds=thresholds,
            usage=usage,
        )
    if usage.used_percent >= thresholds.soft_percent and not explicit_request:
        return ProviderQuotaDeferredPreflightResult(
            provider=provider,
            model=model,
            explicit_request=explicit_request,
            thresholds=thresholds,
            usage=usage,
        )
    return ProviderQuotaPassedPreflightResult(
        provider=provider,
        model=model,
        explicit_request=explicit_request,
        thresholds=thresholds,
        usage=usage,
    )


__all__ = [
    "DEFAULT_CODEX_QUOTA_HARD_PERCENT",
    "DEFAULT_CODEX_QUOTA_SOFT_PERCENT",
    "ProviderQuotaBlockedPreflightResult",
    "ProviderQuotaDeferredPreflightResult",
    "ProviderQuotaPassedPreflightResult",
    "ProviderQuotaPreflightResult",
    "ProviderQuotaSkippedPreflightResult",
    "ProviderQuotaThresholds",
    "ProviderQuotaUnknownPreflightResult",
    "probe_provider_quota",
    "provider_quota_thresholds",
    "skipped_provider_quota_preflight",
]
