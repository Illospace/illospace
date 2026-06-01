from __future__ import annotations

import asyncio
import base64
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems.voice.transcription import AudioTranscriptionError, AudioTranscriptionResult

OPENAI_REALTIME_TRANSCRIPTION_MODEL = "gpt-realtime-whisper"
OPENAI_REALTIME_WEBSOCKET_URL = "wss://api.openai.com/v1/realtime"
OPENAI_REALTIME_SESSION_MODEL = "gpt-realtime-2"
REALTIME_AUDIO_FORMAT = "audio/pcm"
REALTIME_AUDIO_RATE = 24_000
REALTIME_PCM_CHUNK_BYTES = 64 * 1024
TRANSCRIBE_AUDIO_TIMEOUT_SECONDS = 120


async def async_transcribe_openai_audio_path(
    session: AsyncSession,
    path: Path,
    *,
    language: str = "auto",
    safety_identifier: str | None = None,
    websocket_connect: Callable[[str, dict[str, str]], Any] | None = None,
) -> AudioTranscriptionResult:
    api_key = await _openai_memory_api_key(session)
    return await async_transcribe_pcm_stream_with_openai_realtime(
        api_key,
        _pcm_chunks_from_audio_file(path),
        language=language,
        websocket_connect=websocket_connect,
        safety_identifier=safety_identifier,
    )


async def async_transcribe_pcm_stream_with_openai_realtime(
    api_key: str,
    pcm_chunks: AsyncIterator[bytes],
    *,
    language: str = "auto",
    websocket_connect: Callable[[str, dict[str, str]], Any] | None = None,
    safety_identifier: str | None = None,
    timeout: float = TRANSCRIBE_AUDIO_TIMEOUT_SECONDS,
) -> AudioTranscriptionResult:
    url = f"{OPENAI_REALTIME_WEBSOCKET_URL}?{urlencode({'model': OPENAI_REALTIME_SESSION_MODEL})}"
    headers = {"Authorization": f"Bearer {api_key}"}
    if safety_identifier:
        headers["OpenAI-Safety-Identifier"] = safety_identifier

    connect = websocket_connect or _connect_openai_realtime_websocket
    completion: str | None = None
    deltas: list[str] = []
    bytes_streamed = 0
    deadline = asyncio.get_running_loop().time() + timeout

    async with connect(url, headers) as websocket:
        await websocket.send(json.dumps(realtime_transcription_session_update(language)))
        async for chunk in pcm_chunks:
            if not chunk:
                continue
            bytes_streamed += len(chunk)
            await websocket.send(
                json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                })
            )
        if bytes_streamed <= 0:
            raise AudioTranscriptionError("Audio file did not contain transcribable audio.")

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

    return AudioTranscriptionResult(
        transcript=(completion or "").strip(),
        provider="openai",
        transport="realtime_websocket",
        model=OPENAI_REALTIME_TRANSCRIPTION_MODEL,
        session_model=OPENAI_REALTIME_SESSION_MODEL,
        language=_voice_language(language),
        input_format=REALTIME_AUDIO_FORMAT,
        input_rate=REALTIME_AUDIO_RATE,
        bytes_streamed=bytes_streamed,
    )


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


async def _pcm_chunks_from_audio_file(path: Path) -> AsyncIterator[bytes]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioTranscriptionError("Audio transcription requires ffmpeg in the API runtime image.")

    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-ac",
        "1",
        "-ar",
        str(REALTIME_AUDIO_RATE),
        "-f",
        "s16le",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    stderr_task = asyncio.create_task(process.stderr.read() if process.stderr else _empty_bytes())
    try:
        while True:
            chunk = await asyncio.wait_for(
                process.stdout.read(REALTIME_PCM_CHUNK_BYTES),
                timeout=TRANSCRIBE_AUDIO_TIMEOUT_SECONDS,
            )
            if not chunk:
                break
            yield chunk
        return_code = await asyncio.wait_for(process.wait(), timeout=TRANSCRIBE_AUDIO_TIMEOUT_SECONDS)
        stderr = await stderr_task
        if return_code != 0:
            detail = _clean_subprocess_output(stderr)
            suffix = f": {detail}" if detail else ""
            raise AudioTranscriptionError(f"Audio file could not be converted for realtime transcription{suffix}")
    except asyncio.TimeoutError as exc:
        raise AudioTranscriptionError("Audio transcription timed out while preparing audio.") from exc
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if not stderr_task.done():
            stderr_task.cancel()


async def _empty_bytes() -> bytes:
    return b""


async def _openai_memory_api_key(session: AsyncSession) -> str:
    from brain.systems.runtime_settings.memory import _async_installation_embedding_api_key
    from brain.systems.runtime_settings.voice import OPENAI_MEMORY_KEY_REQUIRED_DETAIL

    api_key = await _async_installation_embedding_api_key(session, "openai")
    if not api_key:
        raise AudioTranscriptionError(OPENAI_MEMORY_KEY_REQUIRED_DETAIL)
    return api_key


def _openai_transcription_settings(language: str) -> dict[str, str]:
    settings = {
        "model": OPENAI_REALTIME_TRANSCRIPTION_MODEL,
    }
    if _voice_language(language) in {"en", "fr"}:
        settings["language"] = _voice_language(language)
    return settings


def _voice_language(value: object) -> str:
    language = str(value or "auto").strip().lower()
    return language if language in {"auto", "en", "fr"} else "auto"


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
