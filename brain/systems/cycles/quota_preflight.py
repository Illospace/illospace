"""Cycle adapter for neutral provider subscription-quota probing."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.integrations.provider_quota_preflight import (
    ProviderQuotaPreflightResult,
    probe_provider_quota,
)
from brain.platform.providers.model_policy import (
    async_get_default_model,
    infer_provider_from_model,
)
from brain.systems.cycles.common import (
    SCHEDULED_CYCLE_ORIGIN,
    cycle_run_launch_context,
)


def _with_cycle_presentation(
    result: ProviderQuotaPreflightResult,
) -> ProviderQuotaPreflightResult:
    if result.blocked:
        return result.with_presentation(
            visible_message=(
                "Cycle quota blocked: Codex usage is "
                f"{result.used_percent:g}%, at or above the "
                f"{result.thresholds.hard_percent:g}% hard limit. "
                "Illo will admit new runs automatically after usage falls below the limit."
            )
        )
    if result.deferred:
        return result.with_presentation(
            visible_message=(
                "Scheduled Cycle quota deferred: Codex usage is "
                f"{result.used_percent:g}%, at or above the "
                f"{result.thresholds.soft_percent:g}% soft limit. "
                "Illo will try again on a later scheduled run."
            )
        )
    return result


async def async_preflight_cycle_external_quota(
    session: AsyncSession,
    *,
    cycle: Any,
    run: Any,
) -> ProviderQuotaPreflightResult:
    """Probe subscription quota before admitting a Cycle agent run."""

    user_id = str(getattr(cycle, "user_id", "") or "") or None
    org_id = str(getattr(cycle, "org_id", "") or "") or None
    model = str(getattr(cycle, "model_override", None) or "").strip()
    if not model:
        model = await async_get_default_model(
            session,
            include_provider_prefix=True,
            user_id=user_id,
            org_id=org_id,
        )

    provider = infer_provider_from_model(model)
    origin = str(cycle_run_launch_context(run).get("origin") or "")
    result = probe_provider_quota(
        provider=provider,
        model=model,
        explicit_request=origin != SCHEDULED_CYCLE_ORIGIN,
    )
    return _with_cycle_presentation(result)


__all__ = ["async_preflight_cycle_external_quota"]
