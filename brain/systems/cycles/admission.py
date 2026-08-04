"""Ordered admission for one resolved Cycle provider route."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias, cast

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.integrations.provider_auth_preflight import (
    ProviderAuthPreflightResult,
)
from brain.platform.integrations.provider_quota_preflight import (
    ProviderQuotaPreflightResult,
)
from brain.platform.providers.model_policy import (
    EFFORT_TIER_SET,
    async_get_default_model,
    infer_provider_from_model,
    normalize_model_name,
)
from brain.systems.cycles.auth_preflight import (
    async_preflight_cycle_external_auth,
)
from brain.systems.cycles.common import json_dict
from brain.systems.cycles.quota_preflight import (
    preflight_cycle_external_quota,
)

logger = logging.getLogger("cycles")


CycleThinking: TypeAlias = Literal["none", "low", "medium", "high", "xhigh"]


@dataclass(frozen=True, slots=True)
class CycleProviderRoute:
    """The one provider route used by every Cycle admission check."""

    user_id: str | None
    org_id: str | None
    model: str
    thinking: CycleThinking | None = None

    def __post_init__(self) -> None:
        canonical_model = normalize_model_name(self.model)
        checks = (
            (
                bool(self.model) and canonical_model == self.model,
                f"Cycle route model must be canonical: {canonical_model!r}",
            ),
            (
                self.thinking is None or self.thinking in EFFORT_TIER_SET,
                "Cycle route thinking must be a canonical effort tier",
            ),
        )
        for valid, message in checks:
            if not valid:
                raise ValueError(message)

    @property
    def provider(self) -> str:
        return infer_provider_from_model(self.model)

    @property
    def work_intake_model_policy(self) -> dict[str, str]:
        policy = {"model": self.model}
        if self.thinking is not None:
            policy["thinking"] = self.thinking
        return policy


@dataclass(frozen=True, slots=True)
class CycleAdmissionAdmitted:
    """A complete decision to admit work through one resolved route."""

    route: CycleProviderRoute


def _require_rejection_notice_case(
    *,
    actual: str,
    expected: str,
    rejection: str,
) -> None:
    if actual != expected:
        raise ValueError(f"{rejection} requires preflight case {expected!r}")


@dataclass(frozen=True, slots=True)
class CycleAdmissionAuthBlocked:
    """An auth rejection with its matching provider notice."""

    notice: ProviderAuthPreflightResult

    def __post_init__(self) -> None:
        _require_rejection_notice_case(
            actual=self.notice.status,
            expected="auth_blocked",
            rejection=type(self).__name__,
        )


@dataclass(frozen=True, slots=True)
class CycleAdmissionQuotaBlocked:
    """A hard-quota rejection with its matching provider notice."""

    notice: ProviderQuotaPreflightResult

    def __post_init__(self) -> None:
        _require_rejection_notice_case(
            actual=self.notice.decision,
            expected="blocked",
            rejection=type(self).__name__,
        )


@dataclass(frozen=True, slots=True)
class CycleAdmissionQuotaDeferred:
    """A soft-quota deferral with its matching provider notice."""

    notice: ProviderQuotaPreflightResult

    def __post_init__(self) -> None:
        _require_rejection_notice_case(
            actual=self.notice.decision,
            expected="deferred",
            rejection=type(self).__name__,
        )


CycleAdmissionRejected: TypeAlias = (
    CycleAdmissionAuthBlocked
    | CycleAdmissionQuotaBlocked
    | CycleAdmissionQuotaDeferred
)
CycleAdmissionOutcome: TypeAlias = CycleAdmissionAdmitted | CycleAdmissionRejected


def _cycle_run_overrides(cycle: Cycle, run: CycleRun) -> dict[str, Any]:
    context_snapshot = json_dict(getattr(run, "context_snapshot", None))
    revision_snapshot = context_snapshot.get("revision")
    if isinstance(revision_snapshot, dict):
        return revision_snapshot
    return {
        "model_override": getattr(cycle, "model_override", None),
        "thinking_override": getattr(cycle, "thinking_override", None),
    }


def _cycle_thinking(overrides: dict[str, Any]) -> CycleThinking | None:
    thinking = str(overrides.get("thinking_override") or "").strip().lower()
    if thinking and thinking not in EFFORT_TIER_SET:
        logger.warning(
            "Ignoring invalid thinking_override in CycleRun revision snapshot",
        )
        return None
    return cast(CycleThinking | None, thinking or None)


async def async_resolve_cycle_provider_route(
    session: AsyncSession,
    *,
    cycle: Cycle,
    run: CycleRun,
) -> CycleProviderRoute:
    """Resolve the provider route once from the run-bound Cycle revision."""

    user_id = str(getattr(cycle, "user_id", "") or "") or None
    org_id = str(getattr(cycle, "org_id", "") or "") or None
    overrides = _cycle_run_overrides(cycle, run)

    raw_model = str(overrides.get("model_override") or "").strip()
    if raw_model and raw_model.lower() != "default":
        model = raw_model
    else:
        model = await async_get_default_model(
            session,
            include_provider_prefix=True,
            user_id=user_id,
            org_id=org_id,
        )

    return CycleProviderRoute(
        user_id=user_id,
        org_id=org_id,
        model=normalize_model_name(model),
        thinking=_cycle_thinking(overrides),
    )


def _record_preflight_snapshot(
    run: CycleRun,
    *,
    key: str,
    preflight: ProviderAuthPreflightResult | ProviderQuotaPreflightResult,
) -> None:
    context_snapshot = dict(getattr(run, "context_snapshot", None) or {})
    context_snapshot[key] = preflight.to_dict()
    run.context_snapshot = context_snapshot


async def async_prepare_cycle_run_admission(
    session: AsyncSession,
    *,
    cycle: Cycle,
    run: CycleRun,
) -> CycleAdmissionOutcome:
    """Resolve one route, then run auth and quota checks in order."""

    route = await async_resolve_cycle_provider_route(
        session,
        cycle=cycle,
        run=run,
    )
    auth = await async_preflight_cycle_external_auth(session, route=route)
    if auth.status != "skipped":
        _record_preflight_snapshot(run, key="auth_preflight", preflight=auth)
    if auth.blocked:
        return CycleAdmissionAuthBlocked(notice=auth)

    quota = preflight_cycle_external_quota(
        route=route,
        run=run,
    )
    _record_preflight_snapshot(run, key="quota_preflight", preflight=quota)
    try:
        outcome_type, outcome_value = {
            "admitted": (CycleAdmissionAdmitted, route),
            "blocked": (CycleAdmissionQuotaBlocked, quota),
            "deferred": (CycleAdmissionQuotaDeferred, quota),
        }[quota.decision]
    except KeyError as exc:
        raise ValueError(f"Unsupported Cycle quota decision: {quota.decision!r}") from exc
    return outcome_type(outcome_value)


__all__ = [
    "CycleAdmissionAdmitted",
    "CycleAdmissionAuthBlocked",
    "CycleAdmissionOutcome",
    "CycleAdmissionQuotaBlocked",
    "CycleAdmissionQuotaDeferred",
    "CycleAdmissionRejected",
    "CycleProviderRoute",
    "async_prepare_cycle_run_admission",
    "async_resolve_cycle_provider_route",
]
