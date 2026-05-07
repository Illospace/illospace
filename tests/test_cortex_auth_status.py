from unittest.mock import MagicMock, patch


def test_auth_status_reports_runtime_db_key_state():
    from brain.app.api.routers.cortex import auth_status

    user = {"id": "user-1", "org_id": "org-1", "role": "owner"}

    mock_session = MagicMock()
    # scalars().first() called twice: once for personal key, once for org key
    # Return truthy values to indicate keys exist
    mock_session.scalars.return_value.first.side_effect = [1, 1]

    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_uow.session = mock_session

    mock_llm = MagicMock(source="org_main", auth_mode="api_key", is_oauth=False)

    with patch("brain.systems.services.runtime_introspection.resolve_llm_client", return_value=mock_llm), \
         patch("brain.systems.services.runtime_introspection.resolve_default_provider", return_value="anthropic"), \
         patch("brain.systems.services.runtime_introspection.UnitOfWork", return_value=mock_uow):
        data = auth_status(provider="anthropic", user=user)

    assert data["authenticated"] is True
    assert data["has_personal_db_key"] is True
    assert data["has_org_db_key"] is True
    assert data["runtime_uses_db_key"] is True
    assert data["runtime_key_source"] == "org_main"
    assert data["runtime_key_scope"] == "org"
    assert data["is_selected_provider"] is False
    assert data["status"] == "available"
    assert data["setup_required"] is False


def test_auth_status_requires_db_key_even_if_env_key_exists():
    from brain.app.api.routers.cortex import auth_status

    user = {"id": "user-1", "org_id": "org-1", "role": "owner"}

    mock_session = MagicMock()
    # scalars().first() called twice: both return None (no keys)
    mock_session.scalars.return_value.first.side_effect = [None, None]

    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_uow.session = mock_session

    with patch("brain.systems.services.runtime_introspection.resolve_llm_client", side_effect=RuntimeError("missing")), \
         patch("brain.systems.services.runtime_introspection.UnitOfWork", return_value=mock_uow):
        data = auth_status(user)

    assert data["authenticated"] is False
    assert data["runtime_uses_db_key"] is False
    assert data["runtime_key_source"] == "none"
    assert data["status"] == "not_configured"
    assert data["setup_required"] is True


def test_auth_status_reports_openai_codex_cache_runtime():
    from brain.app.api.routers.cortex import auth_status

    user = {"id": "user-1", "org_id": "org-1", "role": "owner"}

    mock_session = MagicMock()
    mock_session.scalars.return_value.first.side_effect = [None, None]

    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_uow.session = mock_session

    mock_llm = MagicMock(source="codex_cache", auth_mode="chatgpt", is_oauth=True)

    with patch("brain.systems.services.runtime_introspection.resolve_llm_client", return_value=mock_llm), \
         patch("brain.systems.services.runtime_introspection.resolve_default_provider", return_value="anthropic"), \
         patch("brain.systems.services.runtime_introspection.UnitOfWork", return_value=mock_uow):
        data = auth_status(provider="openai", user=user)

    assert data["provider"] == "openai"
    assert data["authenticated"] is True
    assert data["method"] == "chatgpt"
    assert data["runtime_key_source"] == "codex_cache"
    assert data["runtime_key_scope"] == "external"
    assert data["status"] == "in_use"
    assert data["runtime_uses_external_auth"] is True
