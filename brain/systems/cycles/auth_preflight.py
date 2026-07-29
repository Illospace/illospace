"""Scheduled-Cycle adapter for neutral provider credential probing."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.integrations.provider_auth_preflight import (
    ProviderAuthPreflightResult,
    async_probe_provider_auth,
)
from brain.platform.providers.model_policy import (
    async_get_default_model,
    infer_provider_from_model,
)


def _with_cycle_presentation(
    result: ProviderAuthPreflightResult,
) -> ProviderAuthPreflightResult:
    if not result.blocked:
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
    cycle: Any,
) -> ProviderAuthPreflightResult:
    """Probe the Cycle's selected provider before admitting an agent run."""

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
    result = await async_probe_provider_auth(
        session,
        user_id=user_id,
        org_id=org_id,
        provider=provider,
        model=model,
    )
    return _with_cycle_presentation(result)


__all__ = ["async_preflight_cycle_external_auth"]
