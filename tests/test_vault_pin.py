"""Tests for vault PIN protection (ORM-based)."""

import os
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from cryptography.fernet import Fernet

TEST_KEY = Fernet.generate_key().decode()
USER_ID = "user-1"


@pytest.fixture(autouse=True)
def set_vault_key(monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", TEST_KEY)


@pytest.fixture
def config_store():
    """In-memory vault_config store backed by mock UnitOfWork."""
    return {}


@pytest.fixture
def session_store():
    """In-memory vault_sessions store backed by mock UnitOfWork."""
    return {}


@pytest.fixture
def mock_vault_uow(config_store, session_store):
    """Mock UnitOfWork that simulates VaultConfig and VaultSession CRUD in memory."""
    from brain.platform.db.models.vault import VaultConfig, VaultSession

    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)

    def scalars_side_effect(stmt):
        """Simulate SELECT on VaultConfig by key."""
        result = MagicMock()
        # Try to extract the key from the statement's where clause
        # We intercept based on the key stored in config_store
        # The vault code does: select(VaultConfig).where(VaultConfig.key == key)
        # We look at the compiled where clause to extract the key
        try:
            key = stmt._where_criteria[0].right.value
        except (AttributeError, IndexError):
            key = None

        if key and key in config_store:
            mock_config = MagicMock()
            mock_config.key = key
            mock_config.value = config_store[key]
            # Allow setting .value to update the store
            def set_value(val):
                config_store[key] = val
            type(mock_config).value = property(
                lambda self: config_store.get(key),
                lambda self, val: config_store.__setitem__(key, val),
            )
            type(mock_config).updated_at = property(
                lambda self: None,
                lambda self, val: None,
            )
            result.first.return_value = mock_config
        else:
            result.first.return_value = None
        return result

    def add_side_effect(obj):
        """Simulate INSERT for vault config/session models."""
        if isinstance(obj, VaultSession) or hasattr(obj, 'token_hash'):
            session_store[obj.token_hash] = obj
        elif hasattr(obj, 'key') and hasattr(obj, 'value'):
            config_store[obj.key] = obj.value

    def delete_side_effect(obj):
        """Simulate DELETE for VaultConfig."""
        if hasattr(obj, 'key'):
            config_store.pop(obj.key, None)

    def get_side_effect(model, primary_key):
        if model is VaultSession:
            return session_store.get(primary_key)
        return None

    mock_uow.session.scalars.side_effect = scalars_side_effect
    mock_uow.session.add.side_effect = add_side_effect
    mock_uow.session.delete.side_effect = delete_side_effect
    mock_uow.session.get.side_effect = get_side_effect

    # Also mock vault repo for get_secret/require_secret
    mock_uow.vault = MagicMock()
    mock_uow.vault.get_by_key.return_value = None
    mock_uow.session.execute = MagicMock()

    return mock_uow


@pytest.fixture
def patch_uow(mock_vault_uow):
    """Patch UnitOfWork globally for vault module."""
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_vault_uow):
        yield mock_vault_uow


def test_set_pin_stores_hash(patch_uow, config_store):
    from brain.systems.vault import set_pin, has_pin
    result = set_pin(USER_ID, "1234")
    assert result is True
    assert f"pin:{USER_ID}:hash" in config_store
    assert len(config_store[f"pin:{USER_ID}:hash"]) > 0


def test_has_pin_false_initially(patch_uow):
    from brain.systems.vault import has_pin
    assert has_pin(USER_ID) is False


def test_has_pin_true_after_set(patch_uow, config_store):
    from brain.systems.vault import set_pin, has_pin
    set_pin(USER_ID, "1234")
    assert has_pin(USER_ID) is True


def test_verify_pin_correct(patch_uow, config_store):
    from brain.systems.vault import set_pin, verify_pin
    set_pin(USER_ID, "1234")
    assert verify_pin(USER_ID, "1234") is True


def test_verify_pin_wrong(patch_uow, config_store):
    from brain.systems.vault import set_pin, verify_pin
    set_pin(USER_ID, "1234")
    assert verify_pin(USER_ID, "wrong") is False
    assert config_store[f"pin:{USER_ID}:failures"] == '1'


def test_verify_pin_lockout_after_3_failures(patch_uow, config_store):
    from brain.systems.vault import set_pin, verify_pin
    set_pin(USER_ID, "1234")
    verify_pin(USER_ID, "wrong1")
    verify_pin(USER_ID, "wrong2")
    verify_pin(USER_ID, "wrong3")
    assert f"pin:{USER_ID}:lockout" in config_store
    assert config_store[f"pin:{USER_ID}:lockout"] != ''
    # Should still fail even with correct pin during lockout
    assert verify_pin(USER_ID, "1234") is False


def test_verify_pin_lockout_expires(patch_uow, config_store):
    from brain.systems.vault import set_pin, verify_pin
    set_pin(USER_ID, "1234")
    verify_pin(USER_ID, "wrong1")
    verify_pin(USER_ID, "wrong2")
    verify_pin(USER_ID, "wrong3")
    # Set lockout to past
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    config_store[f"pin:{USER_ID}:lockout"] = past
    assert verify_pin(USER_ID, "1234") is True


def test_verify_pin_no_pin_set(patch_uow):
    from brain.systems.vault import verify_pin
    assert verify_pin(USER_ID, "anything") is True


def test_generate_vault_token(patch_uow, session_store):
    from brain.systems.vault import generate_vault_token
    token, expires = generate_vault_token(USER_ID)
    assert token is not None
    assert len(token) > 10
    assert expires > datetime.now(timezone.utc)
    assert len(session_store) == 1
    stored = next(iter(session_store.values()))
    assert stored.user_id == USER_ID
    assert stored.expires_at == expires


def test_validate_vault_token_valid(patch_uow, session_store):
    from brain.systems.vault import generate_vault_token, validate_vault_token
    token, _ = generate_vault_token(USER_ID)
    assert validate_vault_token(USER_ID, token) is True
    stored = next(iter(session_store.values()))
    assert stored.last_seen_at is not None


def test_validate_vault_token_expired(patch_uow, session_store):
    from brain.systems.vault import generate_vault_token, validate_vault_token
    token, _ = generate_vault_token(USER_ID)
    # Set expiry to past
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    stored = next(iter(session_store.values()))
    stored.expires_at = datetime.fromisoformat(past)
    assert validate_vault_token(USER_ID, token) is False
    assert stored.revoked_at is not None


def test_validate_vault_token_empty(patch_uow):
    from brain.systems.vault import validate_vault_token
    assert validate_vault_token(USER_ID, "") is False
    assert validate_vault_token(USER_ID, None) is False


def test_validate_vault_token_unknown(patch_uow):
    from brain.systems.vault import validate_vault_token
    assert validate_vault_token(USER_ID, "nonexistent-token") is False


def test_validate_vault_token_rejects_other_user(patch_uow):
    from brain.systems.vault import generate_vault_token, validate_vault_token
    token, _ = generate_vault_token(USER_ID)
    assert validate_vault_token("user-2", token) is False


def test_revoke_vault_token(patch_uow, session_store):
    from brain.systems.vault import generate_vault_token, revoke_vault_token, validate_vault_token
    token, _ = generate_vault_token(USER_ID)
    revoke_vault_token(USER_ID, token)
    stored = next(iter(session_store.values()))
    assert stored.revoked_at is not None
    assert validate_vault_token(USER_ID, token) is False


def test_require_secret_raises_when_missing(patch_uow):
    from brain.systems.vault import require_secret
    with pytest.raises(ValueError, match="not found"):
        require_secret("NONEXISTENT_KEY", user_id=USER_ID)


def test_require_secret_returns_value(patch_uow, monkeypatch):
    monkeypatch.setenv("SOME_SECRET", "hello")
    from brain.systems.vault import require_secret
    assert require_secret("SOME_SECRET", user_id=USER_ID, allow_env_fallback=True) == "hello"


def test_set_pin_change_requires_current(patch_uow, config_store):
    from brain.systems.vault import set_pin
    set_pin(USER_ID, "1234")
    # Try changing without correct current PIN
    assert set_pin(USER_ID, "5678", "wrong") is False
    # Change with correct current PIN
    assert set_pin(USER_ID, "5678", "1234") is True
