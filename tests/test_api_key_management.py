"""Tests for per-user API key management with sharing and fallback chain."""
import pytest
from unittest.mock import patch, MagicMock, PropertyMock


def _make_uow_mock(session_mock=None):
    """Create a mock UnitOfWork that works as a context manager.

    Returns (uow_class_mock, session_mock) so tests can configure
    session.scalars(...).first() return values.
    """
    sess = session_mock or MagicMock()
    uow_instance = MagicMock()
    uow_instance.session = sess
    uow_instance.__enter__ = MagicMock(return_value=uow_instance)
    uow_instance.__exit__ = MagicMock(return_value=False)

    uow_class = MagicMock(return_value=uow_instance)
    return uow_class, sess, uow_instance


class TestResolveApiKey:
    """API key resolution follows fallback chain:
    user default -> org main key -> env
    """

    @patch("brain.systems.vault._decrypt", return_value="sk-user-key")
    @patch("brain.systems.vault.UnitOfWork")
    def test_returns_user_default_key_first(self, mock_uow_cls, mock_decrypt):
        """User's chosen default key takes priority."""
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.side_effect = uow_cls.side_effect
        mock_uow_cls.return_value = uow

        # session.scalars(stmt).first() returns a UserApiKey-like object
        key_row = MagicMock()
        key_row.encrypted_key = b"encrypted"
        sess.scalars.return_value.first.return_value = key_row
        uow.session = sess

        from brain.systems.vault import resolve_api_key
        key, source = resolve_api_key(user_id="user-1", provider="anthropic")
        assert key == "sk-user-key"
        assert source == "user_default"

    @patch("brain.systems.vault._decrypt", return_value="sk-org-main")
    @patch("brain.systems.vault.UnitOfWork")
    def test_falls_back_to_org_main_key(self, mock_uow_cls, mock_decrypt):
        """If user has no default, use org main key."""
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        user_obj = MagicMock()
        user_obj.org_id = "org-1"

        org_key = MagicMock()
        org_key.encrypted_key = b"org-enc"

        # First scalars call: user default key -> None
        # Second scalars call: provider-specific default key -> None
        # session.get(User, user_id) -> user_obj (for org lookup)
        # Third scalars call: org key -> org_key
        sess.scalars.return_value.first.side_effect = [None, None, org_key]
        sess.get.return_value = user_obj

        from brain.systems.vault import resolve_api_key
        key, source = resolve_api_key(user_id="user-2", provider="anthropic")
        assert key == "sk-org-main"
        assert source == "org_main"

    @patch("brain.systems.vault._decrypt", return_value="sk-openai-user-default")
    @patch("brain.systems.vault.UnitOfWork")
    def test_falls_back_to_provider_specific_default_key(self, mock_uow_cls, mock_decrypt):
        """A provider-scoped default label should resolve even if the global default slot points elsewhere."""
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        key_row = MagicMock()
        key_row.encrypted_key = b"openai-enc"

        # First scalars call: global default for provider -> None
        # Second scalars call: provider-specific label=default -> key
        sess.scalars.return_value.first.side_effect = [None, key_row]

        from brain.systems.vault import resolve_api_key
        key, source = resolve_api_key(user_id="user-3", provider="openai")
        assert key == "sk-openai-user-default"
        assert source == "user_default"

    @patch("brain.systems.vault._decrypt", return_value="sk-anthropic-legacy")
    @patch("brain.systems.vault.UnitOfWork")
    def test_falls_back_to_latest_active_provider_key_when_no_default_label(self, mock_uow_cls, mock_decrypt):
        """Legacy labeled keys like 'Claude Code' should still resolve per provider."""
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        key_row = MagicMock()
        key_row.encrypted_key = b"anthropic-enc"
        key_row.label = "Claude Code"

        # First scalars call: global default for provider -> None
        # Second scalars call: provider-specific active fallback -> key
        sess.scalars.return_value.first.side_effect = [None, key_row]

        from brain.systems.vault import resolve_api_key
        key, source = resolve_api_key(user_id="user-legacy", provider="anthropic")
        assert key == "sk-anthropic-legacy"
        assert source == "user_default"

    @patch("brain.systems.vault.UnitOfWork")
    def test_falls_back_to_env(self, mock_uow_cls):
        """If no user default or org main key, fall back to environment."""
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        user_obj = MagicMock()
        user_obj.org_id = "org-1"

        # Three scalars calls return None (no global default, no provider default, no org key)
        sess.scalars.return_value.first.side_effect = [None, None, None]
        sess.get.return_value = user_obj

        from brain.systems.vault import resolve_api_key
        with patch("os.environ.get", return_value="sk-env-key"):
            key, source = resolve_api_key(user_id="user-4", provider="anthropic")
        assert key == "sk-env-key"
        assert source == "env"

    @patch("brain.systems.vault._decrypt", return_value="sk-org-main")
    @patch("brain.systems.vault.UnitOfWork")
    def test_system_operations_use_org_main_key(self, mock_uow_cls, mock_decrypt):
        """Without user_id (system ops like nightly), resolves org main key directly."""
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        org_key = MagicMock()
        org_key.encrypted_key = b"org-enc"
        sess.scalars.return_value.first.return_value = org_key

        from brain.systems.vault import resolve_api_key
        key, source = resolve_api_key(org_id="org-1", provider="anthropic")
        assert key == "sk-org-main"
        assert source == "org_main"


class TestSetApiKey:
    """Setting a per-user API key."""

    @patch("brain.systems.vault._encrypt", return_value=b"enc")
    @patch("brain.systems.vault.UnitOfWork")
    def test_set_api_key_creates_new(self, mock_uow_cls, mock_encrypt):
        """When no existing key, creates a new UserApiKey."""
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        # No existing key found
        sess.scalars.return_value.first.return_value = None

        # Capture what gets added to the session
        added_objects = []
        def capture_add(obj):
            obj.id = 42  # Simulate flush setting an id
            added_objects.append(obj)
        sess.add.side_effect = capture_add

        from brain.systems.vault import set_api_key
        key_id = set_api_key("user-1", "sk-test-key", provider="anthropic", label="default")

        assert key_id == 42
        assert len(added_objects) == 1
        assert added_objects[0].user_id == "user-1"
        assert added_objects[0].provider == "anthropic"
        assert added_objects[0].encrypted_key == b"enc"

    @patch("brain.systems.vault._encrypt", return_value=b"enc-updated")
    @patch("brain.systems.vault.UnitOfWork")
    def test_set_api_key_updates_existing(self, mock_uow_cls, mock_encrypt):
        """When existing key found, updates it in place."""
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        existing = MagicMock()
        existing.id = 99
        sess.scalars.return_value.first.return_value = existing

        from brain.systems.vault import set_api_key
        key_id = set_api_key("user-1", "sk-updated", provider="anthropic", label="default")

        assert key_id == 99
        assert existing.encrypted_key == b"enc-updated"
        assert existing.is_active is True


class TestUpdateResolvedApiKey:
    """Updating whichever API key row the resolver selected."""

    @patch("brain.systems.vault._encrypt", return_value=b"enc-refreshed")
    @patch("brain.systems.vault.UnitOfWork")
    def test_updates_provider_specific_user_default_fallback(self, mock_uow_cls, mock_encrypt):
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        existing = MagicMock()
        existing.id = 99

        # First lookup is the global default slot; second is provider-specific fallback.
        sess.scalars.return_value.first.side_effect = [None, existing]

        from brain.systems.vault import update_resolved_api_key
        updated = update_resolved_api_key(
            user_id="user-1",
            provider="openai",
            source="user_default",
            api_key="refreshed-json",
        )

        assert updated is True
        assert existing.encrypted_key == b"enc-refreshed"
        assert existing.is_active is True
        mock_encrypt.assert_called_once_with("refreshed-json")
        sess.flush.assert_called_once()

    @patch("brain.systems.vault._encrypt", return_value=b"enc-org-refreshed")
    @patch("brain.systems.vault.UnitOfWork")
    def test_updates_org_main_key_derived_from_user(self, mock_uow_cls, mock_encrypt):
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        user_obj = MagicMock()
        user_obj.org_id = "org-1"
        org_key = MagicMock()

        sess.get.return_value = user_obj
        sess.scalars.return_value.first.return_value = org_key

        from brain.systems.vault import update_resolved_api_key
        updated = update_resolved_api_key(
            user_id="user-1",
            provider="openai",
            source="org_main",
            api_key="refreshed-json",
        )

        assert updated is True
        assert org_key.encrypted_key == b"enc-org-refreshed"
        mock_encrypt.assert_called_once_with("refreshed-json")
        sess.flush.assert_called_once()

    @patch("brain.systems.vault._encrypt", return_value=b"unused")
    @patch("brain.systems.vault.UnitOfWork")
    def test_returns_false_for_non_db_sources(self, mock_uow_cls, mock_encrypt):
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        from brain.systems.vault import update_resolved_api_key
        updated = update_resolved_api_key(
            user_id="user-1",
            provider="openai",
            source="env",
            api_key="refreshed-json",
        )

        assert updated is False
        mock_encrypt.assert_not_called()
        mock_uow_cls.assert_not_called()
        sess.scalars.assert_not_called()


class TestShareApiKey:
    """Sharing an API key with another user."""

    @patch("brain.systems.vault.UnitOfWork")
    def test_share_validates_ownership(self, mock_uow_cls):
        """Sharing a key you don't own raises ValueError."""
        uow_cls, sess, uow = _make_uow_mock()
        mock_uow_cls.return_value = uow
        uow.session = sess

        # Key not owned by sharer
        sess.scalars.return_value.first.return_value = None

        from brain.systems.vault import share_api_key
        with pytest.raises(ValueError, match="not found or not owned"):
            share_api_key(api_key_id=1, shared_with_user_id="user-2",
                         shared_by_user_id="wrong-user")
