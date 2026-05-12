from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import delete

from brain.platform.db.models.org import Org, User
from brain.platform.db.models.system import OrgProviderModelMapping
from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work
from brain.platform.providers.model_policy import DEFAULT_PROVIDER_MODEL_MAPS, get_provider_model_map

from .schemas import RuntimeModelsRead, RuntimeModelsUpdate, RuntimeOption

MODEL_TIERS = ("low", "medium", "high")

OPENAI_MODEL_OPTIONS = [
    RuntimeOption(key="gpt-5.5", label="GPT-5.5", description="Best quality for hard reasoning."),
    RuntimeOption(key="gpt-5.4-pro", label="GPT-5.4 Pro", description="Previous maximum-quality route."),
    RuntimeOption(key="gpt-5.4", label="GPT-5.4", description="Previous balanced route."),
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
    if ":" in value:
        provider, name = value.split(":", 1)
        if provider != "openai":
            raise HTTPException(status_code=400, detail="Only OpenAI models can be configured here")
        value = name
    return value


def _defaults() -> dict[str, str]:
    default_map = DEFAULT_PROVIDER_MODEL_MAPS.get("openai", {})
    return {
        "low": default_map.get("low", "gpt-5.4-mini"),
        "medium": default_map.get("medium", "gpt-5.4"),
        "high": default_map.get("high", "gpt-5.5"),
    }


def get_runtime_models(user: User) -> RuntimeModelsRead:
    models = _defaults()
    mapped = get_provider_model_map("openai", org_id=user.org_id, user_id=user.id)
    for tier in MODEL_TIERS:
        value = mapped.get(tier)
        if isinstance(value, str) and value:
            models[tier] = _normalize_model(value)
    return RuntimeModelsRead(**models, options=OPENAI_MODEL_OPTIONS)


def update_runtime_models(user: User, update: RuntimeModelsUpdate) -> RuntimeModelsRead:
    values = {tier: _normalize_model(getattr(update, tier)) for tier in MODEL_TIERS}
    with open_unit_of_work(UnitOfWork) as uow:
        uow.session.execute(
            delete(OrgProviderModelMapping).where(
                OrgProviderModelMapping.org_id == user.org_id,
                OrgProviderModelMapping.provider == "openai",
            )
        )
        for tier, model in values.items():
            uow.session.add(
                OrgProviderModelMapping(
                    org_id=user.org_id,
                    provider="openai",
                    intelligence_level=tier,
                    model_name=model,
                )
            )

        org = uow.session.get(Org, user.org_id)
        if org is not None:
            config = dict(org.memory_model_config or {})
            config["default_provider"] = "openai"
            for legacy_key in ("session_harvest", "depth_0", "depth_1_plus"):
                config.pop(legacy_key, None)
            org.memory_model_config = config
        uow.commit()
    return get_runtime_models(user)
