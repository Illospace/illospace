"""Neutral provider credential probing for admission boundaries."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Self, TypeAlias

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.integrations.llm import async_resolve_llm_client
from brain.platform.providers.model_policy import required_openai_auth_mode


@dataclass(frozen=True, slots=True, kw_only=True)
class _ProviderAuthPreflightResult:
    provider: str
    model: str

    def _to_dict(
        self,
        *,
        status: str,
        credential: str | None = None,
        error_code: str | None = None,
        repair_action: str | None = None,
        visible_message: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "provider": self.provider,
            "model": self.model,
            "credential": credential,
            "error_code": error_code,
            "repair_action": repair_action,
            "visible_message": visible_message,
        }


class ProviderAuthPassedPreflightResult(_ProviderAuthPreflightResult):
    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        return self._to_dict(status="passed")


class ProviderAuthSkippedPreflightResult(_ProviderAuthPreflightResult):
    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        return self._to_dict(status="skipped")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderAuthBlockedPreflightResult(_ProviderAuthPreflightResult):
    credential: str
    error_code: str
    repair_action: str | None = None
    visible_message: str | None = None

    def with_presentation(
        self,
        *,
        repair_action: str,
        visible_message: str,
    ) -> Self:
        return replace(
            self,
            repair_action=repair_action,
            visible_message=visible_message,
        )

    def to_dict(self) -> dict[str, Any]:
        return self._to_dict(
            status="auth_blocked",
            credential=self.credential,
            error_code=self.error_code,
            repair_action=self.repair_action,
            visible_message=self.visible_message,
        )


ProviderAuthPreflightResult: TypeAlias = (
    ProviderAuthPassedPreflightResult
    | ProviderAuthSkippedPreflightResult
    | ProviderAuthBlockedPreflightResult
)


def skipped_provider_auth_preflight(
    *,
    provider: str,
    model: str,
) -> ProviderAuthSkippedPreflightResult:
    return ProviderAuthSkippedPreflightResult(
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
        return ProviderAuthBlockedPreflightResult(
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

    return ProviderAuthPassedPreflightResult(
        provider=provider,
        model=model,
    )


__all__ = [
    "ProviderAuthBlockedPreflightResult",
    "ProviderAuthPassedPreflightResult",
    "ProviderAuthPreflightResult",
    "ProviderAuthSkippedPreflightResult",
    "async_probe_provider_auth",
    "skipped_provider_auth_preflight",
]
