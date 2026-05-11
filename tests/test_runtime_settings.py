from unittest.mock import MagicMock, patch

import pytest


def test_provider_auth_status_reports_openai_codex_runtime():
    from brain.systems.services.runtime_introspection import get_provider_auth_status

    mock_session = MagicMock()
    mock_session.scalars.return_value.first.side_effect = [1, None]

    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_uow.session = mock_session

    mock_llm = MagicMock(source="user_default", auth_mode="chatgpt", is_oauth=False)

    with patch("brain.systems.services.runtime_introspection.UnitOfWork", return_value=mock_uow), \
         patch("brain.systems.services.runtime_introspection.resolve_llm_client", return_value=mock_llm), \
         patch("brain.systems.services.runtime_introspection.resolve_default_provider", return_value="openai"):
        data = get_provider_auth_status(user_id="user-1", org_id="org-1", provider="openai")

    assert data["provider"] == "openai"
    assert data["effective_provider"] == "openai"
    assert data["status"] == "in_use"
    assert data["method"] == "chatgpt"
    assert data["runtime_key_source"] == "user_default"


def test_runtime_settings_tool_returns_model_mappings_and_active_status():
    import brain.systems.services.runtime_introspection as runtime_settings_service
    from brain.app.mcp.server import tool_runtime_settings

    with patch.object(runtime_settings_service, "get_runtime_settings_snapshot", return_value={
            "selected_provider": "openai",
            "effective_provider": "openai",
            "agency": {
                "recommendation_mode": True,
                "auto_execute_read_only": False,
            },
            "providers": {"openai": {"status": "in_use"}},
            "provider_model_mappings": {"openai": {"medium": "gpt-5.4"}},
            "worker_backend": {"agent_effective_worker_backend": "predict_rlm"},
            "active": {"provider": "openai", "status": "in_use"},
    }):
        data = tool_runtime_settings(provider="openai", user_id="user-1", org_id="org-1")

    assert data["selected_provider"] == "openai"
    assert data["active"]["status"] == "in_use"
    assert data["provider_model_mappings"]["openai"]["medium"] == "gpt-5.4"
    assert data["worker_backend"]["agent_effective_worker_backend"] == "predict_rlm"
    assert data["agency"]["recommendation_mode"] is True
    assert data["agency"]["auto_execute_read_only"] is False


def test_store_openai_connection_reports_invalid_format():
    from types import SimpleNamespace

    from brain.systems.runtime_settings.auth import store_openai_connection

    with pytest.raises(Exception) as exc:
        store_openai_connection(SimpleNamespace(id="user-1", org_id="org-1", role="owner"), "not-a-key")

    assert getattr(exc.value, "status_code", None) == 400
    assert "OpenAI API key or Codex auth JSON" in exc.value.detail


def test_store_openai_connection_reports_missing_vault_master_key(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.auth as auth_settings

    def raise_missing_vault_key(*args, **kwargs):
        raise RuntimeError("VAULT_MASTER_KEY is required. Refusing to auto-generate a vault key.")

    monkeypatch.setattr(auth_settings, "verify_provider_api_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_settings, "set_api_key", raise_missing_vault_key)

    with pytest.raises(Exception) as exc:
        auth_settings.store_openai_connection(
            SimpleNamespace(id="user-1", org_id="org-1", role="owner"),
            "sk-test",
        )

    assert getattr(exc.value, "status_code", None) == 503
    assert "VAULT_MASTER_KEY" in exc.value.detail


def test_runtime_models_default_high_uses_gpt_5_5(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.models as runtime_models

    monkeypatch.setattr(runtime_models, "get_provider_model_map", lambda *args, **kwargs: {}, raising=False)

    data = runtime_models.get_runtime_models(SimpleNamespace(id="user-1", org_id="org-1"))

    assert data.high == "gpt-5.5"
    assert any(option.key == "gpt-5.5" for option in data.options)


def test_connect_openai_embedding_key_updates_memory_and_org_runtime(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.auth as auth_settings
    import brain.systems.runtime_settings.memory as memory_settings

    memory_read = SimpleNamespace(embedder="openai", embedding_status="ready")
    configure = MagicMock()
    store_org_key = MagicMock()

    monkeypatch.setattr(auth_settings, "parse_provider_connect_token", lambda token, provider: (token, "api_key"))
    monkeypatch.setattr(auth_settings, "verify_provider_api_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_settings, "set_api_key", MagicMock(side_effect=AssertionError("runtime key should not be stored")))
    monkeypatch.setattr(auth_settings, "_store_org_openai_api_key", store_org_key)
    monkeypatch.setattr(memory_settings, "configure_openai_embedding_api_key", configure)
    monkeypatch.setattr(memory_settings, "get_runtime_memory", lambda user: memory_read)

    result = auth_settings.connect_openai_embedding_api_key(
        SimpleNamespace(id="user-1", org_id="org-1", role="owner"),
        "sk-test",
    )

    assert result is memory_read
    configure.assert_called_once_with("sk-test")
    store_org_key.assert_called_once()
    store_args, store_kwargs = store_org_key.call_args
    assert store_args[0].id == "user-1"
    assert store_args[0].org_id == "org-1"
    assert store_args[0].role == "owner"
    assert store_args[1] == "sk-test"
    assert store_kwargs == {"required": False}


def test_owner_openai_api_key_is_stored_as_org_runtime_key(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.auth as auth_settings

    store_org = MagicMock()
    monkeypatch.setattr("brain.systems.vault._encrypt", lambda token: b"encrypted-openai")
    monkeypatch.setattr(auth_settings, "store_org_api_key", store_org)

    stored = auth_settings._store_org_openai_api_key(
        SimpleNamespace(id="user-1", org_id="org-1", role="owner"),
        "sk-test",
    )

    assert stored is True
    store_org.assert_called_once_with(
        "org-1",
        "openai",
        b"encrypted-openai",
        label="Workspace OpenAI key",
        uow_factory=auth_settings.UnitOfWork,
    )


def test_runtime_settings_snapshot_includes_routing_marketplace(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.services.runtime_introspection as runtime_settings_service

    monkeypatch.setattr(
        runtime_settings_service,
        "get_routing_marketplace_snapshot",
        lambda **kwargs: {"flags": {"shadow": True}, "latest_decisions": []},
    )
    monkeypatch.setattr(runtime_settings_service, "get_provider_auth_status", lambda **kwargs: {"status": "in_use"}, raising=False)
    monkeypatch.setattr(runtime_settings_service, "get_provider_model_map", lambda *args, **kwargs: {"medium": "gpt-5.4"}, raising=False)
    monkeypatch.setattr(runtime_settings_service, "get_agent_worker_backend_settings", lambda **kwargs: SimpleNamespace(to_dict=lambda: {}), raising=False)
    monkeypatch.setattr(runtime_settings_service, "resolve_default_provider", lambda **kwargs: "openai", raising=False)

    snapshot = runtime_settings_service.get_runtime_settings_snapshot(user_id="user-1", org_id="org-1")

    assert snapshot["routing_marketplace"]["flags"]["shadow"] is True


def test_runtime_settings_snapshot_exposes_learning_policy(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.services.runtime_introspection as runtime_settings_service

    monkeypatch.setenv("LEARNING_POLICY_DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.setenv("LEARNING_POLICY_ALLOWED_MODEL_TIERS", "local,low")
    monkeypatch.setenv("LEARNING_POLICY_EXTERNAL_EVAL_EXPORT_ALLOWED", "false")
    monkeypatch.setattr(runtime_settings_service, "get_routing_marketplace_snapshot", lambda **kwargs: {}, raising=False)
    monkeypatch.setattr(runtime_settings_service, "get_provider_auth_status", lambda **kwargs: {"status": "in_use"}, raising=False)
    monkeypatch.setattr(runtime_settings_service, "get_provider_model_map", lambda *args, **kwargs: {"medium": "gpt-5.4"}, raising=False)
    monkeypatch.setattr(runtime_settings_service, "get_agent_worker_backend_settings", lambda **kwargs: SimpleNamespace(to_dict=lambda: {}), raising=False)
    monkeypatch.setattr(runtime_settings_service, "resolve_default_provider", lambda **kwargs: "openai", raising=False)

    snapshot = runtime_settings_service.get_runtime_settings_snapshot(user_id="user-1", org_id="org-1")

    assert snapshot["learning_policy"]["deployment_mode"] == "self_hosted"
    assert snapshot["learning_policy"]["allowed_model_tiers"] == ["local", "low"]
    assert snapshot["learning_policy"]["external_eval_export_allowed"] is False


def test_runtime_settings_snapshot_exposes_provider_health(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.services.runtime_introspection as runtime_settings_service
    from brain.platform.provider_health import record_provider_failure, reset_provider_health

    reset_provider_health()
    record_provider_failure(
        operation_type="scout",
        provider="openai",
        model="gpt-5.4-mini",
        exc="provider timed out",
    )

    monkeypatch.setattr(runtime_settings_service, "get_routing_marketplace_snapshot", lambda **kwargs: {}, raising=False)
    monkeypatch.setattr(runtime_settings_service, "get_provider_auth_status", lambda **kwargs: {"status": "in_use"}, raising=False)
    monkeypatch.setattr(runtime_settings_service, "get_provider_model_map", lambda *args, **kwargs: {"medium": "gpt-5.4"}, raising=False)
    monkeypatch.setattr(runtime_settings_service, "get_agent_worker_backend_settings", lambda **kwargs: SimpleNamespace(to_dict=lambda: {}), raising=False)
    monkeypatch.setattr(runtime_settings_service, "resolve_default_provider", lambda **kwargs: "openai", raising=False)

    snapshot = runtime_settings_service.get_runtime_settings_snapshot(user_id="user-1", org_id="org-1")

    entries = snapshot["provider_health"]["operations"]["scout"]
    assert entries[0]["provider"] == "openai"
    assert entries[0]["status"] == "unavailable"
    assert snapshot["provider_health"]["policies"]["scout"]["fail_open"] is True


def test_get_llm_info_uses_low_tier_for_background_models(monkeypatch):
    from types import SimpleNamespace

    from brain.app.api.routers.system import _get_llm_info

    org = SimpleNamespace(memory_model_config={
        "default_provider": "openai",
        "session_harvest": "openai:gpt-5.4-mini",
        "depth_0": "openai/gpt-5.4",
    })
    user_obj = SimpleNamespace(default_provider="openai")

    class FakeSession:
        def get(self, model, identifier):
            name = getattr(model, "__name__", "")
            if name == "Org":
                return org if identifier == "org-1" else None
            if name == "User":
                return user_obj if identifier == "user-1" else None
            return None

    class FakeUoW:
        def __enter__(self):
            self.session = FakeSession()
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("brain.app.api.routers.system.UnitOfWork", FakeUoW)
    monkeypatch.setattr(
        "brain.platform.providers.model_policy.get_provider_model_maps",
        lambda **kwargs: {"openai": {"low": "gpt-5-mini", "medium": "gpt-5.4"}},
    )
    monkeypatch.setattr("brain.platform.providers.model_policy.resolve_default_provider", lambda **kwargs: "openai")
    monkeypatch.setattr("brain.platform.providers.model_policy.get_model_for_tier", lambda *args, **kwargs: "gpt-5-mini")
    monkeypatch.setattr(
        "brain.app.api.routers.system.get_agent_worker_backend_settings",
        lambda **kwargs: SimpleNamespace(to_dict=lambda: {}),
        raising=False,
    )

    info = _get_llm_info({"id": "user-1", "org_id": "org-1"})

    assert info is not None
    assert info["harvest_model"] == "gpt-5-mini"
    assert info["consolidation_model"] == "gpt-5-mini"
    assert "cortex_default_concurrency" not in info


def test_get_llm_info_exposes_provider_health(monkeypatch):
    from types import SimpleNamespace

    from brain.app.api.routers.system import _get_llm_info
    from brain.platform.provider_health import record_provider_failure, reset_provider_health

    reset_provider_health()
    record_provider_failure(
        operation_type="verifier",
        provider="openai",
        model="gpt-5.4-mini",
        exc="verifier unavailable",
    )

    org = SimpleNamespace(memory_model_config={"default_provider": "openai"})
    user_obj = SimpleNamespace(default_provider="openai")

    class FakeSession:
        def get(self, model, identifier):
            name = getattr(model, "__name__", "")
            if name == "Org":
                return org if identifier == "org-1" else None
            if name == "User":
                return user_obj if identifier == "user-1" else None
            return None

    class FakeUoW:
        def __enter__(self):
            self.session = FakeSession()
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("brain.app.api.routers.system.UnitOfWork", FakeUoW)
    monkeypatch.setattr("brain.platform.providers.model_policy.get_provider_model_maps", lambda **kwargs: {"openai": {"medium": "gpt-5.4"}})
    monkeypatch.setattr("brain.platform.providers.model_policy.resolve_default_provider", lambda **kwargs: "openai")
    monkeypatch.setattr("brain.platform.providers.model_policy.get_model_for_tier", lambda *args, **kwargs: "gpt-5-mini")
    monkeypatch.setattr(
        "brain.app.api.routers.system.get_agent_worker_backend_settings",
        lambda **kwargs: SimpleNamespace(to_dict=lambda: {}),
        raising=False,
    )

    info = _get_llm_info({"id": "user-1", "org_id": "org-1"})

    assert info is not None
    assert info["provider_health"]["operations"]["verifier"][0]["status"] == "unavailable"


def test_runtime_memory_reports_installation_scope_and_installation_keys(monkeypatch):
    import base64
    import json
    from types import SimpleNamespace

    import brain.kernel.config as cfg
    import brain.systems.runtime_settings.memory as memory_settings
    import brain.systems.vault as vault

    stored_config = {
        memory_settings.RUNTIME_MEMORY_SETTINGS_KEY: json.dumps({
            "backend": "api",
            "provider": "gemini",
            "api_model": "gemini-embedding-2",
            "cpu_model": "all-MiniLM-L6-v2",
            "dimensions": 768,
            "reranker": "weighted",
        }),
        memory_settings._runtime_secret_config_key("gemini"): base64.b64encode(b"encrypted:gemini-key").decode(),
    }

    monkeypatch.setattr(cfg, "EMBEDDING_BACKEND", "api", raising=False)
    monkeypatch.setattr(cfg, "EMBEDDING_API_PROVIDER", "gemini", raising=False)
    monkeypatch.setattr(memory_settings, "_read_runtime_config_value", lambda key: stored_config.get(key))
    monkeypatch.setattr(vault, "_decrypt", lambda token: token.decode().removeprefix("encrypted:"))
    monkeypatch.setattr(memory_settings, "_indexed_vector_count", lambda: 0)

    data = memory_settings.get_runtime_memory(SimpleNamespace(id="user-1", org_id="org-1"))

    assert data.scope == "installation"
    assert data.embedder == "gemini"
    assert data.embedding_model == "gemini-embedding-2"
    assert data.api_key_statuses == {"openai": False, "gemini": True}


def test_runtime_memory_does_not_use_env_api_keys(monkeypatch):
    import brain.systems.runtime_settings.memory as memory_settings

    monkeypatch.setenv("GEMINI_API_KEY", "gemini-env-key")
    monkeypatch.setenv("EMBEDDING_API_KEY", "embedding-env-key")
    monkeypatch.setattr(memory_settings, "_read_runtime_config_value", lambda key: None)

    assert memory_settings._installation_embedding_api_key("gemini") is None


def test_update_runtime_memory_blocks_embedder_change_when_installation_vectors_exist(monkeypatch):
    from types import SimpleNamespace

    import brain.kernel.config as cfg
    import brain.systems.runtime_settings.memory as memory_settings
    from brain.systems.runtime_settings.schemas import RuntimeMemoryUpdate

    monkeypatch.setenv("EMBEDDING_BACKEND", "cpu")
    monkeypatch.setenv("EMBEDDING_DIM", "384")
    monkeypatch.setattr(cfg, "EMBEDDING_BACKEND", "cpu", raising=False)
    monkeypatch.setattr(cfg, "EMBEDDING_DIM", 384, raising=False)
    monkeypatch.setattr(memory_settings, "_indexed_vector_count", lambda: 7)

    with pytest.raises(memory_settings.HTTPException) as exc:
        memory_settings.update_runtime_memory(
            SimpleNamespace(id="user-1", org_id="org-1"),
            RuntimeMemoryUpdate(embedder="gemini", embedding_model="gemini-embedding-2", reranker="weighted"),
        )

    assert exc.value.status_code == 409
    assert "installation memory vectors already exist" in exc.value.detail


def test_update_runtime_memory_writes_installation_reranker(monkeypatch):
    from types import SimpleNamespace

    import brain.kernel.config as cfg
    import brain.systems.runtime_settings.memory as memory_settings
    from brain.systems.runtime_settings.schemas import RuntimeMemoryUpdate

    captured = {}

    monkeypatch.setenv("EMBEDDING_BACKEND", "cpu")
    monkeypatch.setenv("EMBEDDING_DIM", "384")
    monkeypatch.setattr(cfg, "EMBEDDING_BACKEND", "cpu", raising=False)
    monkeypatch.setattr(cfg, "EMBEDDING_DIM", 384, raising=False)
    monkeypatch.setattr(memory_settings, "_indexed_vector_count", lambda: 0)
    monkeypatch.setattr(
        memory_settings,
        "_save_embedding_runtime_config",
        lambda config, **kwargs: captured.update(config.stored_settings()),
    )

    memory_settings.update_runtime_memory(
        SimpleNamespace(id="user-1", org_id="org-1"),
        RuntimeMemoryUpdate(embedder="local_cpu", embedding_model=None, reranker="weighted"),
    )

    assert captured["backend"] == "cpu"
    assert captured["reranker"] == "weighted"


def test_openai_memory_key_persists_in_encrypted_runtime_store(monkeypatch):
    import base64
    import json

    import brain.systems.runtime_settings.memory as memory_settings
    import brain.systems.vault as vault

    stored_config = {}

    monkeypatch.setattr(memory_settings, "_read_runtime_config_value", lambda key: stored_config.get(key))
    monkeypatch.setattr(memory_settings, "_write_runtime_config_value", lambda key, value: stored_config.__setitem__(key, value))
    monkeypatch.setattr(memory_settings, "_sync_gpu_embedding_worker", lambda backend: None)
    monkeypatch.setattr(vault, "_encrypt", lambda value: f"encrypted:{value}".encode())
    monkeypatch.setattr(vault, "_decrypt", lambda token: token.decode().removeprefix("encrypted:"))

    memory_settings.configure_openai_embedding_api_key("sk-test-memory")

    runtime = json.loads(stored_config[memory_settings.RUNTIME_MEMORY_SETTINGS_KEY])
    assert runtime["backend"] == "api"
    assert runtime["provider"] == "openai"
    assert runtime["api_model"] == "text-embedding-3-small"
    assert runtime["dimensions"] == 768
    assert "api_key" not in runtime

    encrypted = stored_config[memory_settings._runtime_secret_config_key("openai")]
    assert encrypted != "sk-test-memory"
    assert base64.b64decode(encrypted).decode() == "encrypted:sk-test-memory"
    assert memory_settings._installation_embedding_api_key("openai") == "sk-test-memory"


def test_openai_memory_key_reports_missing_vault_master_key(monkeypatch):
    import pytest

    import brain.systems.runtime_settings.memory as memory_settings
    import brain.systems.vault as vault

    def missing_key(_value):
        raise RuntimeError("VAULT_MASTER_KEY is required. Refusing to auto-generate a vault key.")

    monkeypatch.setattr(vault, "_encrypt", missing_key)

    with pytest.raises(memory_settings.HTTPException) as exc:
        memory_settings.configure_openai_embedding_api_key("sk-test-memory")

    assert exc.value.status_code == 503
    assert "VAULT_MASTER_KEY" in exc.value.detail


def test_embedding_api_reads_persisted_runtime_config(monkeypatch):
    import numpy as np

    import brain.systems.memory.embeddings as embeddings
    from brain.systems.runtime_settings.memory import EmbeddingRuntimeConfig

    captured = {}

    class Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    def post(url, *, headers, json, timeout):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return Response()

    monkeypatch.setattr(
        embeddings,
        "_runtime_embedding_config",
        lambda: EmbeddingRuntimeConfig(
            backend="api",
            provider="openai",
            api_model="text-embedding-3-small",
            cpu_model="all-MiniLM-L6-v2",
            dimensions=768,
            api_key="sk-db-memory",
        ),
    )
    monkeypatch.setattr("httpx.post", post)

    vector = embeddings._embed_api_openai(["hello"], "query")

    assert isinstance(vector, np.ndarray)
    assert captured["headers"]["Authorization"] == "Bearer sk-db-memory"
    assert captured["json"]["dimensions"] == 768


def test_connect_gemini_api_key_requires_installation_admin(monkeypatch):
    from types import SimpleNamespace

    from brain.systems.runtime_settings.auth import connect_gemini_api_key

    with pytest.raises(Exception) as exc:
        connect_gemini_api_key(SimpleNamespace(id="user-1", org_id="org-1", role="member"), "gemini-key")

    assert getattr(exc.value, "status_code", None) == 403


def test_start_runtime_update_launches_detached_safe_deploy(monkeypatch, tmp_path):
    import brain.systems.runtime_settings.self_update as self_update

    root = tmp_path / "repo"
    state_dir = tmp_path / "state"
    launcher = root / "illo"
    (root / ".git").mkdir(parents=True)
    launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    popen_calls = []

    class Proc:
        pid = 4242

    def fake_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return Proc()

    monkeypatch.setenv("ILLO_SELF_UPDATE_ROOT", str(root))
    monkeypatch.setenv("ILLO_SELF_UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.delenv("ILLO_SELF_UPDATE_COMMAND", raising=False)
    monkeypatch.delenv("ILLO_SELF_UPDATE_REQUEST_FILE", raising=False)
    monkeypatch.setattr(self_update, "_active_agent_run_count", lambda: 2)
    monkeypatch.setattr(self_update, "_pid_running", lambda _pid: False)
    monkeypatch.setattr(self_update.subprocess, "Popen", fake_popen)

    status = self_update.start_runtime_update(requested_by="owner-1")

    assert status.status == "running"
    assert status.available is True
    assert status.pid == 4242
    assert status.active_agent_runs == 2
    assert "active AgentRun" in (status.detail or "")
    command, kwargs = popen_calls[0]
    assert command == ["bash", str(launcher), "update"]
    assert kwargs["cwd"] == str(root)
    assert kwargs["stdin"] is self_update.subprocess.DEVNULL
    assert kwargs["stderr"] is self_update.subprocess.STDOUT
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert (state_dir / "illo-self-update.pid").read_text(encoding="utf-8").strip() == "4242"


def test_start_runtime_update_reuses_running_update(monkeypatch, tmp_path):
    import brain.systems.runtime_settings.self_update as self_update

    root = tmp_path / "repo"
    state_dir = tmp_path / "state"
    (root / ".git").mkdir(parents=True)
    (root / "illo").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    state_dir.mkdir()
    (state_dir / "illo-self-update.json").write_text(
        '{"pid": 5150, "started_at": "2026-05-11T12:00:00+00:00"}',
        encoding="utf-8",
    )

    monkeypatch.setenv("ILLO_SELF_UPDATE_ROOT", str(root))
    monkeypatch.setenv("ILLO_SELF_UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.delenv("ILLO_SELF_UPDATE_COMMAND", raising=False)
    monkeypatch.delenv("ILLO_SELF_UPDATE_REQUEST_FILE", raising=False)
    monkeypatch.setattr(self_update, "_active_agent_run_count", lambda: 1)
    monkeypatch.setattr(self_update, "_pid_running", lambda pid: pid == 5150)
    monkeypatch.setattr(
        self_update.subprocess,
        "Popen",
        MagicMock(side_effect=AssertionError("should not launch a duplicate update")),
    )

    status = self_update.start_runtime_update(requested_by="owner-1")

    assert status.status == "running"
    assert status.pid == 5150
    assert status.detail == "Illospace update is already running."


def test_start_runtime_update_rejects_non_checkout_without_override(monkeypatch, tmp_path):
    import brain.systems.runtime_settings.self_update as self_update

    root = tmp_path / "repo"
    state_dir = tmp_path / "state"
    root.mkdir()
    (root / "illo").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    monkeypatch.setenv("ILLO_SELF_UPDATE_ROOT", str(root))
    monkeypatch.setenv("ILLO_SELF_UPDATE_STATE_DIR", str(state_dir))
    monkeypatch.delenv("ILLO_SELF_UPDATE_COMMAND", raising=False)
    monkeypatch.delenv("ILLO_SELF_UPDATE_REQUEST_FILE", raising=False)

    with pytest.raises(Exception) as exc:
        self_update.start_runtime_update(requested_by="owner-1")

    assert getattr(exc.value, "status_code", None) == 409
    assert "not running from a git checkout" in exc.value.detail


def test_start_runtime_update_queues_compose_sidecar_request(monkeypatch, tmp_path):
    import json

    import brain.systems.runtime_settings.self_update as self_update

    request_file = tmp_path / "self-update" / "request.json"
    status_file = tmp_path / "self-update" / "status.json"
    log_path = tmp_path / "logs" / "illo-self-update.log"

    monkeypatch.setenv("ILLO_SELF_UPDATE_REQUEST_FILE", str(request_file))
    monkeypatch.setenv("ILLO_SELF_UPDATE_STATUS_FILE", str(status_file))
    monkeypatch.setenv("ILLO_SELF_UPDATE_LOG_PATH", str(log_path))
    monkeypatch.setattr(self_update, "_active_agent_run_count", lambda: 3)

    status = self_update.start_runtime_update(requested_by="owner-1")

    assert status.status == "running"
    assert status.available is True
    assert status.pid is None
    assert status.active_agent_runs == 3
    assert status.log_path == str(log_path)
    assert "queued" in (status.detail or "")
    request_payload = json.loads(request_file.read_text(encoding="utf-8"))
    status_payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert request_payload["requested_by"] == "owner-1"
    assert status_payload["status"] == "queued"


def test_runtime_update_status_reports_compose_sidecar_available(monkeypatch, tmp_path):
    import brain.systems.runtime_settings.self_update as self_update

    request_file = tmp_path / "self-update" / "request.json"
    status_file = tmp_path / "self-update" / "status.json"
    status_file.parent.mkdir()
    status_file.write_text(
        '{"status": "idle", "detail": "Compose updater sidecar is ready."}',
        encoding="utf-8",
    )

    monkeypatch.setenv("ILLO_SELF_UPDATE_REQUEST_FILE", str(request_file))
    monkeypatch.setenv("ILLO_SELF_UPDATE_STATUS_FILE", str(status_file))
    monkeypatch.setattr(self_update, "_active_agent_run_count", lambda: 0)

    status = self_update.get_runtime_update_status()

    assert status.status == "idle"
    assert status.available is True
    assert status.detail == "Compose updater sidecar is ready."


def test_runtime_update_status_waits_for_compose_sidecar_heartbeat(monkeypatch, tmp_path):
    import brain.systems.runtime_settings.self_update as self_update

    request_file = tmp_path / "self-update" / "request.json"
    heartbeat_file = tmp_path / "self-update" / "heartbeat.json"

    monkeypatch.setenv("ILLO_SELF_UPDATE_REQUEST_FILE", str(request_file))
    monkeypatch.setenv("ILLO_SELF_UPDATE_HEARTBEAT_FILE", str(heartbeat_file))
    monkeypatch.setattr(self_update, "_active_agent_run_count", lambda: 0)

    status = self_update.get_runtime_update_status()

    assert status.status == "idle"
    assert status.available is False
    assert "waiting for the Compose updater sidecar" in (status.detail or "")
