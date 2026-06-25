from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.async_io import path_stat

MAX_AUDIO_ATTACHMENT_BYTES = 64 * 1024 * 1024


class AudioTranscriptionError(RuntimeError):
    """Raised when an audio attachment cannot be transcribed."""


@dataclass(frozen=True)
class AudioTranscriptionResult:
    transcript: str
    language: str
    provider: str
    model: str
    transport: str
    session_model: str | None = None
    input_format: str | None = None
    input_rate: int | None = None
    bytes_streamed: int = 0

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "transport": self.transport,
            "model": self.model,
            "language": self.language,
            "bytes_streamed": self.bytes_streamed,
            "transcript": self.transcript,
        }
        if self.session_model:
            payload["session_model"] = self.session_model
        if self.input_format:
            payload["input_format"] = self.input_format
        if self.input_rate:
            payload["input_rate"] = self.input_rate
        return payload


async def async_transcribe_audio_path(
    session: AsyncSession,
    path: str | Path,
    *,
    language: str | None = None,
    filename: str | None = None,
    safety_identifier: str | None = None,
) -> AudioTranscriptionResult:
    audio_path = await _validated_audio_path(path, filename=filename)
    config = await _runtime_voice_config(session)
    selected_language = _selected_language(config, language)

    if config.provider == "openai":
        from brain.systems.voice.openai_realtime import async_transcribe_openai_audio_path

        return await async_transcribe_openai_audio_path(
            session,
            audio_path,
            language=selected_language,
            safety_identifier=safety_identifier,
        )
    if config.provider == "local":
        from brain.systems.voice.local_whisper import async_transcribe_local_audio_path

        return await async_transcribe_local_audio_path(
            audio_path,
            language=selected_language,
            model_size=getattr(config, "model_size", "base"),
        )
    if config.provider in {"gemini", "google"}:
        raise AudioTranscriptionError(
            "Gemini Live is selected, but server-side audio attachment transcription is not enabled yet."
        )
    raise AudioTranscriptionError(f"Unsupported voice transcription provider: {config.provider}")


async def _validated_audio_path(path: str | Path, *, filename: str | None) -> Path:
    audio_path = Path(path)
    try:
        stat = await path_stat(audio_path)
    except OSError as exc:
        raise AudioTranscriptionError(f"Audio attachment is not readable: {filename or audio_path.name}") from exc
    if not audio_path.is_file():
        raise AudioTranscriptionError(f"Audio attachment is not a file: {filename or audio_path.name}")
    if stat.st_size > MAX_AUDIO_ATTACHMENT_BYTES:
        raise AudioTranscriptionError(
            f"Audio attachment is too large to transcribe ({stat.st_size} bytes; max {MAX_AUDIO_ATTACHMENT_BYTES})."
        )
    return audio_path


async def _runtime_voice_config(session: AsyncSession):
    from brain.systems.runtime_settings.voice import async_get_runtime_voice_config

    return await async_get_runtime_voice_config(session)


def _selected_language(config: object, explicit_language: str | None) -> str:
    from brain.systems.runtime_settings.voice import _voice_language

    value = explicit_language if explicit_language is not None else getattr(config, "language", "auto")
    return _voice_language(value)
