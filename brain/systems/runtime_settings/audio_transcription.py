from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.async_io import path_stat, run_subprocess

from .memory import _async_installation_embedding_api_key
from .voice import (
    OPENAI_MEMORY_KEY_REQUIRED_DETAIL,
    OPENAI_REALTIME_TRANSCRIPTION_MODEL,
    RuntimeVoiceConfig,
    _openai_transcription_settings,
    _voice_language,
    async_get_runtime_voice_config,
)

OPENAI_REALTIME_WEBSOCKET_URL = "wss://api.openai.com/v1/realtime"
OPENAI_REALTIME_SESSION_MODEL = "gpt-realtime-2"
REALTIME_AUDIO_FORMAT = "audio/pcm"
REALTIME_AUDIO_RATE = 24_000
REALTIME_PCM_CHUNK_BYTES = 64 * 1024
MAX_AUDIO_ATTACHMENT_BYTES = 64 * 1024 * 1024
TRANSCRIBE_AUDIO_TIMEOUT_SECONDS = 120


class AudioTranscriptionError(RuntimeError):
    """Raised when an audio attachment cannot be transcribed."""


@dataclass(frozen=True)
class RealtimeAudioTranscription:
    transcript: str
    language: str
    model: str = OPENAI_REALTIME_TRANSCRIPTION_MODEL
    session_model: str = OPENAI_REALTIME_SESSION_MODEL
    provider: str = "openai"
    transport: str = "realtime_websocket"
    input_format: str = REALTIME_AUDIO_FORMAT
    input_rate: int = REALTIME_AUDIO_RATE
    bytes_streamed: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "transport": self.transport,
            "model": self.model,
            "session_model": self.session_model,
            "language": self.language,
            "input_format": self.input_format,
            "input_rate": self.input_rate,
            "bytes_streamed": self.bytes_streamed,
            "transcript": self.transcript,
        }


def realtime_transcription_session_update(language: str) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": REALTIME_AUDIO_FORMAT, "rate": REALTIME_AUDIO_RATE},
                    "transcription": _openai_transcription_settings(language),
                    "turn_detection": None,
                }
            },
        },
    }


async def async_transcribe_audio_path(
    session: AsyncSession,
    path: str | Path,
    *,
    language: str | None = None,
    filename: str | None = None,
    safety_identifier: str | None = None,
) -> RealtimeAudioTranscription:
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

    config = await async_get_runtime_voice_config(session)
    if config.provider != "openai":
        raise AudioTranscriptionError("Audio transcription currently needs OpenAI Realtime voice settings.")
    api_key = await _async_installation_embedding_api_key(session, "openai")
    if not api_key:
        raise AudioTranscriptionError(OPENAI_MEMORY_KEY_REQUIRED_DETAIL)

    selected_language = _selected_language(config, language)
    pcm = await async_audio_file_to_pcm(audio_path)
    return await async_transcribe_pcm_with_openai_realtime(
        api_key,
        pcm,
        language=selected_language,
        safety_identifier=safety_identifier,
    )


def _selected_language(config: RuntimeVoiceConfig, explicit_language: str | None) -> str:
    if explicit_language is not None:
        return _voice_language(explicit_language)
    return _voice_language(config.language)


async def async_audio_file_to_pcm(path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioTranscriptionError("Audio transcription requires ffmpeg in the API runtime image.")
    try:
        completed = await run_subprocess(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                path,
                "-ac",
                "1",
                "-ar",
                str(REALTIME_AUDIO_RATE),
                "-f",
                "s16le",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TRANSCRIBE_AUDIO_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioTranscriptionError("Audio transcription timed out while preparing audio.") from exc
    except OSError as exc:
        raise AudioTranscriptionError("Audio transcription could not prepare the audio file.") from exc

    if completed.returncode != 0:
        stderr = _clean_subprocess_output(completed.stderr)
        detail = f": {stderr}" if stderr else ""
        raise AudioTranscriptionError(f"Audio file could not be converted for realtime transcription{detail}")
    output = completed.stdout if isinstance(completed.stdout, bytes) else str(completed.stdout or "").encode("utf-8")
    if not output:
        raise AudioTranscriptionError("Audio file did not contain transcribable audio.")
    return output


async def async_transcribe_pcm_with_openai_realtime(
    api_key: str,
    pcm_audio: bytes,
    *,
    language: str = "auto",
    websocket_connect: Callable[[str, dict[str, str]], Any] | None = None,
    safety_identifier: str | None = None,
    timeout: float = TRANSCRIBE_AUDIO_TIMEOUT_SECONDS,
) -> RealtimeAudioTranscription:
    if not pcm_audio:
        raise AudioTranscriptionError("Audio file did not contain transcribable audio.")

    url = f"{OPENAI_REALTIME_WEBSOCKET_URL}?{urlencode({'model': OPENAI_REALTIME_SESSION_MODEL})}"
    headers = {"Authorization": f"Bearer {api_key}"}
    if safety_identifier:
        headers["OpenAI-Safety-Identifier"] = safety_identifier

    connect = websocket_connect or _connect_openai_realtime_websocket
    completion: str | None = None
    deltas: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout

    async with connect(url, headers) as websocket:
        await websocket.send(json.dumps(realtime_transcription_session_update(language)))
        for offset in range(0, len(pcm_audio), REALTIME_PCM_CHUNK_BYTES):
            chunk = pcm_audio[offset:offset + REALTIME_PCM_CHUNK_BYTES]
            await websocket.send(
                json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                })
            )
        await websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))

        while completion is None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AudioTranscriptionError("OpenAI Realtime transcription timed out.")
            raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            event = _json_event(raw)
            event_type = str(event.get("type") or "")
            if event_type == "error":
                raise AudioTranscriptionError(_openai_error_message(event))
            if event_type == "conversation.item.input_audio_transcription.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    deltas.append(delta)
            elif event_type == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript")
                completion = transcript if isinstance(transcript, str) else "".join(deltas)

    return RealtimeAudioTranscription(
        transcript=(completion or "").strip(),
        language=_voice_language(language),
        bytes_streamed=len(pcm_audio),
    )


def _connect_openai_realtime_websocket(url: str, headers: dict[str, str]) -> Any:
    import websockets

    try:
        return websockets.connect(url, additional_headers=headers, max_size=16 * 1024 * 1024)
    except TypeError:
        return websockets.connect(url, extra_headers=headers, max_size=16 * 1024 * 1024)


def _json_event(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        event = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise AudioTranscriptionError("OpenAI Realtime returned an invalid event.") from exc
    return event if isinstance(event, dict) else {}


def _openai_error_message(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or "OpenAI Realtime transcription failed."
        return str(message)
    return "OpenAI Realtime transcription failed."


def _clean_subprocess_output(output: object) -> str:
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace").strip()[:500]
    return str(output or "").strip()[:500]
