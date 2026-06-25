from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _memory_without_openai_key():
    from brain.systems.runtime_settings.schemas import RuntimeMemoryRead

    return RuntimeMemoryRead(
        embedder="openai",
        embedding_status="ready",
        indexed_vectors=0,
        api_key_statuses={"openai": False},
        reranker="weighted",
        embedder_options=[],
        embedding_model_options=[],
        reranker_options=[],
    )


@pytest.mark.asyncio
async def test_runtime_voice_defaults_to_ready_openai_realtime_from_memory_key(monkeypatch):
    from brain.systems.runtime_settings import voice as voice_settings
    from brain.systems.runtime_settings.schemas import RuntimeMemoryRead

    session = MagicMock()
    user = SimpleNamespace(id="user-1", org_id="org-1")

    monkeypatch.setattr(
        voice_settings,
        "async_get_runtime_memory",
        AsyncMock(
            return_value=RuntimeMemoryRead(
                embedder="openai",
                embedding_status="ready",
                indexed_vectors=0,
                api_key_statuses={"openai": True},
                reranker="weighted",
                embedder_options=[],
                embedding_model_options=[],
                reranker_options=[],
            )
        ),
    )
    monkeypatch.setattr(voice_settings, "_async_read_runtime_config_value", AsyncMock(return_value=None))

    voice = await voice_settings.async_get_runtime_voice(session, user)

    assert voice.provider == "openai"
    assert voice.model == "gpt-realtime-whisper"
    assert voice.source == "memory"
    assert voice.language == "auto"
    assert voice.status == "ready"
    assert voice.detail is None
    assert [option.key for option in voice.language_options] == ["auto", "en", "fr"]


@pytest.mark.asyncio
async def test_runtime_settings_payload_includes_voice_status(monkeypatch):
    from brain.systems.runtime_settings import service as runtime_service
    from brain.systems.runtime_settings.schemas import RuntimeMemoryRead

    session = MagicMock()
    user = SimpleNamespace(id="user-1", org_id="org-1", role="member")
    memory = RuntimeMemoryRead(
        embedder="openai",
        embedding_status="ready",
        indexed_vectors=0,
        api_key_statuses={"openai": True},
        reranker="weighted",
        embedder_options=[],
        embedding_model_options=[],
        reranker_options=[],
    )

    monkeypatch.setattr(
        runtime_service,
        "async_get_openai_connection",
        AsyncMock(return_value={"status": "connected", "setup_required": False}),
    )
    monkeypatch.setattr(
        runtime_service,
        "async_get_runtime_models",
        AsyncMock(return_value={"default": "gpt-5.5", "options": []}),
    )
    monkeypatch.setattr(
        runtime_service,
        "async_get_runtime_memory",
        AsyncMock(return_value=memory),
    )
    monkeypatch.setattr(runtime_service, "async_get_runtime_voice_config", AsyncMock(return_value=None))

    settings = await runtime_service.async_get_runtime_settings(session, user)

    assert settings.voice.model == "gpt-realtime-whisper"
    assert settings.voice.status == "ready"
    assert settings.voice.language == "auto"


@pytest.mark.asyncio
async def test_runtime_voice_is_missing_without_openai_memory_key(monkeypatch):
    from brain.systems.runtime_settings import voice as voice_settings
    from brain.systems.runtime_settings.schemas import RuntimeMemoryRead

    memory = RuntimeMemoryRead(
        embedder="gemini",
        embedding_status="ready",
        indexed_vectors=0,
        api_key_statuses={"openai": False, "gemini": True},
        reranker="weighted",
        embedder_options=[],
        embedding_model_options=[],
        reranker_options=[],
    )

    voice = voice_settings.runtime_voice_from_memory(memory)

    assert voice.provider == "openai"
    assert voice.model == "gpt-realtime-whisper"
    assert voice.source == "memory"
    assert voice.language == "auto"
    assert voice.status == "missing"
    assert "OpenAI API key" in (voice.detail or "")


@pytest.mark.asyncio
async def test_runtime_voice_reports_gemini_as_not_enabled(monkeypatch):
    from brain.systems.runtime_settings import voice as voice_settings
    from brain.systems.runtime_settings.schemas import RuntimeMemoryRead

    memory = RuntimeMemoryRead(
        embedder="gemini",
        embedding_status="ready",
        indexed_vectors=0,
        api_key_statuses={"openai": True, "gemini": True},
        reranker="weighted",
        embedder_options=[],
        embedding_model_options=[],
        reranker_options=[],
    )

    voice = voice_settings.runtime_voice_from_memory(
        memory,
        voice_settings.RuntimeVoiceConfig(provider="gemini", language="auto"),
    )

    assert voice.provider == "gemini"
    assert voice.model == "gemini-live"
    assert voice.status == "error"
    assert "not enabled yet" in (voice.detail or "")


@pytest.mark.asyncio
async def test_runtime_voice_session_does_not_use_openai_key_when_gemini_selected(monkeypatch):
    from fastapi import HTTPException

    from brain.systems.runtime_settings import voice as voice_settings

    key_lookup = AsyncMock(return_value="sk-memory-openai")
    monkeypatch.setattr(
        voice_settings,
        "async_get_runtime_voice_config",
        AsyncMock(return_value=voice_settings.RuntimeVoiceConfig(provider="gemini", language="auto")),
    )
    monkeypatch.setattr(voice_settings, "_async_installation_embedding_api_key", key_lookup)

    with pytest.raises(HTTPException) as excinfo:
        await voice_settings.async_create_runtime_voice_session(MagicMock(), SimpleNamespace(id="user-1"))

    assert excinfo.value.status_code == 409
    assert "Gemini Live voice is not enabled yet" in str(excinfo.value.detail)
    key_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_voice_session_uses_openai_memory_key_without_returning_it(monkeypatch):
    from brain.systems.runtime_settings import voice as voice_settings

    session = MagicMock()
    user = SimpleNamespace(id="user-1", org_id="org-1", role="member")
    create_secret = AsyncMock(
        return_value={
            "client_secret": {"value": "ek_test_voice", "expires_at": 1_800_000_000},
            "session": {"id": "sess_voice"},
        }
    )

    monkeypatch.setattr(voice_settings, "async_get_runtime_voice_config", AsyncMock(return_value=voice_settings.RuntimeVoiceConfig(language="fr")))
    monkeypatch.setattr(
        voice_settings,
        "_async_installation_embedding_api_key",
        AsyncMock(return_value="sk-memory-openai"),
    )
    monkeypatch.setattr(voice_settings, "_async_create_openai_realtime_client_secret", create_secret)

    result = await voice_settings.async_create_runtime_voice_session(session, user)

    assert result.provider == "openai"
    assert result.model == "gpt-realtime-whisper"
    assert result.language == "fr"
    assert result.client_secret == "ek_test_voice"
    assert result.expires_at == 1_800_000_000
    assert "sk-memory-openai" not in result.model
    create_secret.assert_awaited_once()
    assert create_secret.await_args.args[0] == "sk-memory-openai"
    assert create_secret.await_args.kwargs["language"] == "fr"


@pytest.mark.asyncio
async def test_openai_realtime_secret_request_uses_manual_commit_for_realtime_whisper(monkeypatch):
    from brain.systems.runtime_settings import voice as voice_settings

    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"value": "ek_test_voice", "expires_at": 1_800_000_000}

    async def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return Response()

    monkeypatch.setattr(voice_settings, "async_http_post", fake_post)

    result = await voice_settings._async_create_openai_realtime_client_secret("sk-memory-openai")

    request_json = captured["kwargs"]["json"]
    audio_input = request_json["session"]["audio"]["input"]
    assert result["value"] == "ek_test_voice"
    assert captured["url"] == "https://api.openai.com/v1/realtime/client_secrets"
    assert request_json["session"]["type"] == "transcription"
    assert audio_input["transcription"]["model"] == "gpt-realtime-whisper"
    assert "language" not in audio_input["transcription"]
    assert "prompt" not in audio_input["transcription"]
    assert audio_input["turn_detection"] is None


@pytest.mark.asyncio
async def test_openai_realtime_secret_request_can_pin_french_language(monkeypatch):
    from brain.systems.runtime_settings import voice as voice_settings

    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"value": "ek_test_voice", "expires_at": 1_800_000_000}

    async def fake_post(url, **kwargs):
        captured["kwargs"] = kwargs
        return Response()

    monkeypatch.setattr(voice_settings, "async_http_post", fake_post)

    await voice_settings._async_create_openai_realtime_client_secret("sk-memory-openai", language="fr")

    transcription = captured["kwargs"]["json"]["session"]["audio"]["input"]["transcription"]
    assert transcription["model"] == "gpt-realtime-whisper"
    assert transcription["language"] == "fr"
    assert "prompt" not in transcription


@pytest.mark.asyncio
async def test_runtime_voice_update_persists_language(monkeypatch):
    from brain.systems.runtime_settings import voice as voice_settings
    from brain.systems.runtime_settings.schemas import RuntimeMemoryRead, RuntimeVoiceUpdate

    session = MagicMock()
    user = SimpleNamespace(id="user-1", org_id="org-1")
    written = {}

    async def fake_write(_session, key, value):
        written["key"] = key
        written["value"] = value

    monkeypatch.setattr(voice_settings, "_async_write_runtime_config_value", fake_write)
    monkeypatch.setattr(
        voice_settings,
        "async_get_runtime_memory",
        AsyncMock(
            return_value=RuntimeMemoryRead(
                embedder="gemini",
                embedding_status="ready",
                indexed_vectors=0,
                api_key_statuses={"openai": True, "gemini": True},
                reranker="weighted",
                embedder_options=[],
                embedding_model_options=[],
                reranker_options=[],
            )
        ),
    )

    result = await voice_settings.async_update_runtime_voice(
        session,
        user,
        RuntimeVoiceUpdate(provider="openai", language="fr"),
    )

    assert written["key"] == "runtime_voice"
    assert '"language": "fr"' in written["value"]
    assert result.language == "fr"
    assert result.status == "ready"


def test_runtime_voice_secret_helpers_accept_current_openai_response_shape():
    from brain.systems.runtime_settings import voice as voice_settings

    payload = {"value": "ek_test_voice", "expires_at": 1_800_000_000}

    assert voice_settings._client_secret_value(payload) == "ek_test_voice"
    assert voice_settings._client_secret_expires_at(payload) == 1_800_000_000


@pytest.mark.asyncio
async def test_runtime_voice_session_endpoint_is_available_to_workspace_members(monkeypatch):
    from brain.systems.runtime_settings import router as runtime_router

    session = MagicMock()
    user = SimpleNamespace(id="user-1", org_id="org-1", role="member")
    result = SimpleNamespace(
        provider="openai",
        model="gpt-realtime-whisper",
        client_secret="ek_test_voice",
        expires_at=1_800_000_000,
    )
    create_session = AsyncMock(return_value=result)

    monkeypatch.setattr(runtime_router, "async_create_runtime_voice_session", create_session)

    response = await runtime_router.create_voice_session(user=user, db=session)

    assert response is result
    create_session.assert_awaited_once_with(session, user)


@pytest.mark.asyncio
async def test_runtime_voice_local_provider_is_ready_when_faster_whisper_available(monkeypatch):
    from brain.systems.runtime_settings import voice as voice_settings
    from brain.systems.voice import local_whisper

    monkeypatch.setattr(local_whisper, "local_whisper_available", lambda: True)

    voice = voice_settings.runtime_voice_from_memory(
        _memory_without_openai_key(),
        voice_settings.RuntimeVoiceConfig(provider="local", language="fr", model_size="small"),
    )

    assert voice.provider == "local"
    assert voice.model == "faster-whisper-small"
    assert voice.model_size == "small"
    assert voice.language == "fr"
    assert voice.status == "ready"
    assert voice.detail is None
    assert [option.key for option in voice.provider_options] == ["openai", "local", "gemini"]
    assert [option.key for option in voice.model_size_options] == ["tiny", "base", "small"]


@pytest.mark.asyncio
async def test_runtime_voice_local_provider_is_missing_without_faster_whisper(monkeypatch):
    from brain.systems.runtime_settings import voice as voice_settings
    from brain.systems.voice import local_whisper

    monkeypatch.setattr(local_whisper, "local_whisper_available", lambda: False)

    voice = voice_settings.runtime_voice_from_memory(
        _memory_without_openai_key(),
        voice_settings.RuntimeVoiceConfig(provider="local"),
    )

    assert voice.provider == "local"
    assert voice.model == "faster-whisper-base"
    assert voice.model_size == "base"
    assert voice.status == "missing"
    assert "faster-whisper" in (voice.detail or "")


@pytest.mark.asyncio
async def test_runtime_voice_update_persists_provider_and_model_size(monkeypatch):
    from brain.systems.runtime_settings import voice as voice_settings
    from brain.systems.runtime_settings.schemas import RuntimeVoiceUpdate
    from brain.systems.voice import local_whisper

    monkeypatch.setattr(local_whisper, "local_whisper_available", lambda: True)
    written = {}

    async def fake_write(_session, key, value):
        written["key"] = key
        written["value"] = value

    monkeypatch.setattr(voice_settings, "_async_write_runtime_config_value", fake_write)
    monkeypatch.setattr(
        voice_settings,
        "async_get_runtime_memory",
        AsyncMock(return_value=_memory_without_openai_key()),
    )

    result = await voice_settings.async_update_runtime_voice(
        MagicMock(),
        SimpleNamespace(id="user-1", org_id="org-1"),
        RuntimeVoiceUpdate(provider="local", language="fr", model_size="small"),
    )

    assert written["key"] == "runtime_voice"
    assert '"provider": "local"' in written["value"]
    assert '"model_size": "small"' in written["value"]
    assert result.provider == "local"
    assert result.model_size == "small"
    assert result.status == "ready"


@pytest.mark.asyncio
async def test_runtime_voice_session_rejects_local_provider_without_openai_lookup(monkeypatch):
    from fastapi import HTTPException

    from brain.systems.runtime_settings import voice as voice_settings

    key_lookup = AsyncMock(return_value="sk-memory-openai")
    monkeypatch.setattr(
        voice_settings,
        "async_get_runtime_voice_config",
        AsyncMock(return_value=voice_settings.RuntimeVoiceConfig(provider="local")),
    )
    monkeypatch.setattr(voice_settings, "_async_installation_embedding_api_key", key_lookup)

    with pytest.raises(HTTPException) as excinfo:
        await voice_settings.async_create_runtime_voice_session(MagicMock(), SimpleNamespace(id="user-1"))

    assert excinfo.value.status_code == 409
    assert "push-to-talk" in str(excinfo.value.detail)
    key_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_transcribe_runtime_voice_clip_routes_through_configured_provider(monkeypatch):
    from brain.systems.runtime_settings import voice as voice_settings
    from brain.systems.voice import transcription as transcription_mod

    captured = {}

    async def fake_transcribe(session, path, **kwargs):
        captured["path"] = path
        captured["kwargs"] = kwargs
        captured["existed_during_call"] = Path(path).is_file()
        return transcription_mod.AudioTranscriptionResult(
            transcript="bonjour le monde",
            language="fr",
            provider="local",
            model="faster-whisper-base",
            transport="faster_whisper",
            bytes_streamed=11,
        )

    monkeypatch.setattr(transcription_mod, "async_transcribe_audio_path", fake_transcribe)

    class FakeUpload:
        filename = "clip.webm"

        async def read(self):
            return b"webm-audio-bytes"

    result = await voice_settings.async_transcribe_runtime_voice_clip(
        MagicMock(),
        SimpleNamespace(id="user-1", org_id="org-1"),
        upload=FakeUpload(),
    )

    assert result.transcript == "bonjour le monde"
    assert result.provider == "local"
    assert result.model == "faster-whisper-base"
    assert result.language == "fr"
    assert captured["kwargs"]["filename"] == "clip.webm"
    assert str(captured["kwargs"]["safety_identifier"]).startswith("illo-user-")
    assert captured["existed_during_call"] is True
    # Temp file is cleaned up after transcription returns.
    assert not Path(captured["path"]).exists()


@pytest.mark.asyncio
async def test_transcribe_runtime_voice_clip_rejects_empty_audio():
    from fastapi import HTTPException

    from brain.systems.runtime_settings import voice as voice_settings

    class EmptyUpload:
        filename = "clip.webm"

        async def read(self):
            return b""

    with pytest.raises(HTTPException) as excinfo:
        await voice_settings.async_transcribe_runtime_voice_clip(
            MagicMock(),
            SimpleNamespace(id="user-1", org_id="org-1"),
            upload=EmptyUpload(),
        )

    assert excinfo.value.status_code == 422
