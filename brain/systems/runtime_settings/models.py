from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.org import Org, User
from brain.platform.providers.model_policy import (
    PROVIDER_MODEL_OPTIONS,
    async_get_default_model,
)

from .schemas import RuntimeModelsRead, RuntimeModelsUpdate, RuntimeOption

OPENAI_MODEL_OPTIONS = [
    RuntimeOption(key="gpt-5.5", label="GPT-5.5", description="Default model for hard reasoning."),
    RuntimeOption(key="gpt-5.4-pro", label="GPT-5.4 Pro", description="Previous maximum-quality route."),
    RuntimeOption(key="gpt-5.4", label="GPT-5.4", description="Balanced general-purpose model."),
    RuntimeOption(key="gpt-5.4-mini", label="GPT-5.4 Mini", description="Fast and economical for lighter tasks."),
    RuntimeOption(key="gpt-5-mini", label="GPT-5 Mini", description="Low-cost route."),
    RuntimeOption(key="gpt-5-nano", label="GPT-5 Nano", description="Smallest low-latency route."),
    RuntimeOption(key="gpt-5.3-codex", label="GPT-5.3 Codex", description="Coding-optimized model."),
    RuntimeOption(key="gpt-5.3-codex-spark", label="GPT-5.3 Codex Spark", description="Fast coding model."),
    RuntimeOption(key="gpt-5.2", label="GPT-5.2", description="Stable professional-work model."),
    RuntimeOption(key="gpt-4.1", label="GPT-4.1", description="Legacy high-quality fallback."),
    RuntimeOption(key="gpt-4.1-mini", label="GPT-4.1 Mini", description="Legacy lightweight fallback."),
]


def _normalize_model(model: str) -> str:
    value = model.strip()
    if not value:
        raise HTTPException(status_code=400, detail="Model values cannot be empty")
    if ":" in value or "/" in value:
        separator = ":" if ":" in value else "/"
        provider, name = value.split(separator, 1)
        if provider != "openai":
            raise HTTPException(status_code=400, detail="Only OpenAI models can be configured here")
        value = name
    if value not in PROVIDER_MODEL_OPTIONS["openai"]:
        raise HTTPException(status_code=400, detail=f"Unsupported OpenAI model: {value}")
    return value


async def async_get_runtime_models(session: AsyncSession, user: User) -> RuntimeModelsRead:
    default = await async_get_default_model(
        session,
        "openai",
        include_provider_prefix=False,
        org_id=user.org_id,
        user_id=user.id,
    )
    return RuntimeModelsRead(default=_normalize_model(default), options=OPENAI_MODEL_OPTIONS)


async def async_update_runtime_models(
    session: AsyncSession,
    user: User,
    update: RuntimeModelsUpdate,
) -> RuntimeModelsRead:
    model = _normalize_model(update.default)
    org = await session.get(Org, user.org_id)
    if org is not None:
        config = dict(org.memory_model_config or {})
        config["default_provider"] = "openai"
        config["default_model"] = f"openai/{model}"
        for stale_key in (
            "session_harvest",
            "depth_0",
            "depth_1_plus",
            "low",
            "medium",
            "high",
        ):
            config.pop(stale_key, None)
        org.memory_model_config = config
    await session.flush()
    return await async_get_runtime_models(session, user)
