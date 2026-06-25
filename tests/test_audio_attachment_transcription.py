from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_realtime_transcription_streams_pcm_commit_and_uses_whisper():
    from brain.systems.voice import openai_realtime

    sent: list[dict] = []
    captured: dict[str, object] = {}

    class FakeWebSocket:
        def __init__(self, url: str, headers: dict[str, str]):
            captured["url"] = url
            captured["headers"] = headers
            self.events = [
                {"type": "conversation.item.input_audio_transcription.delta", "delta": "bon"},
                {"type": "conversation.item.input_audio_transcription.delta", "delta": "jour"},
                {"type": "conversation.item.input_audio_transcription.completed", "transcript": "bonjour team"},
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def send(self, payload: str):
            sent.append(json.loads(payload))

        async def recv(self):
            return json.dumps(self.events.pop(0))

    def fake_connect(url: str, headers: dict[str, str]):
        return FakeWebSocket(url, headers)

    async def chunks():
        yield b"\x00\x01" * 50
        yield b"\x00\x01" * 50

    result = await openai_realtime.async_transcribe_pcm_stream_with_openai_realtime(
        "sk-memory-openai",
        chunks(),
        language="fr",
        websocket_connect=fake_connect,
        safety_identifier="illo-user-test",
    )

    assert captured["url"] == "wss://api.openai.com/v1/realtime?model=gpt-realtime-2"
    assert captured["headers"] == {
        "Authorization": "Bearer sk-memory-openai",
        "OpenAI-Safety-Identifier": "illo-user-test",
    }
    session_update = sent[0]
    audio_input = session_update["session"]["audio"]["input"]
    assert session_update["type"] == "session.update"
    assert session_update["session"]["type"] == "transcription"
    assert audio_input["format"] == {"type": "audio/pcm", "rate": 24000}
    assert audio_input["transcription"]["model"] == "gpt-realtime-whisper"
    assert audio_input["transcription"]["language"] == "fr"
    assert "prompt" not in audio_input["transcription"]
    assert audio_input["turn_detection"] is None
    assert sent[-1] == {"type": "input_audio_buffer.commit"}
    assert result.transcript == "bonjour team"
    assert result.bytes_streamed == 200


@pytest.mark.asyncio
async def test_transcribe_audio_path_reuses_memory_key_and_runtime_language(monkeypatch, tmp_path):
    from brain.systems.voice import openai_realtime, transcription

    audio_path = tmp_path / "voice.webm"
    audio_path.write_bytes(b"webm audio")

    transcribe = AsyncMock(
        return_value=transcription.AudioTranscriptionResult(
            transcript="allo team",
            language="fr",
            provider="openai",
            transport="realtime_websocket",
            model="gpt-realtime-whisper",
            bytes_streamed=9,
        )
    )
    monkeypatch.setattr(
        transcription,
        "_runtime_voice_config",
        AsyncMock(return_value=SimpleNamespace(provider="openai", language="fr")),
    )
    monkeypatch.setattr(openai_realtime, "async_transcribe_openai_audio_path", transcribe)

    session = object()
    result = await transcription.async_transcribe_audio_path(session, audio_path)

    assert result.transcript == "allo team"
    transcribe.assert_awaited_once_with(
        session,
        audio_path,
        language="fr",
        safety_identifier=None,
    )


@pytest.mark.asyncio
async def test_transcribe_audio_path_reports_gemini_boundary(monkeypatch, tmp_path):
    from brain.systems.voice import transcription

    audio_path = tmp_path / "voice.webm"
    audio_path.write_bytes(b"webm audio")
    monkeypatch.setattr(
        transcription,
        "_runtime_voice_config",
        AsyncMock(return_value=SimpleNamespace(provider="gemini", language="auto")),
    )

    with pytest.raises(transcription.AudioTranscriptionError) as excinfo:
        await transcription.async_transcribe_audio_path(object(), audio_path)

    assert "Gemini Live is selected" in str(excinfo.value)


@pytest.mark.asyncio
async def test_transcribe_audio_path_routes_local_provider_to_faster_whisper(monkeypatch, tmp_path):
    from brain.systems.voice import local_whisper, transcription

    audio_path = tmp_path / "voice.webm"
    audio_path.write_bytes(b"webm audio")

    local = AsyncMock(
        return_value=transcription.AudioTranscriptionResult(
            transcript="bonjour",
            language="fr",
            provider="local",
            model="faster-whisper-small",
            transport="faster_whisper",
        )
    )
    monkeypatch.setattr(
        transcription,
        "_runtime_voice_config",
        AsyncMock(return_value=SimpleNamespace(provider="local", language="fr", model_size="small")),
    )
    monkeypatch.setattr(local_whisper, "async_transcribe_local_audio_path", local)

    result = await transcription.async_transcribe_audio_path(object(), audio_path)

    assert result.transcript == "bonjour"
    assert result.provider == "local"
    local.assert_awaited_once_with(audio_path, language="fr", model_size="small")


@pytest.mark.asyncio
async def test_local_whisper_joins_segments_and_normalizes_model_size(monkeypatch, tmp_path):
    from brain.systems.voice import local_whisper

    audio_path = tmp_path / "voice.webm"
    audio_path.write_bytes(b"webm audio")

    captured = {}

    class FakeInfo:
        language = "fr"

    class FakeModel:
        def transcribe(self, path, language=None, vad_filter=True):
            captured["path"] = path
            captured["language"] = language
            captured["vad_filter"] = vad_filter
            return iter([SimpleNamespace(text=" bon"), SimpleNamespace(text="jour")]), FakeInfo()

    monkeypatch.setattr(local_whisper, "_load_model", lambda size: FakeModel())

    result = await local_whisper.async_transcribe_local_audio_path(
        audio_path,
        language="fr",
        model_size="not-a-size",
    )

    assert result.transcript == "bonjour"
    assert result.provider == "local"
    assert result.transport == "faster_whisper"
    assert result.language == "fr"
    # Unknown size falls back to the base default.
    assert result.model == "faster-whisper-base"
    assert captured["language"] == "fr"
    assert captured["vad_filter"] is True


@pytest.mark.asyncio
async def test_local_whisper_auto_language_passes_none_to_model(monkeypatch, tmp_path):
    from brain.systems.voice import local_whisper

    audio_path = tmp_path / "voice.webm"
    audio_path.write_bytes(b"webm audio")
    captured = {}

    class FakeModel:
        def transcribe(self, path, language=None, vad_filter=True):
            captured["language"] = language
            return iter([SimpleNamespace(text="hello")]), SimpleNamespace(language="en")

    monkeypatch.setattr(local_whisper, "_load_model", lambda size: FakeModel())

    result = await local_whisper.async_transcribe_local_audio_path(audio_path, language="auto")

    assert captured["language"] is None
    assert result.language == "en"


@pytest.mark.asyncio
async def test_local_whisper_raises_when_no_transcript(monkeypatch, tmp_path):
    from brain.systems.voice import local_whisper

    audio_path = tmp_path / "voice.webm"
    audio_path.write_bytes(b"webm audio")

    class FakeModel:
        def transcribe(self, path, language=None, vad_filter=True):
            return iter([]), SimpleNamespace(language="fr")

    monkeypatch.setattr(local_whisper, "_load_model", lambda size: FakeModel())

    with pytest.raises(local_whisper.AudioTranscriptionError):
        await local_whisper.async_transcribe_local_audio_path(audio_path, language="fr")


@pytest.mark.asyncio
async def test_transcribe_audio_attachment_handler_uses_context_audio(monkeypatch, tmp_path):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import voice as voice_handler
    from brain.systems.voice import transcription

    audio_path = tmp_path / "voice.webm"
    audio_path.write_bytes(b"webm audio")
    captured: dict[str, object] = {}

    class FakeUnitOfWork:
        session = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def fake_transcribe(session, path, **kwargs):
        captured["session"] = session
        captured["path"] = path
        captured["kwargs"] = kwargs
        return transcription.AudioTranscriptionResult(
            transcript="ship the voice tool",
            language="auto",
            provider="openai",
            transport="realtime_websocket",
            model="gpt-realtime-whisper",
            bytes_streamed=512,
        )

    monkeypatch.setattr(voice_handler, "UnitOfWork", FakeUnitOfWork)
    monkeypatch.setattr(voice_handler, "async_transcribe_audio_path", fake_transcribe)

    context = {
        "thread_attachment_context": {
            "items": [
                {
                    "id": "attachment-voice",
                    "kind": "audio",
                    "filename": "voice.webm",
                    "path": str(audio_path),
                    "url": "/static/uploads/voice.webm",
                    "mime": "audio/webm",
                    "size": 10,
                }
            ]
        }
    }
    with bind_agent_context(
        user_id="user-1",
        org_id="org-1",
        execution_metadata=context,
    ):
        payload = await voice_handler._handle_transcribe_audio_attachment(attachment_id="attachment-voice")

    assert payload["transcript"] == "ship the voice tool"
    assert payload["model"] == "gpt-realtime-whisper"
    assert payload["source"]["id"] == "attachment-voice"
    assert captured["path"] == audio_path
    assert captured["kwargs"]["language"] is None
    assert captured["kwargs"]["filename"] == "voice.webm"
    assert str(captured["kwargs"]["safety_identifier"]).startswith("illo-user-")


@pytest.mark.asyncio
async def test_transcribe_audio_attachment_handler_asks_for_id_when_multiple(monkeypatch, tmp_path):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import voice as voice_handler

    first = tmp_path / "first.webm"
    second = tmp_path / "second.webm"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    transcribe = AsyncMock()
    monkeypatch.setattr(voice_handler, "async_transcribe_audio_path", transcribe)

    with bind_agent_context(
        execution_metadata={
            "thread_attachment_context": {
                "items": [
                    {"id": "first", "kind": "audio", "filename": "first.webm", "path": str(first)},
                    {"id": "second", "kind": "audio", "filename": "second.webm", "path": str(second)},
                ]
            }
        }
    ):
        payload = await voice_handler._handle_transcribe_audio_attachment()

    assert payload == {"error": "Multiple audio attachments are available. Provide attachment_id."}
    transcribe.assert_not_called()
