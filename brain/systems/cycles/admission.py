"""Ordered admission for one resolved Cycle provider route."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

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
    async_preflight_cycle_external_quota,
)

logger = logging.getLogger("cycles")


@dataclass(frozen=True)
class CycleProviderRoute:
    """The one provider route used by every Cycle admission check."""

    user_id: str | None
    org_id: str | None
    model: str
    provider: str
    model_policy: dict[str, str]


@dataclass(frozen=True)
class CycleAdmissionOutcome:
    """Ordered auth and quota decisions for a resolved provider route."""

    route: CycleProviderRoute
    auth: ProviderAuthPreflightResult
    quota: ProviderQuotaPreflightResult | None = None

    @property
    def rejection_status(self) -> str | None:
        if self.auth.blocked:
            return "auth_blocked"
        if self.quota is not None and self.quota.blocked:
            return "quota_blocked"
        if self.quota is not None and self.quota.deferred:
            return "skipped"
        return None

    @property
    def rejected(self) -> bool:
        return self.rejection_status is not None

    @property
    def error(self) -> str | None:
        if self.auth.blocked:
            return self.auth.visible_message
        if self.quota is not None and self.quota.blocked:
            return self.quota.visible_message
        return None

    @property
    def skip_reason(self) -> str | None:
        if self.quota is not None and self.quota.deferred:
            return "quota_soft_limit"
        return None


def _cycle_run_overrides(cycle: Cycle, run: CycleRun) -> dict[str, Any]:
    context_snapshot = json_dict(getattr(run, "context_snapshot", None))
    revision_snapshot = context_snapshot.get("revision")
    if isinstance(revision_snapshot, dict):
        return revision_snapshot
    return {
        "model_override": getattr(cycle, "model_override", None),
        "thinking_override": getattr(cycle, "thinking_override", None),
    }


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
        model = normalize_model_name(raw_model)
    else:
        model = await async_get_default_model(
            session,
            include_provider_prefix=True,
            user_id=user_id,
            org_id=org_id,
        )

    model_policy = {"model": model}
    thinking = str(overrides.get("thinking_override") or "").strip().lower()
    if thinking in EFFORT_TIER_SET:
        model_policy["thinking"] = thinking
    elif thinking:
        logger.warning(
            "Ignoring invalid thinking_override in CycleRun revision snapshot",
        )

    return CycleProviderRoute(
        user_id=user_id,
        org_id=org_id,
        model=model,
        provider=infer_provider_from_model(model),
        model_policy=model_policy,
    )


def _record_admission_snapshot(
    run: CycleRun,
    outcome: CycleAdmissionOutcome,
) -> None:
    context_snapshot = dict(getattr(run, "context_snapshot", None) or {})
    if outcome.auth.status != "skipped":
        context_snapshot["auth_preflight"] = outcome.auth.to_dict()
    if outcome.quota is not None:
        context_snapshot["quota_preflight"] = outcome.quota.to_dict()
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
    if auth.blocked:
        outcome = CycleAdmissionOutcome(route=route, auth=auth)
        _record_admission_snapshot(run, outcome)
        return outcome

    quota = await async_preflight_cycle_external_quota(
        session,
        route=route,
        run=run,
    )
    outcome = CycleAdmissionOutcome(route=route, auth=auth, quota=quota)
    _record_admission_snapshot(run, outcome)
    return outcome


__all__ = [
    "CycleAdmissionOutcome",
    "CycleProviderRoute",
    "async_prepare_cycle_run_admission",
    "async_resolve_cycle_provider_route",
]
