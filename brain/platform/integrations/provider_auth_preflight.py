"""Neutral provider credential probing for admission boundaries."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.integrations.llm import async_resolve_llm_client
from brain.platform.providers.model_policy import required_openai_auth_mode


@dataclass(frozen=True, slots=True)
class ProviderAuthPreflightResult:
    status: str
    provider: str
    model: str
    credential: str | None = None
    error_code: str | None = None
    repair_action: str | None = None
    visible_message: str | None = None

    @property
    def blocked(self) -> bool:
        return self.status == "auth_blocked"

    def with_presentation(
        self,
        *,
        repair_action: str,
        visible_message: str,
    ) -> ProviderAuthPreflightResult:
        return replace(
            self,
            repair_action=repair_action,
            visible_message=visible_message,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "credential": self.credential,
            "error_code": self.error_code,
            "repair_action": self.repair_action,
            "visible_message": self.visible_message,
        }


def skipped_provider_auth_preflight(
    *,
    provider: str,
    model: str,
) -> ProviderAuthPreflightResult:
    return ProviderAuthPreflightResult(
        status="skipped",
        provider=provider,
        model=model,
    )


def _normalize_model(model: str) -> str:
    value = str(model or "").strip()
    for prefix in ("anthropic/", "openai/", "anthropic:", "openai:"):
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def _credential_label(
    *,
    provider: str,
    model: str,
    auth_mode: str | None,
    error_text: str,
) -> str:
    normalized_model = _normalize_model(model).lower()
    normalized_error = error_text.lower()
    if provider == "anthropic":
        return "Anthropic API key"
    if (
        auth_mode == "chatgpt"
        or "codex" in normalized_model
        or "codex" in normalized_error
    ):
        return "OpenAI Codex / ChatGPT"
    return "OpenAI runtime"


async def async_probe_provider_auth(
    session: AsyncSession,
    *,
    user_id: str | None,
    org_id: str | None,
    provider: str,
    model: str,
) -> ProviderAuthPreflightResult:
    """Probe one caller-approved provider route without presentation policy."""

    if provider not in {"anthropic", "openai"}:
        raise ValueError(f"Unsupported provider auth probe: {provider}")

    auth_mode = required_openai_auth_mode(model) if provider == "openai" else None
    try:
        await async_resolve_llm_client(
            user_id=user_id,
            org_id=org_id,
            provider=provider,
            auth_mode=auth_mode,
            session=session,
        )
    except Exception as exc:
        return ProviderAuthPreflightResult(
            status="auth_blocked",
            provider=provider,
            model=model,
            credential=_credential_label(
                provider=provider,
                model=model,
                auth_mode=auth_mode,
                error_text=str(exc),
            ),
            error_code="provider_credential_unavailable",
        )

    return ProviderAuthPreflightResult(
        status="passed",
        provider=provider,
        model=model,
    )


__all__ = [
    "ProviderAuthPreflightResult",
    "async_probe_provider_auth",
    "skipped_provider_auth_preflight",
]
