from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


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

    voice = await voice_settings.async_get_runtime_voice(session, user)

    assert voice.provider == "openai"
    assert voice.model == "gpt-realtime-whisper"
    assert voice.source == "memory"
    assert voice.status == "ready"
    assert voice.detail is None


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
        AsyncMock(return_value={"low": "gpt-5-mini", "medium": "gpt-5.4", "high": "gpt-5.5", "options": []}),
    )
    monkeypatch.setattr(
        runtime_service,
        "async_get_runtime_memory",
        AsyncMock(return_value=memory),
    )

    settings = await runtime_service.async_get_runtime_settings(session, user)

    assert settings.voice.model == "gpt-realtime-whisper"
    assert settings.voice.status == "ready"


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
    assert voice.status == "missing"
    assert "OpenAI API key" in (voice.detail or "")


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

    monkeypatch.setattr(
        voice_settings,
        "async_get_embedding_runtime_config",
        AsyncMock(return_value=SimpleNamespace(backend="api", provider="openai")),
    )
    monkeypatch.setattr(
        voice_settings,
        "_async_installation_embedding_api_key",
        AsyncMock(return_value="sk-memory-openai"),
    )
    monkeypatch.setattr(voice_settings, "_async_create_openai_realtime_client_secret", create_secret)

    result = await voice_settings.async_create_runtime_voice_session(session, user)

    assert result.provider == "openai"
    assert result.model == "gpt-realtime-whisper"
    assert result.client_secret == "ek_test_voice"
    assert result.expires_at == 1_800_000_000
    assert "sk-memory-openai" not in result.model
    create_secret.assert_awaited_once()
    assert create_secret.await_args.args[0] == "sk-memory-openai"


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
    assert audio_input["turn_detection"] is None


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
