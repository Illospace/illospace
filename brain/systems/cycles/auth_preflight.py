"""Scheduled-Cycle adapter for neutral provider credential probing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.integrations.provider_auth_preflight import (
    ProviderAuthBlockedPreflightResult,
    ProviderAuthPreflightResult,
    async_probe_provider_auth,
)

if TYPE_CHECKING:
    from brain.systems.cycles.admission import CycleProviderRoute


def _with_cycle_presentation(
    result: ProviderAuthPreflightResult,
) -> ProviderAuthPreflightResult:
    if not isinstance(result, ProviderAuthBlockedPreflightResult):
        return result
    if result.provider == "anthropic":
        repair_action = (
            "Add an Anthropic API key in Settings > Access, then rerun the Cycle or "
            "choose an available OpenAI model."
        )
        credential = result.credential or "Anthropic API key"
    else:
        repair_action = (
            "Reconnect OpenAI in Settings > Access by signing in to Codex / ChatGPT again, "
            "then rerun the Cycle or wait for its next scheduled run."
        )
        credential = result.credential or "OpenAI"
    return result.with_presentation(
        repair_action=repair_action,
        visible_message=(
            "Scheduled Cycle auth blocked: the "
            f"{credential} credential is unavailable or could not be refreshed. "
            f"{repair_action}"
        ),
    )


async def async_preflight_cycle_external_auth(
    session: AsyncSession,
    *,
    route: CycleProviderRoute,
) -> ProviderAuthPreflightResult:
    """Probe the Cycle's selected provider before admitting an agent run."""

    result = await async_probe_provider_auth(
        session,
        user_id=route.user_id,
        org_id=route.org_id,
        provider=route.provider,
        model=route.model,
    )
    return _with_cycle_presentation(result)


__all__ = ["async_preflight_cycle_external_auth"]
