"""Tests for org-owned provider keys and user Codex subscription storage."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_uow_mock(session_mock=None):
    sess = session_mock or MagicMock()
    scalar_result = MagicMock()
    sess.scalars = AsyncMock(return_value=scalar_result)
    sess.get = AsyncMock()
    sess.flush = AsyncMock()
    sess.add = MagicMock()
    uow_instance = MagicMock()
    uow_instance.session = sess
    uow_instance.__aenter__ = AsyncMock(return_value=uow_instance)
    uow_instance.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=uow_instance), sess, uow_instance


class TestResolveApiKey:
    @patch("brain.systems.vault._decrypt", return_value="sk-org-main")
    @patch("brain.systems.vault.UnitOfWork")
    async def test_resolves_org_key_from_user_org(self, mock_uow_cls, mock_decrypt):
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        sess.get.return_value = SimpleNamespace(org_id="org-1")
        org_key = SimpleNamespace(encrypted_key=b"org-enc")
        sess.scalars.return_value.first.return_value = org_key

        from brain.systems.vault import resolve_api_key

        key, source = await resolve_api_key(user_id="user-1", provider="anthropic")

        assert key == "sk-org-main"
        assert source == "org_main"

    @patch("brain.systems.vault._decrypt", return_value="codex-json")
    @patch("brain.systems.vault.UnitOfWork")
    async def test_openai_can_resolve_user_codex_subscription(self, mock_uow_cls, mock_decrypt):
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        codex_connection = SimpleNamespace(encrypted_credential=b"codex-enc")
        sess.scalars.return_value.first.return_value = codex_connection

        from brain.systems.vault import resolve_api_key

        key, source = await resolve_api_key(user_id="user-1", provider="openai")

        assert key == "codex-json"
        assert source == "codex_subscription"

    @patch("brain.systems.vault._decrypt", return_value="sk-org-openai")
    @patch("brain.systems.vault.UnitOfWork")
    async def test_openai_api_key_mode_skips_user_codex_subscription(self, mock_uow_cls, mock_decrypt):
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        sess.get.return_value = SimpleNamespace(org_id="org-1")
        org_key = SimpleNamespace(encrypted_key=b"org-enc")
        sess.scalars.return_value.first.return_value = org_key

        from brain.systems.vault import resolve_api_key

        key, source = await resolve_api_key(user_id="user-1", provider="openai", auth_mode="api_key")

        assert key == "sk-org-openai"
        assert source == "org_main"

    @patch("brain.systems.vault.UnitOfWork")
    async def test_falls_back_to_env(self, mock_uow_cls):
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        sess.get.return_value = SimpleNamespace(org_id="org-1")
        sess.scalars.return_value.first.return_value = None

        from brain.systems.vault import resolve_api_key

        with patch("os.environ.get", return_value="sk-env-key"):
            key, source = await resolve_api_key(user_id="user-1", provider="anthropic")

        assert key == "sk-env-key"
        assert source == "env"


class TestSetCredentials:
    @patch("brain.systems.vault._encrypt", return_value=b"enc")
    @patch("brain.systems.vault.UnitOfWork")
    async def test_set_org_api_key_creates_new(self, mock_uow_cls, mock_encrypt):
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess
        sess.scalars.return_value.first.return_value = None

        def capture_add(obj):
            obj.id = 42

        sess.add.side_effect = capture_add

        from brain.systems.vault import set_org_api_key

        key_id = await set_org_api_key("org-1", "sk-test-key", provider="anthropic", label="main")

        assert key_id == 42
        added = sess.add.call_args.args[0]
        assert added.org_id == "org-1"
        assert added.provider == "anthropic"
        assert added.encrypted_key == b"enc"

    @patch("brain.systems.vault._encrypt", return_value=b"enc-codex")
    @patch("brain.systems.vault.UnitOfWork")
    async def test_set_user_codex_connection_is_the_only_user_credential(self, mock_uow_cls, mock_encrypt):
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess
        sess.scalars.return_value.first.return_value = None

        def capture_add(obj):
            obj.id = 7

        sess.add.side_effect = capture_add

        from brain.systems.vault import set_user_codex_connection

        connection_id = await set_user_codex_connection("user-1", '{"auth_mode":"chatgpt"}')

        assert connection_id == 7
        added = sess.add.call_args.args[0]
        assert added.user_id == "user-1"
        assert added.encrypted_credential == b"enc-codex"


class TestUpdateResolvedApiKey:
    @patch("brain.systems.vault._encrypt", return_value=b"enc-refreshed")
    @patch("brain.systems.vault.UnitOfWork")
    async def test_updates_codex_subscription_refresh(self, mock_uow_cls, mock_encrypt):
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess
        connection = SimpleNamespace(encrypted_credential=b"old", is_active=True)
        sess.scalars.return_value.first.return_value = connection

        from brain.systems.vault import update_resolved_api_key

        updated = await update_resolved_api_key(
            user_id="user-1",
            provider="openai",
            source="codex_subscription",
            api_key="refreshed-json",
        )

        assert updated is True
        assert connection.encrypted_credential == b"enc-refreshed"
        sess.flush.assert_awaited_once()

    @patch("brain.systems.vault._encrypt", return_value=b"enc-org-refreshed")
    @patch("brain.systems.vault.UnitOfWork")
    async def test_updates_org_key(self, mock_uow_cls, mock_encrypt):
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess
        sess.get.return_value = SimpleNamespace(org_id="org-1")
        org_key = SimpleNamespace(encrypted_key=b"old")
        sess.scalars.return_value.first.return_value = org_key

        from brain.systems.vault import update_resolved_api_key

        updated = await update_resolved_api_key(
            user_id="user-1",
            provider="openai",
            source="org_main",
            api_key="refreshed-json",
        )

        assert updated is True
        assert org_key.encrypted_key == b"enc-org-refreshed"
        sess.flush.assert_awaited_once()

    @patch("brain.systems.vault._encrypt", return_value=b"unused")
    @patch("brain.systems.vault.UnitOfWork")
    async def test_returns_false_for_non_db_sources(self, mock_uow_cls, mock_encrypt):
        from brain.systems.vault import update_resolved_api_key

        updated = await update_resolved_api_key(
            user_id="user-1",
            provider="openai",
            source="env",
            api_key="refreshed-json",
        )

        assert updated is False
        mock_encrypt.assert_not_called()
        mock_uow_cls.assert_not_called()
