"""Neutral provider credential probing for admission boundaries."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Self

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.integrations.llm import async_resolve_llm_client
from brain.platform.providers.model_policy import required_openai_auth_mode


class ProviderAuthPreflightStatus(StrEnum):
    AUTH_BLOCKED = "auth_blocked"
    PASSED = "passed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderAuthPreflightResult(ABC):
    provider: str
    model: str

    @property
    @abstractmethod
    def status(self) -> ProviderAuthPreflightStatus:
        raise NotImplementedError

    @property
    def blocked(self) -> bool:
        return self.status is ProviderAuthPreflightStatus.AUTH_BLOCKED

    @abstractmethod
    def _details_dict(self) -> dict[str, str | None]:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            **self._details_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class _ProviderAuthAvailablePreflightResult(ProviderAuthPreflightResult):
    def _details_dict(self) -> dict[str, str | None]:
        return {
            "credential": None,
            "error_code": None,
            "repair_action": None,
            "visible_message": None,
        }


class ProviderAuthPassedPreflightResult(_ProviderAuthAvailablePreflightResult):
    __slots__ = ()

    @property
    def status(self) -> ProviderAuthPreflightStatus:
        return ProviderAuthPreflightStatus.PASSED


class ProviderAuthSkippedPreflightResult(_ProviderAuthAvailablePreflightResult):
    __slots__ = ()

    @property
    def status(self) -> ProviderAuthPreflightStatus:
        return ProviderAuthPreflightStatus.SKIPPED


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderAuthBlockedPreflightResult(ProviderAuthPreflightResult):
    credential: str
    error_code: str
    repair_action: str | None = None
    visible_message: str | None = None

    @property
    def status(self) -> ProviderAuthPreflightStatus:
        return ProviderAuthPreflightStatus.AUTH_BLOCKED

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

    def _details_dict(self) -> dict[str, str | None]:
        return {
            "credential": self.credential,
            "error_code": self.error_code,
            "repair_action": self.repair_action,
            "visible_message": self.visible_message,
        }


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
    "ProviderAuthPreflightStatus",
    "ProviderAuthSkippedPreflightResult",
    "async_probe_provider_auth",
    "skipped_provider_auth_preflight",
]
