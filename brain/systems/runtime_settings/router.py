from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.platform.db.models.org import User

from .auth import (
    async_connect_gemini_api_key,
    async_connect_openai_api_key,
    async_connect_openai_embedding_api_key,
    async_exchange_openai_oauth,
    start_openai_oauth,
)
from .memory import async_check_runtime_memory, async_update_runtime_memory
from .models import async_update_runtime_models
from .schemas import (
    OpenAIKeyConnectRequest,
    OpenAIOAuthExchangeRequest,
    OpenAIOAuthStartResponse,
    RuntimeConnectionRead,
    RuntimeMemoryCheckRead,
    RuntimeMemoryRead,
    RuntimeMemoryUpdate,
    RuntimeModelsRead,
    RuntimeModelsUpdate,
    RuntimeSettingsRead,
    RuntimeUpdateRead,
    RuntimeVoiceRead,
    RuntimeVoiceSessionRead,
    RuntimeVoiceUpdate,
)
from .service import async_get_runtime_settings, can_manage_runtime_settings
from .self_update import async_get_runtime_update_status, async_start_runtime_update
from .voice import async_create_runtime_voice_session, async_update_runtime_voice

router = APIRouter(prefix="/api/runtime-settings", tags=["runtime-settings"], dependencies=[Depends(rate_limit)])
_OPENAI_OAUTH_CALLBACK_START_MODES = {"auto", "server", "local_bridge"}


async def _runtime_user(
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    runtime_user = await db.get(User, str(user_id))
    if not runtime_user:
        raise HTTPException(status_code=404, detail="User not found")
    return runtime_user


def _require_settings_admin(user: User) -> None:
    if not can_manage_runtime_settings(user):
        raise HTTPException(status_code=403, detail="You need owner or admin access to manage runtime settings")


@router.get("", response_model=RuntimeSettingsRead)
async def read_runtime_settings(
    user: User = Depends(_runtime_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeSettingsRead:
    return await async_get_runtime_settings(db, user)


@router.post("/connection/openai/api-key", response_model=RuntimeConnectionRead)
async def connect_openai_key(
    payload: OpenAIKeyConnectRequest,
    user: User = Depends(_runtime_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeConnectionRead:
    return await async_connect_openai_api_key(db, user, payload.api_key)


@router.post("/connection/openai/embedding-api-key", response_model=RuntimeMemoryRead)
async def connect_openai_embedding_key(
    payload: OpenAIKeyConnectRequest,
    user: User = Depends(_runtime_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeMemoryRead:
    return await async_connect_openai_embedding_api_key(db, user, payload.api_key)


@router.post("/connection/gemini/api-key", response_model=RuntimeMemoryRead)
async def connect_gemini_key(
    payload: OpenAIKeyConnectRequest,
    user: User = Depends(_runtime_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeMemoryRead:
    return await async_connect_gemini_api_key(db, user, payload.api_key)


@router.post("/connection/openai/oauth/start", response_model=OpenAIOAuthStartResponse)
async def start_openai_connection(
    request: Request,
    user: User = Depends(_runtime_user),
) -> dict[str, object]:
    callback_mode = await _openai_oauth_start_callback_mode(request)
    return start_openai_oauth(request, callback_mode=callback_mode)


async def _openai_oauth_start_callback_mode(request: Request) -> str:
    try:
        raw = await request.body()
    except Exception:
        return "auto"
    if not raw or not raw.strip():
        return "auto"
    try:
        payload = await request.json()
    except Exception:
        return "auto"
    if not isinstance(payload, dict):
        return "auto"
    return _normalize_openai_oauth_start_callback_mode(payload.get("callback_mode"))


def _normalize_openai_oauth_start_callback_mode(value: object) -> str:
    if isinstance(value, str):
        mode = value.strip()
        if mode in _OPENAI_OAUTH_CALLBACK_START_MODES:
            return mode
    return "auto"


@router.post("/connection/openai/oauth/exchange", response_model=RuntimeConnectionRead)
async def exchange_openai_connection(
    payload: OpenAIOAuthExchangeRequest,
    request: Request,
    user: User = Depends(_runtime_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeConnectionRead:
    return await async_exchange_openai_oauth(db, request, user, payload.callback)


@router.patch("/models", response_model=RuntimeModelsRead)
async def save_runtime_models(
    payload: RuntimeModelsUpdate,
    user: User = Depends(_runtime_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeModelsRead:
    _require_settings_admin(user)
    return await async_update_runtime_models(db, user, payload)


@router.patch("/memory", response_model=RuntimeMemoryRead)
async def save_runtime_memory(
    payload: RuntimeMemoryUpdate,
    user: User = Depends(_runtime_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeMemoryRead:
    _require_settings_admin(user)
    return await async_update_runtime_memory(db, user, payload)


@router.post("/memory/check", response_model=RuntimeMemoryCheckRead)
async def check_memory(
    user: User = Depends(_runtime_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeMemoryCheckRead:
    _require_settings_admin(user)
    return await async_check_runtime_memory(db, user)


@router.post("/voice/session", response_model=RuntimeVoiceSessionRead)
async def create_voice_session(
    user: User = Depends(_runtime_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeVoiceSessionRead:
    return await async_create_runtime_voice_session(db, user)


@router.patch("/voice", response_model=RuntimeVoiceRead)
async def save_runtime_voice(
    payload: RuntimeVoiceUpdate,
    user: User = Depends(_runtime_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeVoiceRead:
    _require_settings_admin(user)
    return await async_update_runtime_voice(db, user, payload)


@router.get("/update", response_model=RuntimeUpdateRead)
async def read_runtime_update(
    user: User = Depends(_runtime_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeUpdateRead:
    return await async_get_runtime_update_status(db)


@router.post("/update", response_model=RuntimeUpdateRead)
async def start_illospace_update(
    user: User = Depends(_runtime_user),
    db: AsyncSession = Depends(get_db),
) -> RuntimeUpdateRead:
    return await async_start_runtime_update(db, requested_by=str(user.id))
