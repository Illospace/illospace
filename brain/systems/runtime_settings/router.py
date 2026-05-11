from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from brain.app.api.auth import get_current_user
from brain.app.api.deps import rate_limit
from brain.platform.db.models.org import User

from .auth import (
    connect_gemini_api_key,
    connect_openai_api_key,
    connect_openai_embedding_api_key,
    exchange_openai_oauth,
    refresh_user,
    start_openai_oauth,
)
from .memory import check_runtime_memory, update_runtime_memory
from .models import update_runtime_models
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
)
from .service import can_manage_runtime_settings, get_runtime_settings
from .self_update import get_runtime_update_status, start_runtime_update

router = APIRouter(prefix="/api/runtime-settings", tags=["runtime-settings"], dependencies=[Depends(rate_limit)])
_OPENAI_OAUTH_CALLBACK_START_MODES = {"auto", "server", "local_bridge"}


def _runtime_user(user: dict[str, Any] = Depends(get_current_user)) -> User:
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    return refresh_user(str(user_id))


def _require_settings_admin(user: User) -> None:
    if not can_manage_runtime_settings(user):
        raise HTTPException(status_code=403, detail="You need owner or admin access to manage runtime settings")


@router.get("", response_model=RuntimeSettingsRead)
def read_runtime_settings(user: User = Depends(_runtime_user)) -> RuntimeSettingsRead:
    return get_runtime_settings(user)


@router.post("/connection/openai/api-key", response_model=RuntimeConnectionRead)
def connect_openai_key(payload: OpenAIKeyConnectRequest, user: User = Depends(_runtime_user)) -> RuntimeConnectionRead:
    return connect_openai_api_key(user, payload.api_key)


@router.post("/connection/openai/embedding-api-key", response_model=RuntimeMemoryRead)
def connect_openai_embedding_key(payload: OpenAIKeyConnectRequest, user: User = Depends(_runtime_user)) -> RuntimeMemoryRead:
    return connect_openai_embedding_api_key(user, payload.api_key)


@router.post("/connection/gemini/api-key", response_model=RuntimeMemoryRead)
def connect_gemini_key(payload: OpenAIKeyConnectRequest, user: User = Depends(_runtime_user)) -> RuntimeMemoryRead:
    return connect_gemini_api_key(user, payload.api_key)


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
def exchange_openai_connection(
    payload: OpenAIOAuthExchangeRequest,
    request: Request,
    user: User = Depends(_runtime_user),
) -> RuntimeConnectionRead:
    return exchange_openai_oauth(request, user, payload.callback)


@router.patch("/models", response_model=RuntimeModelsRead)
def save_runtime_models(payload: RuntimeModelsUpdate, user: User = Depends(_runtime_user)) -> RuntimeModelsRead:
    _require_settings_admin(user)
    return update_runtime_models(user, payload)


@router.patch("/memory", response_model=RuntimeMemoryRead)
def save_runtime_memory(payload: RuntimeMemoryUpdate, user: User = Depends(_runtime_user)) -> RuntimeMemoryRead:
    _require_settings_admin(user)
    return update_runtime_memory(user, payload)


@router.post("/memory/check", response_model=RuntimeMemoryCheckRead)
def check_memory(user: User = Depends(_runtime_user)) -> RuntimeMemoryCheckRead:
    _require_settings_admin(user)
    return check_runtime_memory(user)


@router.get("/update", response_model=RuntimeUpdateRead)
def read_runtime_update(user: User = Depends(_runtime_user)) -> RuntimeUpdateRead:
    _require_settings_admin(user)
    return get_runtime_update_status()


@router.post("/update", response_model=RuntimeUpdateRead)
def start_illospace_update(user: User = Depends(_runtime_user)) -> RuntimeUpdateRead:
    _require_settings_admin(user)
    return start_runtime_update(requested_by=str(user.id))
