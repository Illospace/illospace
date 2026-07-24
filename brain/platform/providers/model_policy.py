"""Shared provider-aware model and cost policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.effort import (
    EFFORT_TIER_SET,
    EFFORT_TIERS,
    PROVIDER_EFFORT_RENDERINGS,
    render_reasoning_effort,
)
from brain.platform.integrations.providers import get_active_provider
from brain.platform.model_catalog import (
    MODEL_CATALOG,
    canonical_catalog_model_id,
)


DEFAULT_RUNTIME_PROVIDER = "openai"
DEFAULT_THINKING_TIER = "high"
DEFAULT_PROVIDER_MODELS: dict[str, str] = {
    entry.provider: entry.model_name
    for entry in MODEL_CATALOG
    if entry.provider_default
}
PROVIDER_MODEL_OPTIONS: dict[str, tuple[str, ...]] = {
    provider: tuple(
        entry.model_name
        for entry in MODEL_CATALOG
        if entry.provider == provider
    )
    for provider in DEFAULT_PROVIDER_MODELS
}

OPENAI_MODEL_ALIASES = set(PROVIDER_MODEL_OPTIONS["openai"])


@dataclass(frozen=True)
class ProviderResolution:
    """Resolved provider selection plus provenance metadata."""

    provider: str
    source: str
    explicit: bool = False


def _strip_provider_prefix(model: str | None) -> str | None:
    if not model:
        return None
    value = str(model).strip()
    for provider in DEFAULT_PROVIDER_MODELS:
        for separator in ("/", ":"):
            prefix = f"{provider}{separator}"
            if value.startswith(prefix):
                return value[len(prefix):]
    return value


def _explicit_provider_prefix(model: str | None) -> str | None:
    value = str(model or "").strip().lower()
    for provider in DEFAULT_PROVIDER_MODELS:
        if value.startswith((f"{provider}/", f"{provider}:")):
            return provider
    return None


def normalize_runtime_provider(provider: str | None = None) -> str:
    """Return a supported provider name, preserving explicit provider choices."""
    normalized = (provider or "").strip().lower()
    if normalized in DEFAULT_PROVIDER_MODELS:
        return normalized
    return DEFAULT_RUNTIME_PROVIDER


def normalize_default_provider(provider: str | None = None) -> str:
    """Return the provider used for implicit/default runtime selection."""
    normalized = (provider or "").strip().lower()
    if normalized == "openai":
        return "openai"
    return DEFAULT_RUNTIME_PROVIDER


MODEL_PRICING_PER_MILLION: dict[str, dict[str, float]] = {
    entry.id: {
        "input": entry.input_price_per_million,
        "output": entry.output_price_per_million,
    }
    for entry in MODEL_CATALOG
}


def get_provider_model_options(provider: str | None = None) -> list[str]:
    """Return selectable concrete models for a provider."""
    provider = normalize_runtime_provider(provider or get_active_provider())
    return list(PROVIDER_MODEL_OPTIONS.get(provider, PROVIDER_MODEL_OPTIONS[DEFAULT_RUNTIME_PROVIDER]))


async def async_get_provider_model_options(
    session: AsyncSession,
    provider: str | None = None,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> list[str]:
    """Return selectable concrete models for a provider using async call shape."""
    del session, user_id, org_id
    return get_provider_model_options(provider)


def get_provider_model_catalogs() -> dict[str, dict[str, Any]]:
    """Return selectable provider model catalogs for UI and introspection."""
    return {
        provider: {
            "default": DEFAULT_PROVIDER_MODELS[provider],
            "options": list(options),
        }
        for provider, options in PROVIDER_MODEL_OPTIONS.items()
    }


async def async_get_provider_model_catalogs(
    session: AsyncSession,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return provider model catalogs using async call shape."""
    del session, user_id, org_id
    return get_provider_model_catalogs()


async def async_resolve_effective_org_id(
    session: AsyncSession,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> str | None:
    if org_id:
        return org_id
    if not user_id:
        return None
    try:
        row = (
            await session.execute(
                text("SELECT org_id FROM users WHERE id = :user_id LIMIT 1"),
                {"user_id": user_id},
            )
        ).mappings().first()
        return row.get("org_id") if row else None
    except Exception:
        return None


def resolve_provider_selection(
    user_id: str | None = None,
    org_id: str | None = None,
    *,
    fallback: str | None = None,
    preferred_provider: str | None = None,
) -> ProviderResolution:
    """Resolve the effective provider and preserve where it came from."""
    del user_id, org_id
    preferred = (preferred_provider or "").strip().lower()
    fallback_provider = normalize_default_provider(fallback or get_active_provider())
    if preferred in DEFAULT_PROVIDER_MODELS:
        return ProviderResolution(provider=normalize_runtime_provider(preferred), source="preferred_provider", explicit=False)
    return ProviderResolution(provider=fallback_provider, source="fallback", explicit=False)


async def async_resolve_provider_selection(
    session: AsyncSession,
    user_id: str | None = None,
    org_id: str | None = None,
    *,
    fallback: str | None = None,
    preferred_provider: str | None = None,
) -> ProviderResolution:
    """Resolve the effective provider using an async session."""
    preferred = (preferred_provider or "").strip().lower()
    fallback_provider = normalize_default_provider(fallback or get_active_provider())
    try:
        effective_org_id = await async_resolve_effective_org_id(
            session,
            user_id=user_id,
            org_id=org_id,
        )
        if effective_org_id:
            org_row = (
                await session.execute(
                    text("SELECT memory_model_config FROM orgs WHERE id = :org_id LIMIT 1"),
                    {"org_id": effective_org_id},
                )
            ).mappings().first()
            config = (org_row or {}).get("memory_model_config") or {}
            org_provider = (config.get("default_provider") or "").strip().lower()
            if org_provider in DEFAULT_PROVIDER_MODELS:
                return ProviderResolution(
                    provider=normalize_runtime_provider(org_provider),
                    source="org_default_provider",
                    explicit=True,
                )
    except Exception:
        pass

    if preferred in DEFAULT_PROVIDER_MODELS:
        return ProviderResolution(provider=normalize_runtime_provider(preferred), source="preferred_provider", explicit=False)
    return ProviderResolution(provider=fallback_provider, source="fallback", explicit=False)


def resolve_default_provider(
    user_id: str | None = None,
    org_id: str | None = None,
    *,
    fallback: str | None = None,
    preferred_provider: str | None = None,
) -> str:
    """Resolve the effective provider for a user/org context."""
    return resolve_provider_selection(
        user_id=user_id,
        org_id=org_id,
        fallback=fallback,
        preferred_provider=preferred_provider,
    ).provider


async def async_resolve_default_provider(
    session: AsyncSession,
    user_id: str | None = None,
    org_id: str | None = None,
    *,
    fallback: str | None = None,
    preferred_provider: str | None = None,
) -> str:
    """Resolve the effective provider for a user/org context using an async session."""
    return (
        await async_resolve_provider_selection(
            session,
            user_id=user_id,
            org_id=org_id,
            fallback=fallback,
            preferred_provider=preferred_provider,
        )
    ).provider


def required_openai_auth_mode(model: str | None) -> str | None:
    """Return the OpenAI auth mode a model requires, or None when either works.

    GPT-5.5 and every GPT-5.6 variant are only reachable through the
    ChatGPT/Codex subscription backend, never an API key. Callers pass the
    result to credential resolution so a run validates and uses the credential
    it will actually need.
    """
    value = str(model or "").strip().lower()
    for prefix in ("anthropic/", "openai/", "anthropic:", "openai:"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return "chatgpt" if value == "gpt-5.5" or value.startswith("gpt-5.6") else None


def get_model_catalog_contract(
    *,
    workspace_default: str | None = None,
) -> list[dict[str, Any]]:
    """Return the provider-aware model contract served to runtime pickers."""

    canonical_workspace_default = canonical_catalog_model_id(workspace_default)
    return [
        {
            "id": entry.id,
            "label": entry.label,
            "provider": entry.provider,
            "description": entry.description,
            "supported_effort_tiers": list(entry.supported_effort_tiers),
            "auth_requirement": (
                "chatgpt"
                if required_openai_auth_mode(entry.id) == "chatgpt"
                else "api_key"
            ),
            "availability_fallback": entry.availability_fallback,
            "default_provenance": {
                "provider_default": entry.provider_default,
                "workspace_default": entry.id == canonical_workspace_default,
            },
        }
        for entry in MODEL_CATALOG
    ]


def infer_provider_from_model(model: str | None, default: str | None = None) -> str:
    """Infer provider from a model string, falling back to the active provider."""
    if model:
        value = model.strip()
        lowered = value.lower()
        if value.startswith("anthropic:"):
            return "anthropic"
        if value.startswith("openai:"):
            return "openai"
        if value.startswith("anthropic/"):
            return "anthropic"
        if value.startswith("openai/"):
            return "openai"
        if lowered.startswith("claude-"):
            return "anthropic"
        if lowered.startswith(("gpt-", "o1", "o3", "o4")):
            return "openai"
        for provider, model_options in PROVIDER_MODEL_OPTIONS.items():
            if value in model_options:
                return provider
        if value in OPENAI_MODEL_ALIASES:
            return "openai"
    return normalize_default_provider(default or get_active_provider())


def _configured_default_model(config: dict[str, Any], provider: str) -> str | None:
    for key in ("default_model", "model"):
        model = config.get(key)
        if not isinstance(model, str) or not model.strip():
            continue
        explicit_provider = _explicit_provider_prefix(model)
        if explicit_provider and explicit_provider != provider:
            continue
        return _strip_provider_prefix(model)
    return None


def _configured_default_thinking(config: dict[str, Any]) -> str | None:
    value = str(config.get("default_thinking") or "").strip().lower()
    return value if value in EFFORT_TIER_SET else None


def get_default_model(
    provider: str | None = None,
    *,
    include_provider_prefix: bool = True,
    user_id: str | None = None,
    org_id: str | None = None,
) -> str:
    """Return the default concrete model for a provider."""
    del user_id, org_id
    provider = normalize_runtime_provider(provider or resolve_default_provider())
    model = DEFAULT_PROVIDER_MODELS.get(provider, DEFAULT_PROVIDER_MODELS[DEFAULT_RUNTIME_PROVIDER])
    return f"{provider}/{model}" if include_provider_prefix else model


async def async_get_default_model(
    session: AsyncSession,
    provider: str | None = None,
    *,
    include_provider_prefix: bool = True,
    user_id: str | None = None,
    org_id: str | None = None,
) -> str:
    """Return the default concrete model using org runtime settings when present."""
    provider = normalize_runtime_provider(
        provider or await async_resolve_default_provider(session, user_id=user_id, org_id=org_id)
    )
    model = DEFAULT_PROVIDER_MODELS.get(provider, DEFAULT_PROVIDER_MODELS[DEFAULT_RUNTIME_PROVIDER])
    effective_org_id = await async_resolve_effective_org_id(session, user_id=user_id, org_id=org_id)
    if effective_org_id:
        try:
            row = (
                await session.execute(
                    text("SELECT memory_model_config FROM orgs WHERE id = :org_id LIMIT 1"),
                    {"org_id": effective_org_id},
                )
            ).mappings().first()
            config = dict((row or {}).get("memory_model_config") or {})
            model = _configured_default_model(config, provider) or model
        except Exception:
            pass
    return f"{provider}/{model}" if include_provider_prefix else model


async def async_get_default_thinking(
    session: AsyncSession,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> str:
    """Return the workspace default reasoning effort."""
    effective_org_id = await async_resolve_effective_org_id(
        session,
        user_id=user_id,
        org_id=org_id,
    )
    if effective_org_id:
        try:
            row = (
                await session.execute(
                    text("SELECT memory_model_config FROM orgs WHERE id = :org_id LIMIT 1"),
                    {"org_id": effective_org_id},
                )
            ).mappings().first()
            configured = _configured_default_thinking(
                dict((row or {}).get("memory_model_config") or {})
            )
            if configured:
                return configured
        except Exception:
            pass
    return DEFAULT_THINKING_TIER


def normalize_model_name(model: str | None) -> str:
    """Normalize a model string to the canonical priced identifier."""
    if not model:
        return "openai/gpt-5.5"

    value = model.strip()
    lower = value.lower()
    if "gpu_server" in lower or "local" in lower or value.startswith("brain.platform.gpu/"):
        return "local"

    catalog_id = canonical_catalog_model_id(value)
    if catalog_id:
        return catalog_id
    explicitly_prefixed = value.replace(":", "/", 1)
    if explicitly_prefixed.startswith(("anthropic/", "openai/")):
        return explicitly_prefixed

    if lower.startswith("claude-"):
        if "fable" in lower:
            return "anthropic/claude-fable-5"
        if "opus" in lower:
            return "anthropic/claude-opus-5"
        if "haiku" in lower:
            return "anthropic/claude-haiku-4-5"
        if "sonnet" in lower:
            return "anthropic/claude-sonnet-5"
    if "gpt-5.5" in lower:
        return "openai/gpt-5.5"
    if "gpt-5.6-sol" in lower:
        return "openai/gpt-5.6-sol"
    if "gpt-5.4-mini" in lower:
        return "openai/gpt-5.4-mini"
    if "gpt-5.4-nano" in lower:
        return "openai/gpt-5.4-nano"
    if "gpt-5.4-pro" in lower:
        return "openai/gpt-5.4-pro"
    if "gpt-5.4" in lower:
        return "openai/gpt-5.4"
    if "gpt-5-mini" in lower:
        return "openai/gpt-5-mini"
    if "gpt-5-nano" in lower:
        return "openai/gpt-5-nano"
    return "openai/gpt-5.5"


def calculate_model_cost(
    model: str,
    tokens_input: int,
    tokens_output: int,
    *,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    """Calculate estimated cost for a model run."""
    normalized_model = normalize_model_name(model)
    if normalized_model == "local":
        return 0.0

    rates = MODEL_PRICING_PER_MILLION.get(
        normalized_model,
        MODEL_PRICING_PER_MILLION["openai/gpt-5.5"],
    )
    cached_input_tokens = max(0, min(int(cache_read or 0), int(tokens_input or 0)))
    uncached_input_tokens = max(0, int(tokens_input or 0) - cached_input_tokens)
    input_cost = (uncached_input_tokens / 1_000_000.0) * rates["input"]
    output_cost = (tokens_output / 1_000_000.0) * rates["output"]
    cache_read_cost = (cached_input_tokens / 1_000_000.0) * rates["input"] * 0.10
    cache_write_cost = (cache_write / 1_000_000.0) * rates["input"] * 1.25
    return round(input_cost + output_cost + cache_read_cost + cache_write_cost, 6)
