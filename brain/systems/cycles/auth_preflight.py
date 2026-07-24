"""Cheap auth checks for scheduled Cycle launches."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.integrations.llm import async_resolve_llm_client
from brain.platform.providers.model_policy import (
    async_get_default_model,
    infer_provider_from_model,
    required_openai_auth_mode,
)


@dataclass(frozen=True)
class CycleAuthPreflightResult:
    status: str
    provider: str | None = None
    model: str | None = None
    credential: str | None = None
    repair_action: str | None = None
    visible_message: str | None = None

    @property
    def blocked(self) -> bool:
        return self.status == "auth_blocked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "credential": self.credential,
            "repair_action": self.repair_action,
            "visible_message": self.visible_message,
        }


def _normalize_model(model: str) -> str:
    value = str(model or "").strip()
    for prefix in ("anthropic/", "openai/", "anthropic:", "openai:"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _credential_label(model: str, auth_mode: str | None, error_text: str = "") -> str:
    normalized_model = _normalize_model(model).lower()
    normalized_error = error_text.lower()
    if auth_mode == "chatgpt" or "codex" in normalized_model or "codex" in normalized_error:
        return "OpenAI Codex / ChatGPT"
    return "OpenAI runtime"


def _blocked_message(credential: str) -> tuple[str, str]:
    repair_action = (
        "Reconnect OpenAI in Settings > Access by signing in to Codex / ChatGPT again, "
        "then rerun the Cycle or wait for its next scheduled run."
    )
    return (
        repair_action,
        f"Scheduled Cycle auth blocked: the {credential} credential is unavailable or could not be refreshed. {repair_action}",
    )


async def async_preflight_cycle_external_auth(
    session: AsyncSession,
    *,
    cycle: Any,
) -> CycleAuthPreflightResult:
    """Validate required external auth before admitting the full agent."""
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
    if provider != "openai":
        return CycleAuthPreflightResult(status="skipped", provider=provider, model=model)

    auth_mode = required_openai_auth_mode(model)
    try:
        await async_resolve_llm_client(
            user_id=user_id,
            org_id=org_id,
            provider=provider,
            auth_mode=auth_mode,
            session=session,
        )
    except Exception as exc:
        credential = _credential_label(model, auth_mode, str(exc))
        repair_action, visible_message = _blocked_message(credential)
        return CycleAuthPreflightResult(
            status="auth_blocked",
            provider=provider,
            model=model,
            credential=credential,
            repair_action=repair_action,
            visible_message=visible_message,
        )

    return CycleAuthPreflightResult(status="passed", provider=provider, model=model)
