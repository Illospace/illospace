"""Neutral provider subscription-quota probing for admission boundaries."""

from __future__ import annotations

import math
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Self, TypeVar

from brain.platform.integrations.codex_usage import (
    CodexKnownUsage,
    CodexKnownUsageReading,
    CodexUnknownUsageReading,
    CodexUsageReading,
    CodexUsageUnknownReason,
    read_codex_usage,
)
from brain.platform.providers.model_policy import required_openai_auth_mode


DEFAULT_CODEX_QUOTA_SOFT_PERCENT = 75.0
DEFAULT_CODEX_QUOTA_HARD_PERCENT = 90.0


class ProviderQuotaPreflightStatus(StrEnum):
    PASSED = "passed"
    QUOTA_BLOCKED = "quota_blocked"
    QUOTA_DEFERRED = "quota_deferred"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class ProviderQuotaDecision(StrEnum):
    ADMITTED = "admitted"
    BLOCKED = "blocked"
    DEFERRED = "deferred"


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
class ProviderQuotaPreflightResult(ABC):
    provider: str
    model: str
    thresholds: ProviderQuotaThresholds
    explicit_request: bool = False
    visible_message: str | None = None

    @property
    @abstractmethod
    def status(self) -> ProviderQuotaPreflightStatus:
        raise NotImplementedError

    @property
    @abstractmethod
    def decision(self) -> ProviderQuotaDecision:
        raise NotImplementedError

    @property
    def blocked(self) -> bool:
        return self.decision is ProviderQuotaDecision.BLOCKED

    @property
    def deferred(self) -> bool:
        return self.decision is ProviderQuotaDecision.DEFERRED

    def with_presentation(self, *, visible_message: str) -> Self:
        return replace(self, visible_message=visible_message)

    @abstractmethod
    def _usage_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "decision": self.decision,
            "provider": self.provider,
            "model": self.model,
            **self._usage_dict(),
            "explicit_request": self.explicit_request,
            "thresholds": self.thresholds.to_dict(),
            "visible_message": self.visible_message,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderQuotaSkippedPreflightResult(ProviderQuotaPreflightResult):
    @property
    def status(self) -> ProviderQuotaPreflightStatus:
        return ProviderQuotaPreflightStatus.SKIPPED

    @property
    def decision(self) -> ProviderQuotaDecision:
        return ProviderQuotaDecision.ADMITTED

    def _usage_dict(self) -> dict[str, Any]:
        return {
            "usage_status": None,
            "used_percent": None,
            "unknown_reason": None,
            "observed_at": None,
            "source_path": None,
            "limit_id": None,
            "plan_type": None,
            "last_known_good": None,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class _ProviderQuotaKnownPreflightResult(ProviderQuotaPreflightResult):
    usage: CodexKnownUsageReading

    @property
    def used_percent(self) -> float:
        return self.usage.used_percent

    def _usage_dict(self) -> dict[str, Any]:
        return {
            "usage_status": self.usage.status,
            "used_percent": self.usage.used_percent,
            "unknown_reason": None,
            "observed_at": self.usage.observed_at,
            "source_path": self.usage.source_path,
            "limit_id": self.usage.limit_id,
            "plan_type": self.usage.plan_type,
            "last_known_good": None,
        }


class ProviderQuotaPassedPreflightResult(_ProviderQuotaKnownPreflightResult):
    __slots__ = ()

    @property
    def status(self) -> ProviderQuotaPreflightStatus:
        return ProviderQuotaPreflightStatus.PASSED

    @property
    def decision(self) -> ProviderQuotaDecision:
        return ProviderQuotaDecision.ADMITTED


class ProviderQuotaBlockedPreflightResult(_ProviderQuotaKnownPreflightResult):
    __slots__ = ()

    @property
    def status(self) -> ProviderQuotaPreflightStatus:
        return ProviderQuotaPreflightStatus.QUOTA_BLOCKED

    @property
    def decision(self) -> ProviderQuotaDecision:
        return ProviderQuotaDecision.BLOCKED


class ProviderQuotaDeferredPreflightResult(_ProviderQuotaKnownPreflightResult):
    __slots__ = ()

    @property
    def status(self) -> ProviderQuotaPreflightStatus:
        return ProviderQuotaPreflightStatus.QUOTA_DEFERRED

    @property
    def decision(self) -> ProviderQuotaDecision:
        return ProviderQuotaDecision.DEFERRED


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderQuotaUnknownPreflightResult(ProviderQuotaPreflightResult):
    usage: CodexUnknownUsageReading

    @property
    def status(self) -> ProviderQuotaPreflightStatus:
        return ProviderQuotaPreflightStatus.UNKNOWN

    @property
    def decision(self) -> ProviderQuotaDecision:
        return ProviderQuotaDecision.ADMITTED

    @property
    def unknown_reason(self) -> CodexUsageUnknownReason:
        return self.usage.reason

    @property
    def last_known_good(self) -> CodexKnownUsage | None:
        return self.usage.last_known_good

    def _usage_dict(self) -> dict[str, Any]:
        return {
            "usage_status": self.usage.status,
            "used_percent": None,
            "unknown_reason": self.usage.reason,
            "observed_at": self.usage.observed_at,
            "source_path": self.usage.source_path,
            "limit_id": self.usage.limit_id,
            "plan_type": self.usage.plan_type,
            "last_known_good": (
                self.usage.last_known_good.to_dict()
                if self.usage.last_known_good
                else None
            ),
        }


KnownQuotaResultT = TypeVar(
    "KnownQuotaResultT",
    bound=_ProviderQuotaKnownPreflightResult,
)


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


def _known_result(
    result_type: type[KnownQuotaResultT],
    usage: CodexKnownUsageReading,
    *,
    provider: str,
    model: str,
    explicit_request: bool,
    thresholds: ProviderQuotaThresholds,
) -> KnownQuotaResultT:
    return result_type(
        provider=provider,
        model=model,
        thresholds=thresholds,
        usage=usage,
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
        return _known_result(
            ProviderQuotaBlockedPreflightResult,
            usage,
            provider=provider,
            model=model,
            explicit_request=explicit_request,
            thresholds=thresholds,
        )
    if usage.used_percent >= thresholds.soft_percent and not explicit_request:
        return _known_result(
            ProviderQuotaDeferredPreflightResult,
            usage,
            provider=provider,
            model=model,
            explicit_request=explicit_request,
            thresholds=thresholds,
        )
    return _known_result(
        ProviderQuotaPassedPreflightResult,
        usage,
        provider=provider,
        model=model,
        explicit_request=explicit_request,
        thresholds=thresholds,
    )


__all__ = [
    "DEFAULT_CODEX_QUOTA_HARD_PERCENT",
    "DEFAULT_CODEX_QUOTA_SOFT_PERCENT",
    "ProviderQuotaBlockedPreflightResult",
    "ProviderQuotaDecision",
    "ProviderQuotaDeferredPreflightResult",
    "ProviderQuotaPassedPreflightResult",
    "ProviderQuotaPreflightResult",
    "ProviderQuotaPreflightStatus",
    "ProviderQuotaSkippedPreflightResult",
    "ProviderQuotaThresholds",
    "ProviderQuotaUnknownPreflightResult",
    "probe_provider_quota",
    "provider_quota_thresholds",
    "skipped_provider_quota_preflight",
]
