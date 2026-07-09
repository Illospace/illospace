"""Shared provider-aware model and cost policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.integrations.providers import get_active_provider


DEFAULT_RUNTIME_PROVIDER = "openai"
DEFAULT_THINKING_TIER = "high"
DEFAULT_PROVIDER_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5.5",
}
PROVIDER_MODEL_OPTIONS: dict[str, tuple[str, ...]] = {
    "anthropic": (
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ),
    "openai": (
        "gpt-5.6-sol",
        "gpt-5.5",
        "gpt-5.4-pro",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5-mini",
        "gpt-5-nano",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
        "gpt-5.2",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4o",
        "gpt-4o-mini",
        "o3-mini",
    ),
}

THINKING_MAP = {
    "none": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
}

OPENAI_MODEL_ALIASES = set(PROVIDER_MODEL_OPTIONS["openai"])


@dataclass(frozen=True)
class SkillRuntimeConfig:
    """Resolved runtime settings for a skill."""

    provider: str
    model_name: str
    reasoning_effort: str
    thinking_tier: str = "medium"


@dataclass(frozen=True)
class ProviderResolution:
    """Resolved provider selection plus provenance metadata."""

    provider: str
    source: str
    explicit: bool = False


@dataclass(frozen=True)
class SkillRoutingProfile:
    """Raw skill routing inputs used by the marketplace layer."""

    skill_name: str
    reasoning_effort: str | None
    thinking_tier: str = "medium"


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


def _prefix_model(provider: str, model: str) -> str:
    if model.startswith(("anthropic/", "openai/")):
        return model
    if model.startswith("anthropic:"):
        return f"anthropic/{model[len('anthropic:'):]}"
    if model.startswith("openai:"):
        return f"openai/{model[len('openai:'):]}"
    return f"{provider}/{model}"


MODEL_PRICING_PER_MILLION: dict[str, dict[str, float]] = {
    "anthropic/claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "anthropic/claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "anthropic/claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "openai/gpt-4o": {"input": 5.0, "output": 15.0},
    "openai/gpt-4o-mini": {"input": 0.6, "output": 2.4},
    "openai/o3-mini": {"input": 1.1, "output": 4.4},
    "openai/gpt-5.5": {"input": 5.0, "output": 30.0},
    "openai/gpt-5.4": {"input": 2.5, "output": 15.0},
    "openai/gpt-5.4-pro": {"input": 30.0, "output": 180.0},
    "openai/gpt-5.4-mini": {"input": 0.75, "output": 4.5},
    "openai/gpt-5.4-nano": {"input": 0.05, "output": 0.4},
    "openai/gpt-5-mini": {"input": 0.25, "output": 2.0},
    "openai/gpt-5-nano": {"input": 0.05, "output": 0.4},
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


def _load_skill_routing_row(skill_name: str) -> dict[str, str | None] | None:
    del skill_name
    return None


async def _async_load_skill_routing_row(
    session: AsyncSession,
    skill_name: str,
) -> dict[str, str | None] | None:
    try:
        row = (
            await session.execute(
                text(
                    """
                    SELECT thinking_tier
                    FROM skills
                    WHERE name = :name AND NOT archived
                    LIMIT 1
                    """
                ),
                {"name": skill_name},
            )
        ).mappings().first()
        return dict(row) if row else None
    except Exception:
        return None


def _resolve_effective_org_id(
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> str | None:
    del user_id
    if org_id:
        return org_id
    return None


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
                return ProviderResolution(provider=normalize_default_provider(org_provider), source="org_default_provider", explicit=True)
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
    return value if value in THINKING_MAP else None


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


def resolve_skill_runtime(
    skill_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    preferred_provider: str | None = None,
) -> SkillRuntimeConfig:
    """Resolve the native runtime configuration for a skill."""
    provider = resolve_default_provider(
        user_id=user_id,
        org_id=org_id,
        fallback=preferred_provider,
        preferred_provider=preferred_provider,
    )
    row = _load_skill_routing_row(skill_name) or {}
    thinking_tier = row.get("thinking_tier") or "medium"
    model_name = get_default_model(
        provider,
        include_provider_prefix=False,
        user_id=user_id,
        org_id=org_id,
    )
    reasoning_effort = THINKING_MAP.get(thinking_tier, "medium")

    return SkillRuntimeConfig(
        provider=provider,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
        thinking_tier=thinking_tier,
    )


async def async_resolve_skill_runtime(
    session: AsyncSession,
    skill_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    preferred_provider: str | None = None,
) -> SkillRuntimeConfig:
    """Resolve the native runtime configuration for a skill using an async session."""
    provider = await async_resolve_default_provider(
        session,
        user_id=user_id,
        org_id=org_id,
        fallback=preferred_provider,
        preferred_provider=preferred_provider,
    )
    row = await _async_load_skill_routing_row(session, skill_name) or {}
    thinking_tier = row.get("thinking_tier") or "medium"
    model_name = await async_get_default_model(
        session,
        provider,
        include_provider_prefix=False,
        user_id=user_id,
        org_id=org_id,
    )
    reasoning_effort = THINKING_MAP.get(thinking_tier, "medium")

    return SkillRuntimeConfig(
        provider=provider,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
        thinking_tier=thinking_tier,
    )


def resolve_skill_routing_profile(
    skill_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    preferred_provider: str | None = None,
) -> SkillRoutingProfile:
    """Return the raw skill routing profile for marketplace scoring."""
    del user_id, org_id, preferred_provider
    row = _load_skill_routing_row(skill_name) or {}
    thinking_tier = row.get("thinking_tier") or "medium"
    return SkillRoutingProfile(
        skill_name=skill_name,
        reasoning_effort=THINKING_MAP.get(thinking_tier, "medium"),
        thinking_tier=thinking_tier,
    )


async def async_resolve_skill_routing_profile(
    session: AsyncSession,
    skill_name: str,
) -> SkillRoutingProfile:
    """Return the raw skill routing profile using an async session."""
    row = await _async_load_skill_routing_row(session, skill_name) or {}
    thinking_tier = row.get("thinking_tier") or "medium"
    return SkillRoutingProfile(
        skill_name=skill_name,
        reasoning_effort=THINKING_MAP.get(thinking_tier, "medium"),
        thinking_tier=thinking_tier,
    )


def resolve_skill_model(
    skill_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    preferred_provider: str | None = None,
) -> tuple[str, str]:
    """Resolve (provider/model, thinking) from skill runtime settings."""
    runtime = resolve_skill_runtime(
        skill_name,
        user_id=user_id,
        org_id=org_id,
        preferred_provider=preferred_provider,
    )
    return _prefix_model(runtime.provider, runtime.model_name), runtime.reasoning_effort


async def async_resolve_skill_model(
    session: AsyncSession,
    skill_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    preferred_provider: str | None = None,
) -> tuple[str, str]:
    """Resolve (provider/model, thinking) from skill runtime settings using an async session."""
    runtime = await async_resolve_skill_runtime(
        session,
        skill_name,
        user_id=user_id,
        org_id=org_id,
        preferred_provider=preferred_provider,
    )
    return _prefix_model(runtime.provider, runtime.model_name), runtime.reasoning_effort


def normalize_model_name(model: str | None) -> str:
    """Normalize a model string to the canonical priced identifier."""
    if not model:
        return "openai/gpt-5.5"

    value = model.strip()
    lower = value.lower()
    if "gpu_server" in lower or "local" in lower or value.startswith("brain.platform.gpu/"):
        return "local"

    if value.startswith("anthropic:"):
        return f"anthropic/{value[len('anthropic:'):]}"
    if value.startswith("openai:"):
        return f"openai/{value[len('openai:'):]}"

    for prefix in ("anthropic/", "openai/"):
        if value.startswith(prefix):
            return value

    if value in OPENAI_MODEL_ALIASES:
        return f"openai/{value}"

    for provider, model_options in PROVIDER_MODEL_OPTIONS.items():
        if value in model_options:
            return f"{provider}/{value}"

    if lower.startswith("claude-"):
        if "opus" in lower:
            return "anthropic/claude-opus-4-6"
        if "haiku" in lower:
            return "anthropic/claude-haiku-4-5"
        if "sonnet" in lower:
            return "anthropic/claude-sonnet-4-6"
    if "gpt-4o-mini" in lower:
        return "openai/gpt-4o-mini"
    if "gpt-4o" in lower:
        return "openai/gpt-4o"
    if "gpt-5.5" in lower:
        return "openai/gpt-5.5"
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
    if "o3-mini" in lower:
        return "openai/o3-mini"
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
