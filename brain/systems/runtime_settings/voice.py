from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.async_io import async_http_post
from brain.platform.db.models.org import User

from .memory import (
    _async_installation_embedding_api_key,
    async_get_embedding_runtime_config,
    async_get_runtime_memory,
)
from .schemas import RuntimeMemoryRead, RuntimeVoiceRead, RuntimeVoiceSessionRead

OPENAI_REALTIME_TRANSCRIPTION_MODEL = "gpt-realtime-whisper"
OPENAI_REALTIME_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
OPENAI_MEMORY_KEY_REQUIRED_DETAIL = "Voice dictation needs an OpenAI API key in AI Runtime memory settings."


async def async_get_runtime_voice(session: AsyncSession, user: User) -> RuntimeVoiceRead:
    memory = await async_get_runtime_memory(session, user)
    return runtime_voice_from_memory(memory)


def runtime_voice_from_memory(memory: RuntimeMemoryRead) -> RuntimeVoiceRead:
    if memory.embedder == "openai" and memory.api_key_statuses.get("openai"):
        return RuntimeVoiceRead(
            provider="openai",
            model=OPENAI_REALTIME_TRANSCRIPTION_MODEL,
            source="memory",
            status="ready",
            detail=None,
        )

    return RuntimeVoiceRead(
        provider="openai",
        model=OPENAI_REALTIME_TRANSCRIPTION_MODEL,
        source="memory",
        status="missing",
        detail=OPENAI_MEMORY_KEY_REQUIRED_DETAIL,
    )


async def async_create_runtime_voice_session(session: AsyncSession, user: User) -> RuntimeVoiceSessionRead:
    runtime = await async_get_embedding_runtime_config(session, include_secret=False)
    provider = str(getattr(runtime, "provider", "") or "").strip().lower()
    api_key = await _async_installation_embedding_api_key(session, "openai")
    if provider != "openai" or not api_key:
        raise HTTPException(
            status_code=409,
            detail=OPENAI_MEMORY_KEY_REQUIRED_DETAIL,
        )

    payload = await _async_create_openai_realtime_client_secret(api_key)
    secret = _client_secret_value(payload)
    if not secret:
        raise HTTPException(status_code=502, detail="OpenAI did not return a realtime voice client secret")

    return RuntimeVoiceSessionRead(
        provider="openai",
        model=OPENAI_REALTIME_TRANSCRIPTION_MODEL,
        client_secret=secret,
        expires_at=_client_secret_expires_at(payload),
    )


async def _async_create_openai_realtime_client_secret(api_key: str) -> dict[str, Any]:
    response = await async_http_post(
        OPENAI_REALTIME_CLIENT_SECRETS_URL,
        timeout=20,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "expires_after": {"anchor": "created_at", "seconds": 600},
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "noise_reduction": {"type": "near_field"},
                        "transcription": {"model": OPENAI_REALTIME_TRANSCRIPTION_MODEL},
                        "turn_detection": None,
                    }
                },
            },
        },
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="OpenAI realtime voice session creation failed")
    data = response.json()
    return data if isinstance(data, dict) else {}


def _client_secret_value(payload: dict[str, Any]) -> str | None:
    raw_value = payload.get("value")
    if isinstance(raw_value, str) and raw_value:
        return raw_value

    raw_secret = payload.get("client_secret")
    if isinstance(raw_secret, dict):
        value = raw_secret.get("value")
        return value if isinstance(value, str) and value else None
    return None


def _client_secret_expires_at(payload: dict[str, Any]) -> int | None:
    raw_value = payload.get("expires_at")
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        pass

    raw_secret = payload.get("client_secret")
    if not isinstance(raw_secret, dict):
        return None
    value = raw_secret.get("expires_at")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
