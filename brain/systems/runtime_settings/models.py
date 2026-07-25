from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.org import Org, User
from brain.platform.model_catalog import get_model_catalog_entry
from brain.platform.providers.model_policy import (
    EFFORT_TIERS,
    async_get_default_model,
    async_get_default_thinking,
    get_model_catalog_contract,
)

from .schemas import RuntimeModelsRead, RuntimeModelsUpdate, RuntimeOption

_THINKING_OPTION_DETAILS = {
    "none": ("None", "No additional reasoning effort."),
    "low": ("Low", "Faster responses with light reasoning."),
    "medium": ("Medium", "Balanced reasoning effort."),
    "high": ("High", "More reasoning for difficult work."),
    "xhigh": ("Extra High", "Maximum supported reasoning effort."),
}
THINKING_OPTIONS = [
    RuntimeOption(
        key=tier,
        label=_THINKING_OPTION_DETAILS[tier][0],
        description=_THINKING_OPTION_DETAILS[tier][1],
    )
    for tier in EFFORT_TIERS
]


def _normalize_model(model: str) -> str:
    value = model.strip()
    if not value:
        raise HTTPException(status_code=400, detail="Model values cannot be empty")
    entry = get_model_catalog_entry(value)
    if entry is None:
        raise HTTPException(status_code=400, detail=f"Unsupported model: {value}")
    return entry.id


async def async_get_runtime_models(session: AsyncSession, user: User) -> RuntimeModelsRead:
    default = await async_get_default_model(
        session,
        include_provider_prefix=True,
        org_id=user.org_id,
        user_id=user.id,
    )
    thinking = await async_get_default_thinking(
        session,
        org_id=user.org_id,
        user_id=user.id,
    )
    return RuntimeModelsRead(
        default=_normalize_model(default),
        thinking=thinking,
        catalog=get_model_catalog_contract(workspace_default=default),
        thinking_options=THINKING_OPTIONS,
    )


async def async_update_runtime_models(
    session: AsyncSession,
    user: User,
    update: RuntimeModelsUpdate,
) -> RuntimeModelsRead:
    model = _normalize_model(update.default)
    entry = get_model_catalog_entry(model)
    assert entry is not None
    if update.thinking is not None and update.thinking not in entry.supported_effort_tiers:
        raise HTTPException(
            status_code=400,
            detail=f"{entry.label} does not support {update.thinking} effort",
        )
    org = await session.get(Org, user.org_id)
    if org is not None:
        config = dict(org.memory_model_config or {})
        config["default_provider"] = entry.provider
        config["default_model"] = entry.id
        if update.thinking is not None:
            config["default_thinking"] = update.thinking
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
