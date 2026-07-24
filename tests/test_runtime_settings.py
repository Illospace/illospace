from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_provider_auth_status_reports_openai_codex_runtime():
    from types import SimpleNamespace

    from brain.platform.db.models.org import UserCodexConnection
    from brain.systems.services.runtime_introspection import async_get_provider_auth_status

    mock_session = MagicMock()
    mock_session.scalars = AsyncMock(
        side_effect=[
            SimpleNamespace(first=lambda: 1),
            SimpleNamespace(first=lambda: None),
            SimpleNamespace(first=lambda: SimpleNamespace(id=123, label=None)),
        ]
    )

    mock_llm = MagicMock(source="codex_subscription", auth_mode="chatgpt", is_oauth=False)

    with patch("brain.systems.services.runtime_introspection.async_resolve_llm_client", AsyncMock(return_value=mock_llm)), \
         patch("brain.systems.services.runtime_introspection.async_resolve_default_provider", AsyncMock(return_value="openai")):
        data = await async_get_provider_auth_status(mock_session, user_id="user-1", org_id="org-1", provider="openai")

    assert data["provider"] == "openai"
    assert data["effective_provider"] == "openai"
    assert data["status"] == "in_use"
    assert data["method"] == "chatgpt"
    assert data["runtime_key_source"] == "codex_subscription"
    stmt = mock_session.scalars.await_args_list[-1].args[0]
    assert UserCodexConnection.__tablename__ in str(stmt)


@pytest.mark.asyncio
async def test_provider_auth_status_reports_user_openai_api_key_runtime():
    from types import SimpleNamespace

    from brain.platform.db.models.org import UserCodexConnection
    from brain.systems.services.runtime_introspection import async_get_provider_auth_status

    mock_session = MagicMock()
    mock_session.scalars = AsyncMock(
        side_effect=[
            SimpleNamespace(first=lambda: 1),
            SimpleNamespace(first=lambda: None),
            SimpleNamespace(first=lambda: SimpleNamespace(id=321, label="Personal OpenAI")),
        ]
    )

    mock_llm = MagicMock(source="user_openai", auth_mode="api_key", is_oauth=False)

    with patch("brain.systems.services.runtime_introspection.async_resolve_llm_client", AsyncMock(return_value=mock_llm)), \
         patch("brain.systems.services.runtime_introspection.async_resolve_default_provider", AsyncMock(return_value="openai")):
        data = await async_get_provider_auth_status(mock_session, user_id="user-1", org_id="org-1", provider="openai")

    assert data["provider"] == "openai"
    assert data["status"] == "in_use"
    assert data["method"] == "api_key"
    assert data["runtime_key_source"] == "user_openai"
    assert data["runtime_key_scope"] == "user"
    assert data["runtime_key_label"] == "your OpenAI API key"
    assert data["runtime_uses_db_key"] is True
    assert data["runtime_credential_id"] == 321
    assert data["runtime_credential_name"] == "Personal OpenAI"
    stmt = mock_session.scalars.await_args_list[-1].args[0]
    assert UserCodexConnection.__tablename__ in str(stmt)


@pytest.mark.asyncio
async def test_openai_connection_reports_personal_and_org_key_flags(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.auth as auth_settings

    monkeypatch.setattr(
        auth_settings,
        "async_get_provider_auth_status",
        AsyncMock(return_value={
            "runtime_key_available": True,
            "method": "api_key",
            "runtime_key_source": "user_openai",
            "has_codex_subscription": True,
            "has_org_db_key": True,
        }),
    )

    connection = await auth_settings.async_get_openai_connection(
        MagicMock(),
        SimpleNamespace(id="user-1", org_id="org-1"),
    )

    assert connection.status == "connected"
    assert connection.source == "user_openai"
    assert connection.has_personal_connection is True
    assert connection.has_org_key is True


@pytest.mark.asyncio
async def test_runtime_settings_tool_returns_model_catalogs_and_active_status():
    import brain.systems.services.runtime_introspection as runtime_settings_service
    from brain.app.mcp.server import tool_runtime_settings

    mock_uow = MagicMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    mock_uow.session = MagicMock()

    with patch("brain.app.mcp.server.UnitOfWork", return_value=mock_uow), \
         patch.object(runtime_settings_service, "async_get_runtime_settings_snapshot", AsyncMock(return_value={
            "selected_provider": "openai",
            "effective_provider": "openai",
            "providers": {"openai": {"status": "in_use"}},
            "default_model": "gpt-5.5",
            "provider_model_catalogs": {"openai": {"default": "gpt-5.5", "options": ["gpt-5.5"]}},
            "worker_backend": {"agent_effective_worker_backend": "predict_rlm"},
            "active": {"provider": "openai", "status": "in_use"},
         })):
        data = await tool_runtime_settings(provider="openai", user_id="user-1", org_id="org-1")

    assert data["selected_provider"] == "openai"
    assert data["active"]["status"] == "in_use"
    assert data["provider_model_catalogs"]["openai"]["default"] == "gpt-5.5"
    assert data["worker_backend"]["agent_effective_worker_backend"] == "predict_rlm"


@pytest.mark.asyncio
async def test_manage_deployment_tool_requires_authenticated_user():
    import brain.systems.runtime_settings.self_update as self_update
    from brain.app.mcp.server import tool_manage_deployment

    mock_uow = MagicMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    mock_uow.session = MagicMock()

    with patch("brain.app.mcp.server.UnitOfWork", return_value=mock_uow), \
         patch.object(self_update, "async_start_runtime_update", AsyncMock(side_effect=AssertionError("must not start"))):
        data = await tool_manage_deployment(action="start_update", user_id=None, org_id="org-1")

    assert data["status"] == "denied"
    assert "authenticated workspace user" in data["detail"]


@pytest.mark.asyncio
async def test_manage_deployment_tool_starts_update_for_workspace_member():
    from types import SimpleNamespace

    import brain.systems.runtime_settings.self_update as self_update
    from brain.app.mcp.server import tool_manage_deployment
    from brain.systems.runtime_settings.schemas import RuntimeUpdateRead

    mock_uow = MagicMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    mock_uow.session = MagicMock()
    mock_uow.session.get = AsyncMock(return_value=SimpleNamespace(id="user-1", org_id="org-1", role="member"))
    update = RuntimeUpdateRead(
        status="running",
        available=True,
        pid=None,
        active_agent_runs=0,
        log_path="/data/private/logs/illo-self-update.log",
        detail="Illospace update queued for the Compose updater sidecar.",
    )

    with patch("brain.app.mcp.server.UnitOfWork", return_value=mock_uow), \
         patch.object(self_update, "async_start_runtime_update", AsyncMock(return_value=update)) as start_update:
        data = await tool_manage_deployment(
            action="start_update",
            build_no_cache=True,
            worker_drain_timeout_seconds=45,
            user_id="user-1",
            org_id="org-1",
        )

    assert data["action"] == "start_update"
    assert data["status"] == "running"
    start_update.assert_awaited_once()
    _, kwargs = start_update.await_args
    assert kwargs["requested_by"] == "user-1"
    assert kwargs["build_no_cache"] is True
    assert kwargs["worker_drain_timeout_seconds"] == 45


@pytest.mark.asyncio
async def test_manage_deployment_tool_requires_active_org_membership():
    from types import SimpleNamespace

    import brain.systems.runtime_settings.self_update as self_update
    from brain.app.mcp.server import tool_manage_deployment

    mock_uow = MagicMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    mock_uow.session = MagicMock()
    mock_uow.session.get = AsyncMock(return_value=SimpleNamespace(id="user-1", org_id="other-org", role="member"))

    with patch("brain.app.mcp.server.UnitOfWork", return_value=mock_uow), \
         patch.object(self_update, "async_start_runtime_update", AsyncMock(side_effect=AssertionError("must not start"))):
        data = await tool_manage_deployment(action="start_update", user_id="user-1", org_id="org-1")

    assert data["status"] == "denied"
    assert "active organization" in data["detail"]


@pytest.mark.asyncio
async def test_manage_runtime_services_tool_requires_authenticated_user():
    import brain.systems.runtime_settings.runtime_services as runtime_services
    from brain.app.mcp.server import tool_manage_runtime_services

    mock_uow = MagicMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    mock_uow.session = MagicMock()

    with patch("brain.app.mcp.server.UnitOfWork", return_value=mock_uow), \
         patch.object(runtime_services, "async_restart_runtime_services", AsyncMock(side_effect=AssertionError("must not restart"))):
        data = await tool_manage_runtime_services(action="restart", services=["api"], user_id=None, org_id="org-1")

    assert data["status"] == "denied"
    assert "authenticated workspace user" in data["detail"]


@pytest.mark.asyncio
async def test_manage_runtime_services_tool_restarts_multiple_services_for_workspace_member():
    from types import SimpleNamespace

    import brain.systems.runtime_settings.runtime_services as runtime_services
    from brain.app.mcp.server import tool_manage_runtime_services
    from brain.systems.runtime_settings.schemas import RuntimeServiceRead, RuntimeServicesRead

    mock_uow = MagicMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    mock_uow.session = MagicMock()
    mock_uow.session.get = AsyncMock(return_value=SimpleNamespace(id="user-1", org_id="org-1", role="member"))
    status = RuntimeServicesRead(
        status="running",
        available=True,
        services=[
            RuntimeServiceRead(id="api", name="Illospace API", description="HTTP API"),
            RuntimeServiceRead(id="worker", name="Agent worker", description="AgentRun worker"),
        ],
        requested_services=["api", "worker"],
        log_path="/data/private/logs/illo-runtime-services.log",
        detail="Runtime service restart queued for the host controller.",
    )

    with patch("brain.app.mcp.server.UnitOfWork", return_value=mock_uow), \
         patch.object(runtime_services, "async_restart_runtime_services", AsyncMock(return_value=status)) as restart:
        data = await tool_manage_runtime_services(
            action="restart",
            services=["api", "worker"],
            user_id="user-1",
            org_id="org-1",
        )

    assert data["action"] == "restart"
    assert data["status"] == "running"
    assert data["requested_services"] == ["api", "worker"]
    restart.assert_awaited_once_with(["api", "worker"], requested_by="user-1")


@pytest.mark.asyncio
async def test_runtime_services_queues_restart_request(monkeypatch, tmp_path):
    import json
    from datetime import datetime, timezone

    from brain.systems.runtime_settings.runtime_services import async_restart_runtime_services

    request_file = tmp_path / "runtime-services" / "request.json"
    status_file = tmp_path / "runtime-services" / "status.json"
    heartbeat_file = tmp_path / "self-update" / "heartbeat.json"
    heartbeat_file.parent.mkdir(parents=True)
    heartbeat_file.write_text(
        json.dumps({"status": "ready", "updated_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ILLO_RUNTIME_SERVICES_REQUEST_FILE", str(request_file))
    monkeypatch.setenv("ILLO_RUNTIME_SERVICES_STATUS_FILE", str(status_file))
    monkeypatch.setenv("ILLO_RUNTIME_SERVICES_HEARTBEAT_FILE", str(heartbeat_file))

    result = await async_restart_runtime_services(["api", "worker"], requested_by="user-1")

    assert result.status == "running"
    assert result.requested_services == ["api", "worker"]
    payload = json.loads(request_file.read_text(encoding="utf-8"))
    assert payload["action"] == "restart"
    assert payload["services"] == ["api", "worker"]
    status = json.loads(status_file.read_text(encoding="utf-8"))
    assert status["detail"] == "Runtime service restart queued for the host controller."


@pytest.mark.asyncio
async def test_runtime_services_requires_dedicated_controller_heartbeat(monkeypatch, tmp_path):
    import json
    from datetime import datetime, timezone

    from fastapi import HTTPException

    from brain.systems.runtime_settings.runtime_services import (
        async_get_runtime_services_status,
        async_restart_runtime_services,
    )

    request_file = tmp_path / "runtime-services" / "request.json"
    status_file = tmp_path / "runtime-services" / "status.json"
    runtime_heartbeat_file = tmp_path / "runtime-services" / "heartbeat.json"
    self_update_heartbeat_file = tmp_path / "self-update" / "heartbeat.json"
    request_file.parent.mkdir(parents=True)
    self_update_heartbeat_file.parent.mkdir(parents=True)
    request_file.write_text(
        json.dumps({"action": "restart", "services": ["slack_connector"]}),
        encoding="utf-8",
    )
    status_file.write_text(
        json.dumps(
            {
                "status": "queued",
                "detail": "Runtime service restart queued for the host controller.",
                "services": ["slack_connector"],
            }
        ),
        encoding="utf-8",
    )
    self_update_heartbeat_file.write_text(
        json.dumps({"status": "ready", "updated_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ILLO_RUNTIME_SERVICES_REQUEST_FILE", str(request_file))
    monkeypatch.setenv("ILLO_RUNTIME_SERVICES_STATUS_FILE", str(status_file))
    monkeypatch.setenv("ILLO_RUNTIME_SERVICES_HEARTBEAT_FILE", str(runtime_heartbeat_file))
    monkeypatch.setenv("ILLO_SELF_UPDATE_HEARTBEAT_FILE", str(self_update_heartbeat_file))

    result = await async_get_runtime_services_status()

    assert result.available is False
    assert result.status == "idle"
    assert result.requested_services == ["slack_connector"]
    assert result.detail == "Runtime service management is waiting for the host controller."

    with pytest.raises(HTTPException) as exc:
        await async_restart_runtime_services(["slack_connector"], requested_by="user-1")
    assert exc.value.status_code == 409
    assert exc.value.detail == "Runtime service management is waiting for the host controller."


@pytest.mark.asyncio
async def test_runtime_update_http_endpoint_allows_workspace_member():
    from types import SimpleNamespace

    import brain.systems.runtime_settings.router as router
    from brain.systems.runtime_settings.schemas import RuntimeUpdateRead

    update = RuntimeUpdateRead(
        status="idle",
        available=True,
        pid=None,
        active_agent_runs=0,
        log_path="/data/private/logs/illo-self-update.log",
        detail="Queues the update for the Compose updater sidecar.",
    )
    user = SimpleNamespace(id="user-1", org_id="org-1", role="member")

    with patch.object(router, "async_get_runtime_update_status", AsyncMock(return_value=update)) as get_status, \
         patch.object(router, "async_start_runtime_update", AsyncMock(return_value=update)) as start_update:
        assert await router.read_runtime_update(user=user, db=MagicMock()) == update
        assert await router.start_illospace_update(user=user, db=MagicMock()) == update

    get_status.assert_awaited_once()
    start_update.assert_awaited_once()
    _, kwargs = start_update.await_args
    assert kwargs["requested_by"] == "user-1"


@pytest.mark.asyncio
async def test_store_openai_connection_reports_invalid_format():
    from types import SimpleNamespace

    from brain.systems.runtime_settings.auth import async_store_openai_connection

    with pytest.raises(Exception) as exc:
        await async_store_openai_connection(
            MagicMock(),
            SimpleNamespace(id="user-1", org_id="org-1", role="owner"),
            "not-a-key",
        )

    assert getattr(exc.value, "status_code", None) == 400
    assert "OpenAI API key or Codex auth JSON" in exc.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "owner"])
async def test_store_openai_api_key_uses_personal_runtime_connection(monkeypatch, role):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.auth as auth_settings
    from brain.systems.runtime_settings.schemas import RuntimeConnectionRead

    user = SimpleNamespace(id="user-1", org_id="org-1", role=role)
    session = MagicMock()
    session.get = AsyncMock(return_value=user)
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    store_user_connection = AsyncMock(return_value=7)
    store_org_connection = AsyncMock(side_effect=AssertionError("model keys must stay user-scoped"))
    read_connection = AsyncMock(return_value=RuntimeConnectionRead(
        status="connected",
        setup_required=False,
        method="api_key",
        source="user_openai",
        label="OpenAI API key",
    ))

    monkeypatch.setattr(auth_settings, "verify_provider_api_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_settings, "async_set_user_codex_connection", store_user_connection)
    monkeypatch.setattr(auth_settings, "_async_store_org_openai_api_key", store_org_connection)
    monkeypatch.setattr(auth_settings, "async_get_openai_connection", read_connection)

    result = await auth_settings.async_store_openai_connection(session, user, "sk-test")

    assert result.source == "user_openai"
    store_user_connection.assert_awaited_once()
    assert store_user_connection.await_args.args[:2] == ("user-1", "sk-test")
    assert store_user_connection.await_args.kwargs["label"] == "OpenAI API key"
    assert store_user_connection.await_args.kwargs["session"] is session
    store_org_connection.assert_not_called()
    session.flush.assert_awaited_once()
    session.refresh.assert_awaited_once_with(user)


@pytest.mark.asyncio
async def test_store_openai_org_api_key_rotates_workspace_runtime_key(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.auth as auth_settings
    from brain.systems.runtime_settings.schemas import RuntimeConnectionRead

    session = MagicMock()
    store_org_connection = AsyncMock(return_value=True)
    store_user_connection = AsyncMock(side_effect=AssertionError("org key must not replace personal key"))
    read_connection = AsyncMock(return_value=RuntimeConnectionRead(
        status="connected",
        setup_required=False,
        method="api_key",
        source="org_main",
        label="OpenAI API key",
        has_org_key=True,
    ))

    monkeypatch.setattr(auth_settings, "parse_provider_connect_token", lambda token, provider: (token, "api_key"))
    monkeypatch.setattr(auth_settings, "verify_provider_api_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_settings, "_async_store_org_openai_api_key", store_org_connection)
    monkeypatch.setattr(auth_settings, "async_set_user_codex_connection", store_user_connection)
    monkeypatch.setattr(auth_settings, "async_get_openai_connection", read_connection)

    user = SimpleNamespace(id="owner-1", org_id="org-1", role="owner")

    result = await auth_settings.async_connect_openai_org_api_key(session, user, "sk-org-rotated")

    assert result.has_org_key is True
    store_org_connection.assert_awaited_once()
    assert store_org_connection.await_args.args == (session, user, "sk-org-rotated")
    store_user_connection.assert_not_called()
    read_connection.assert_awaited_once_with(session, user)


@pytest.mark.asyncio
async def test_store_openai_org_api_key_rejects_members(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.auth as auth_settings

    store_org_connection = AsyncMock()
    monkeypatch.setattr(auth_settings, "_async_store_org_openai_api_key", store_org_connection)

    with pytest.raises(Exception) as exc:
        await auth_settings.async_connect_openai_org_api_key(
            MagicMock(),
            SimpleNamespace(id="member-1", org_id="org-1", role="member"),
            "sk-org-rotated",
        )

    assert exc.value.status_code == 403
    store_org_connection.assert_not_called()


@pytest.mark.asyncio
async def test_store_openai_connection_reports_missing_vault_master_key(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.auth as auth_settings

    async def raise_missing_vault_key(*args, **kwargs):
        raise RuntimeError("VAULT_MASTER_KEY is required. Refusing to auto-generate a vault key.")

    monkeypatch.setattr(auth_settings, "verify_provider_api_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_settings, "async_set_user_codex_connection", raise_missing_vault_key)

    with pytest.raises(Exception) as exc:
        await auth_settings.async_store_openai_connection(
            MagicMock(),
            SimpleNamespace(id="user-1", org_id="org-1", role="owner"),
            "sk-test",
        )

    assert getattr(exc.value, "status_code", None) == 503
    assert "VAULT_MASTER_KEY" in exc.value.detail


@pytest.mark.asyncio
async def test_runtime_models_default_uses_configured_model(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.models as runtime_models

    monkeypatch.setattr(
        runtime_models,
        "async_get_default_model",
        AsyncMock(return_value="openai/gpt-5.5"),
        raising=False,
    )
    monkeypatch.setattr(runtime_models, "async_get_default_thinking", AsyncMock(return_value="xhigh"), raising=False)

    data = await runtime_models.async_get_runtime_models(MagicMock(), SimpleNamespace(id="user-1", org_id="org-1"))

    assert data.default == "openai/gpt-5.5"
    assert data.thinking == "xhigh"
    assert any(option.id == "openai/gpt-5.6-sol" for option in data.catalog)
    assert any(option.id == "anthropic/claude-sonnet-5" for option in data.catalog)
    workspace_default = next(
        option for option in data.catalog if option.id == "openai/gpt-5.5"
    )
    assert workspace_default.default_provenance.workspace_default is True
    assert any(option.key == "none" for option in data.thinking_options)
    assert any(option.key == "xhigh" for option in data.thinking_options)


@pytest.mark.asyncio
async def test_runtime_models_update_persists_workspace_model_and_effort(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.models as runtime_models
    from brain.systems.runtime_settings.schemas import RuntimeModelsUpdate

    org = SimpleNamespace(memory_model_config={"default_provider": "openai"})

    class FakeSession:
        async def get(self, _model, identifier):
            assert identifier == "org-1"
            return org

        async def flush(self):
            return None

    expected = SimpleNamespace(default="gpt-5.6-sol", thinking="xhigh")
    monkeypatch.setattr(
        runtime_models,
        "async_get_runtime_models",
        AsyncMock(return_value=expected),
    )

    result = await runtime_models.async_update_runtime_models(
        FakeSession(),
        SimpleNamespace(id="user-1", org_id="org-1"),
        RuntimeModelsUpdate(default="gpt-5.6-sol", thinking="xhigh"),
    )

    assert result is expected
    assert org.memory_model_config["default_model"] == "openai/gpt-5.6-sol"
    assert org.memory_model_config["default_thinking"] == "xhigh"


@pytest.mark.asyncio
async def test_runtime_models_update_accepts_anthropic_catalog_model(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.models as runtime_models
    from brain.systems.runtime_settings.schemas import RuntimeModelsUpdate

    org = SimpleNamespace(memory_model_config={})

    class FakeSession:
        async def get(self, _model, _identifier):
            return org

        async def flush(self):
            return None

    monkeypatch.setattr(
        runtime_models,
        "async_get_runtime_models",
        AsyncMock(return_value=SimpleNamespace()),
    )

    await runtime_models.async_update_runtime_models(
        FakeSession(),
        SimpleNamespace(id="user-1", org_id="org-1"),
        RuntimeModelsUpdate(default="anthropic/claude-sonnet-5", thinking="high"),
    )

    assert org.memory_model_config["default_provider"] == "anthropic"
    assert org.memory_model_config["default_model"] == "anthropic/claude-sonnet-5"


@pytest.mark.asyncio
async def test_connect_openai_embedding_key_updates_memory_and_org_runtime(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.auth as auth_settings
    import brain.systems.runtime_settings.memory as memory_settings

    memory_read = SimpleNamespace(embedder="openai", embedding_status="ready")
    configure = AsyncMock()
    store_org_key = AsyncMock()

    monkeypatch.setattr(auth_settings, "parse_provider_connect_token", lambda token, provider: (token, "api_key"))
    monkeypatch.setattr(auth_settings, "verify_provider_api_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_settings, "async_set_user_codex_connection", AsyncMock(side_effect=AssertionError("runtime key should not be stored")))
    monkeypatch.setattr(auth_settings, "_async_store_org_openai_api_key", store_org_key)
    monkeypatch.setattr(memory_settings, "async_configure_openai_embedding_api_key", configure)
    monkeypatch.setattr(memory_settings, "async_get_runtime_memory", AsyncMock(return_value=memory_read))

    result = await auth_settings.async_connect_openai_embedding_api_key(
        MagicMock(),
        SimpleNamespace(id="user-1", org_id="org-1", role="owner"),
        "sk-test",
    )

    assert result is memory_read
    configure.assert_awaited_once()
    assert configure.await_args.args[1] == "sk-test"
    store_org_key.assert_awaited_once()
    store_args, store_kwargs = store_org_key.call_args
    assert store_args[1].id == "user-1"
    assert store_args[1].org_id == "org-1"
    assert store_args[1].role == "owner"
    assert store_args[2] == "sk-test"
    assert store_kwargs == {"required": False}


@pytest.mark.asyncio
async def test_owner_openai_api_key_is_stored_as_org_runtime_key(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.runtime_settings.auth as auth_settings

    set_org_key = AsyncMock(return_value=5)
    monkeypatch.setattr(auth_settings, "async_set_org_api_key", set_org_key)

    stored = await auth_settings._async_store_org_openai_api_key(
        MagicMock(),
        SimpleNamespace(id="user-1", org_id="org-1", role="owner"),
        "sk-test",
    )

    assert stored is True
    set_org_key.assert_awaited_once()
    assert set_org_key.await_args.args[:2] == ("org-1", "sk-test")
    assert set_org_key.await_args.kwargs["provider"] == "openai"
    assert set_org_key.await_args.kwargs["label"] == "Workspace OpenAI key"


@pytest.mark.asyncio
async def test_runtime_settings_snapshot_includes_routing_marketplace(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.services.runtime_introspection as runtime_settings_service

    monkeypatch.setattr(
        runtime_settings_service,
        "get_routing_marketplace_snapshot",
        AsyncMock(return_value={"flags": {"shadow": True}, "latest_decisions": []}),
    )
    monkeypatch.setattr(runtime_settings_service, "async_get_provider_auth_status", AsyncMock(return_value={"status": "in_use"}), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_get_provider_model_catalogs", AsyncMock(return_value={"openai": {"default": "gpt-5.5", "options": ["gpt-5.5"]}}), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_get_default_model", AsyncMock(return_value="gpt-5.5"), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_get_agent_worker_backend_settings", AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {})), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_resolve_default_provider", AsyncMock(return_value="openai"), raising=False)

    snapshot = await runtime_settings_service.async_get_runtime_settings_snapshot(MagicMock(), user_id="user-1", org_id="org-1")

    assert snapshot["routing_marketplace"]["flags"]["shadow"] is True


@pytest.mark.asyncio
async def test_runtime_settings_snapshot_exposes_learning_policy(monkeypatch):
    from types import SimpleNamespace

    import brain.systems.services.runtime_introspection as runtime_settings_service

    monkeypatch.setenv("LEARNING_POLICY_DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.setenv("LEARNING_POLICY_ALLOWED_MODEL_CLASSES", "local,economy")
    monkeypatch.setenv("LEARNING_POLICY_EXTERNAL_EVAL_EXPORT_ALLOWED", "false")
    monkeypatch.setattr(runtime_settings_service, "get_routing_marketplace_snapshot", AsyncMock(return_value={}), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_get_provider_auth_status", AsyncMock(return_value={"status": "in_use"}), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_get_provider_model_catalogs", AsyncMock(return_value={"openai": {"default": "gpt-5.5", "options": ["gpt-5.5"]}}), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_get_default_model", AsyncMock(return_value="gpt-5.5"), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_get_agent_worker_backend_settings", AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {})), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_resolve_default_provider", AsyncMock(return_value="openai"), raising=False)

    snapshot = await runtime_settings_service.async_get_runtime_settings_snapshot(MagicMock(), user_id="user-1", org_id="org-1")

    assert snapshot["learning_policy"]["deployment_mode"] == "self_hosted"
    assert snapshot["learning_policy"]["allowed_model_classes"] == ["local", "economy"]
    assert snapshot["learning_policy"]["external_eval_export_allowed"] is False


@pytest.mark.asyncio
async def test_runtime_settings_snapshot_exposes_provider_health(monkeypatch):
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

    monkeypatch.setattr(runtime_settings_service, "get_routing_marketplace_snapshot", AsyncMock(return_value={}), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_get_provider_auth_status", AsyncMock(return_value={"status": "in_use"}), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_get_provider_model_catalogs", AsyncMock(return_value={"openai": {"default": "gpt-5.5", "options": ["gpt-5.5"]}}), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_get_default_model", AsyncMock(return_value="gpt-5.5"), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_get_agent_worker_backend_settings", AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {})), raising=False)
    monkeypatch.setattr(runtime_settings_service, "async_resolve_default_provider", AsyncMock(return_value="openai"), raising=False)

    snapshot = await runtime_settings_service.async_get_runtime_settings_snapshot(MagicMock(), user_id="user-1", org_id="org-1")

    entries = snapshot["provider_health"]["operations"]["scout"]
    assert entries[0]["provider"] == "openai"
    assert entries[0]["status"] == "unavailable"
    assert snapshot["provider_health"]["policies"]["scout"]["fail_open"] is True


@pytest.mark.asyncio
async def test_get_llm_info_uses_default_background_models(monkeypatch):
    from types import SimpleNamespace

    from brain.app.api.routers.system import _get_llm_info

    org = SimpleNamespace(memory_model_config={
        "default_provider": "openai",
        "session_harvest": "openai:gpt-5.4-mini",
        "depth_0": "openai/gpt-5.4",
    })
    user_obj = SimpleNamespace(default_provider="openai")

    class FakeSession:
        async def get(self, model, identifier):
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

    async def _model_catalogs(*args, **kwargs):
        return {"openai": {"default": "gpt-5.5", "options": ["gpt-5.5"]}}

    async def _default_provider(*args, **kwargs):
        return "openai"

    async def _default_model(*args, **kwargs):
        return "gpt-5.5"

    async def _backend_settings(*args, **kwargs):
        return SimpleNamespace(to_dict=lambda: {})

    monkeypatch.setattr("brain.app.api.routers.system.async_get_provider_model_catalogs", _model_catalogs)
    monkeypatch.setattr("brain.app.api.routers.system.async_resolve_default_provider", _default_provider)
    monkeypatch.setattr("brain.app.api.routers.system.async_get_default_model", _default_model)
    monkeypatch.setattr("brain.app.api.routers.system.async_get_agent_worker_backend_settings", _backend_settings)

    info = await _get_llm_info({"id": "user-1", "org_id": "org-1"}, db=FakeSession())

    assert info is not None
    assert info["harvest_model"] == "gpt-5.5"
    assert info["consolidation_model"] == "gpt-5.5"
    assert "cortex_default_concurrency" not in info


@pytest.mark.asyncio
async def test_get_llm_info_exposes_provider_health(monkeypatch):
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
        async def get(self, model, identifier):
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

    async def _model_catalogs(*args, **kwargs):
        return {"openai": {"default": "gpt-5.5", "options": ["gpt-5.5"]}}

    async def _default_provider(*args, **kwargs):
        return "openai"

    async def _default_model(*args, **kwargs):
        return "gpt-5.5"

    async def _backend_settings(*args, **kwargs):
        return SimpleNamespace(to_dict=lambda: {})

    monkeypatch.setattr("brain.app.api.routers.system.async_get_provider_model_catalogs", _model_catalogs)
    monkeypatch.setattr("brain.app.api.routers.system.async_resolve_default_provider", _default_provider)
    monkeypatch.setattr("brain.app.api.routers.system.async_get_default_model", _default_model)
    monkeypatch.setattr("brain.app.api.routers.system.async_get_agent_worker_backend_settings", _backend_settings)

    info = await _get_llm_info({"id": "user-1", "org_id": "org-1"}, db=FakeSession())

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
        lambda *args, **kwargs: EmbeddingRuntimeConfig(
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


@pytest.mark.asyncio
async def test_connect_gemini_api_key_requires_installation_admin(monkeypatch):
    from types import SimpleNamespace

    from brain.systems.runtime_settings.auth import async_connect_gemini_api_key

    with pytest.raises(Exception) as exc:
        await async_connect_gemini_api_key(
            MagicMock(),
            SimpleNamespace(id="user-1", org_id="org-1", role="member"),
            "gemini-key",
        )

    assert getattr(exc.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_start_runtime_update_launches_detached_safe_deploy(monkeypatch, tmp_path):
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
    monkeypatch.setattr(self_update, "_async_active_agent_run_count", AsyncMock(return_value=2))
    monkeypatch.setattr(self_update, "_pid_running", lambda _pid: False)
    monkeypatch.setattr(self_update.subprocess, "Popen", fake_popen)

    status = await self_update.async_start_runtime_update(
        MagicMock(),
        requested_by="owner-1",
        build_no_cache=True,
        worker_drain_timeout_seconds=90,
    )

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
    assert kwargs["env"]["ILLO_COMPOSE_BUILD_NO_CACHE"] == "1"
    assert kwargs["env"]["ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_SECONDS"] == "90"
    assert (state_dir / "illo-self-update.pid").read_text(encoding="utf-8").strip() == "4242"


@pytest.mark.asyncio
async def test_start_runtime_update_reuses_running_update(monkeypatch, tmp_path):
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
    monkeypatch.setattr(self_update, "_async_active_agent_run_count", AsyncMock(return_value=1))
    monkeypatch.setattr(self_update, "_pid_running", lambda pid: pid == 5150)
    monkeypatch.setattr(
        self_update.subprocess,
        "Popen",
        MagicMock(side_effect=AssertionError("should not launch a duplicate update")),
    )

    status = await self_update.async_start_runtime_update(
        MagicMock(),
        requested_by="owner-1",
        build_no_cache=True,
        worker_drain_timeout_seconds=45,
    )

    assert status.status == "running"
    assert status.pid == 5150
    assert status.detail == "Illospace update is already running."


@pytest.mark.asyncio
async def test_start_runtime_update_rejects_non_checkout_without_override(monkeypatch, tmp_path):
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
        await self_update.async_start_runtime_update(MagicMock(), requested_by="owner-1")

    assert getattr(exc.value, "status_code", None) == 409
    assert "not running from a git checkout" in exc.value.detail


@pytest.mark.asyncio
async def test_start_runtime_update_queues_compose_sidecar_request(monkeypatch, tmp_path):
    import json

    import brain.systems.runtime_settings.self_update as self_update

    request_file = tmp_path / "self-update" / "request.json"
    status_file = tmp_path / "self-update" / "status.json"
    log_path = tmp_path / "logs" / "illo-self-update.log"

    monkeypatch.setenv("ILLO_SELF_UPDATE_REQUEST_FILE", str(request_file))
    monkeypatch.setenv("ILLO_SELF_UPDATE_STATUS_FILE", str(status_file))
    monkeypatch.setenv("ILLO_SELF_UPDATE_LOG_PATH", str(log_path))
    monkeypatch.setattr(self_update, "_async_active_agent_run_count", AsyncMock(return_value=3))

    status = await self_update.async_start_runtime_update(
        MagicMock(),
        requested_by="owner-1",
        build_no_cache=True,
        worker_drain_timeout_seconds=45,
    )

    assert status.status == "running"
    assert status.available is True
    assert status.pid is None
    assert status.active_agent_runs == 3
    assert status.log_path == str(log_path)
    assert "queued" in (status.detail or "")
    request_payload = json.loads(request_file.read_text(encoding="utf-8"))
    status_payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert request_payload["requested_by"] == "owner-1"
    assert request_payload["build_no_cache"] is True
    assert request_payload["worker_drain_timeout_seconds"] == 45
    assert status_payload["status"] == "queued"


@pytest.mark.asyncio
async def test_runtime_update_status_reports_compose_sidecar_available(monkeypatch, tmp_path):
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
    monkeypatch.setattr(self_update, "_async_active_agent_run_count", AsyncMock(return_value=0))

    status = await self_update.async_get_runtime_update_status(MagicMock())

    assert status.status == "idle"
    assert status.available is True
    assert status.detail == "Compose updater sidecar is ready."


@pytest.mark.asyncio
async def test_runtime_update_status_waits_for_compose_sidecar_heartbeat(monkeypatch, tmp_path):
    import brain.systems.runtime_settings.self_update as self_update

    request_file = tmp_path / "self-update" / "request.json"
    heartbeat_file = tmp_path / "self-update" / "heartbeat.json"

    monkeypatch.setenv("ILLO_SELF_UPDATE_REQUEST_FILE", str(request_file))
    monkeypatch.setenv("ILLO_SELF_UPDATE_HEARTBEAT_FILE", str(heartbeat_file))
    monkeypatch.setattr(self_update, "_async_active_agent_run_count", AsyncMock(return_value=0))

    status = await self_update.async_get_runtime_update_status(MagicMock())

    assert status.status == "idle"
    assert status.available is False
    assert "waiting for the Compose updater sidecar" in (status.detail or "")
