from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.async_io import async_http_post
from brain.platform.db.models.org import User

from .memory import (
    _async_read_runtime_config_value,
    _async_installation_embedding_api_key,
    _async_write_runtime_config_value,
    async_get_runtime_memory,
)
from .schemas import (
    RuntimeMemoryRead,
    RuntimeOption,
    RuntimeVoiceRead,
    RuntimeVoiceSessionRead,
    RuntimeVoiceTranscriptRead,
    RuntimeVoiceUpdate,
)

logger = logging.getLogger(__name__)

OPENAI_REALTIME_TRANSCRIPTION_MODEL = "gpt-realtime-whisper"
OPENAI_REALTIME_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
OPENAI_MEMORY_KEY_REQUIRED_DETAIL = "Voice dictation needs an OpenAI API key in AI Runtime memory settings."
GEMINI_LIVE_MODEL = "gemini-live"
GEMINI_VOICE_UNAVAILABLE_DETAIL = "Gemini Live voice is not enabled yet."
RUNTIME_VOICE_SETTINGS_KEY = "runtime_voice"
LOCAL_WHISPER_MODEL_LABEL = "faster-whisper"
LOCAL_WHISPER_INSTALL_DETAIL = (
    "Local voice dictation needs faster-whisper. It ships with Illospace; "
    "rebuild or update the API image (or run `pip install faster-whisper`) if this persists."
)
VOICE_PROVIDER_OPTIONS = [
    RuntimeOption(
        key="openai",
        label="OpenAI Realtime",
        description="Uses the OpenAI API key saved for AI Runtime memory.",
    ),
    RuntimeOption(
        key="local",
        label="Local (faster-whisper)",
        description="On-device CPU transcription. No API key. Push-to-talk.",
    ),
    RuntimeOption(
        key="gemini",
        label="Gemini Live",
        description="Not enabled yet for browser dictation.",
        disabled=True,
    ),
]
VOICE_LANGUAGE_OPTIONS = [
    RuntimeOption(
        key="auto",
        label="Auto (English / French)",
        description="Detect English or French and preserve the spoken language.",
    ),
    RuntimeOption(key="en", label="English", description="Prefer English transcription."),
    RuntimeOption(key="fr", label="French", description="Prefer French transcription."),
]
VOICE_MODEL_SIZE_OPTIONS = [
    RuntimeOption(
        key="tiny",
        label="Tiny",
        description="Fastest, lowest accuracy. For weak machines or quick tests.",
    ),
    RuntimeOption(
        key="base",
        label="Base",
        description="Balanced default. Solid English and French on CPU.",
    ),
    RuntimeOption(
        key="small",
        label="Small",
        description="Best accuracy, notably better French. Heavier and slower.",
    ),
]


@dataclass(frozen=True)
class RuntimeVoiceConfig:
    provider: str = "openai"
    language: str = "auto"
    model_size: str = "base"

    def stored_settings(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "language": self.language,
            "model_size": self.model_size,
        }


async def async_get_runtime_voice(session: AsyncSession, user: User) -> RuntimeVoiceRead:
    memory = await async_get_runtime_memory(session, user)
    config = await async_get_runtime_voice_config(session)
    return runtime_voice_from_memory(memory, config)


async def async_get_runtime_voice_config(session: AsyncSession) -> RuntimeVoiceConfig:
    raw = await _async_read_runtime_config_value(session, RUNTIME_VOICE_SETTINGS_KEY)
    if not raw:
        return RuntimeVoiceConfig()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid runtime voice settings JSON")
        return RuntimeVoiceConfig()
    if not isinstance(data, dict):
        return RuntimeVoiceConfig()
    return RuntimeVoiceConfig(
        provider=_voice_provider(data.get("provider")),
        language=_voice_language(data.get("language")),
        model_size=_voice_model_size(data.get("model_size")),
    )


async def async_update_runtime_voice(
    session: AsyncSession,
    user: User,
    update: RuntimeVoiceUpdate,
) -> RuntimeVoiceRead:
    config = RuntimeVoiceConfig(
        provider=_voice_provider(update.provider),
        language=_voice_language(update.language),
        model_size=_voice_model_size(update.model_size),
    )
    await _async_write_runtime_config_value(
        session,
        RUNTIME_VOICE_SETTINGS_KEY,
        json.dumps(config.stored_settings(), sort_keys=True),
    )
    memory = await async_get_runtime_memory(session, user)
    return runtime_voice_from_memory(memory, config)


def runtime_voice_from_memory(
    memory: RuntimeMemoryRead,
    config: RuntimeVoiceConfig | None = None,
) -> RuntimeVoiceRead:
    config = config or RuntimeVoiceConfig()
    size = _voice_model_size(config.model_size)

    if config.provider == "local":
        from brain.systems.voice.local_whisper import local_whisper_available

        available = local_whisper_available()
        return _voice_read(
            provider="local",
            model=f"{LOCAL_WHISPER_MODEL_LABEL}-{size}",
            config=config,
            size=size,
            status="ready" if available else "missing",
            detail=None if available else LOCAL_WHISPER_INSTALL_DETAIL,
        )

    has_openai_key = bool(memory.api_key_statuses.get("openai"))
    if config.provider == "openai" and has_openai_key:
        return _voice_read(
            provider="openai",
            model=OPENAI_REALTIME_TRANSCRIPTION_MODEL,
            config=config,
            size=size,
            status="ready",
            detail=None,
        )

    if config.provider == "gemini":
        return _voice_read(
            provider="gemini",
            model=GEMINI_LIVE_MODEL,
            config=config,
            size=size,
            status="error",
            detail=GEMINI_VOICE_UNAVAILABLE_DETAIL,
        )

    return _voice_read(
        provider="openai",
        model=OPENAI_REALTIME_TRANSCRIPTION_MODEL,
        config=config,
        size=size,
        status="missing",
        detail=OPENAI_MEMORY_KEY_REQUIRED_DETAIL,
    )


def _voice_read(
    *,
    provider: str,
    model: str,
    config: RuntimeVoiceConfig,
    size: str,
    status: str,
    detail: str | None,
) -> RuntimeVoiceRead:
    return RuntimeVoiceRead(
        provider=provider,
        model=model,
        source="memory",
        language=_voice_language(config.language),
        model_size=size,
        status=status,
        detail=detail,
        provider_options=VOICE_PROVIDER_OPTIONS,
        language_options=VOICE_LANGUAGE_OPTIONS,
        model_size_options=VOICE_MODEL_SIZE_OPTIONS,
    )


async def async_create_runtime_voice_session(session: AsyncSession, user: User) -> RuntimeVoiceSessionRead:
    config = await async_get_runtime_voice_config(session)
    if config.provider == "local":
        raise HTTPException(
            status_code=409,
            detail="Local voice dictation uses push-to-talk transcription, not a realtime session.",
        )
    if config.provider != "openai":
        raise HTTPException(
            status_code=409,
            detail=GEMINI_VOICE_UNAVAILABLE_DETAIL,
        )
    api_key = await _async_installation_embedding_api_key(session, "openai")
    if not api_key:
        raise HTTPException(
            status_code=409,
            detail=OPENAI_MEMORY_KEY_REQUIRED_DETAIL,
        )

    payload = await _async_create_openai_realtime_client_secret(api_key, language=config.language)
    secret = _client_secret_value(payload)
    if not secret:
        raise HTTPException(status_code=502, detail="OpenAI did not return a realtime voice client secret")

    return RuntimeVoiceSessionRead(
        provider="openai",
        model=OPENAI_REALTIME_TRANSCRIPTION_MODEL,
        language=_voice_language(config.language),
        client_secret=secret,
        expires_at=_client_secret_expires_at(payload),
    )


async def async_transcribe_runtime_voice_clip(
    session: AsyncSession,
    user: User,
    *,
    upload: UploadFile,
) -> RuntimeVoiceTranscriptRead:
    """Transcribe a recorded push-to-talk audio clip using the configured provider.

    Works for any server-side provider (OpenAI realtime or local faster-whisper);
    the active provider is resolved from runtime voice settings inside
    ``async_transcribe_audio_path``.
    """

    from brain.systems.voice.transcription import (
        AudioTranscriptionError,
        async_transcribe_audio_path,
    )

    filename = upload.filename or "voice-clip.webm"
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=422, detail="No audio was uploaded for transcription.")

    suffix = Path(filename).suffix or ".webm"
    tmp_path = await asyncio.to_thread(_write_temp_audio, data, suffix)
    try:
        result = await async_transcribe_audio_path(
            session,
            tmp_path,
            filename=filename,
            safety_identifier=_user_safety_identifier(user),
        )
    except AudioTranscriptionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await asyncio.to_thread(_remove_file, tmp_path)

    return RuntimeVoiceTranscriptRead(
        transcript=result.transcript,
        provider=result.provider,
        model=result.model,
        language=result.language,
        transport=result.transport,
        bytes_streamed=result.bytes_streamed,
    )


def _write_temp_audio(data: bytes, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=suffix, prefix="illo-voice-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except Exception:
        os.unlink(name)
        raise
    return Path(name)


def _remove_file(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _user_safety_identifier(user: User) -> str | None:
    user_id = getattr(user, "id", None)
    if not user_id:
        return None
    org_id = getattr(user, "org_id", None)
    digest = hashlib.sha256(f"{org_id or ''}:{user_id}".encode("utf-8", errors="ignore")).hexdigest()[:32]
    return f"illo-user-{digest}"


async def _async_create_openai_realtime_client_secret(api_key: str, *, language: str = "auto") -> dict[str, Any]:
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
                        "transcription": _openai_transcription_settings(language),
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


def _openai_transcription_settings(language: str) -> dict[str, str]:
    normalized = _voice_language(language)
    settings = {
        "model": OPENAI_REALTIME_TRANSCRIPTION_MODEL,
    }
    if normalized in {"en", "fr"}:
        settings["language"] = normalized
    return settings


def _voice_provider(value: object) -> str:
    provider = str(value or "openai").strip().lower()
    if provider in {"gemini", "google"}:
        return "gemini"
    if provider == "local":
        return "local"
    return "openai"


def _voice_language(value: object) -> str:
    language = str(value or "auto").strip().lower()
    return language if language in {"auto", "en", "fr"} else "auto"


def _voice_model_size(value: object) -> str:
    size = str(value or "base").strip().lower()
    return size if size in {"tiny", "base", "small"} else "base"


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
