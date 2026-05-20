"""Tests for vault PIN protection (ORM-based)."""

import os
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from cryptography.fernet import Fernet

TEST_KEY = Fernet.generate_key().decode()
ORG_ID = "org-1"
USER_ID = "user-1"
OTHER_USER_ID = "user-2"
PIN_SCOPE = f"pin:org:{ORG_ID}:user:{USER_ID}"


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
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)

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

    mock_uow.session.scalars = AsyncMock(side_effect=scalars_side_effect)
    mock_uow.session.add.side_effect = add_side_effect
    mock_uow.session.delete = AsyncMock(side_effect=delete_side_effect)
    mock_uow.session.get = AsyncMock(side_effect=get_side_effect)

    # Also mock vault repo for get_secret/require_secret
    mock_uow.vault = MagicMock()
    mock_uow.vault.get_by_key = AsyncMock(return_value=None)
    mock_uow.session.execute = MagicMock()

    return mock_uow


@pytest.fixture
def patch_uow(mock_vault_uow):
    """Patch UnitOfWork globally for vault module."""
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_vault_uow):
        yield mock_vault_uow


async def test_set_pin_stores_hash(patch_uow, config_store):
    from brain.systems.vault import set_pin, has_pin
    result = await set_pin(ORG_ID, USER_ID, "1234")
    assert result is True
    assert f"{PIN_SCOPE}:hash" in config_store
    assert len(config_store[f"{PIN_SCOPE}:hash"]) > 0


async def test_has_pin_false_initially(patch_uow):
    from brain.systems.vault import has_pin
    assert await has_pin(ORG_ID, USER_ID) is False


async def test_has_pin_true_after_set(patch_uow, config_store):
    from brain.systems.vault import set_pin, has_pin
    await set_pin(ORG_ID, USER_ID, "1234")
    assert await has_pin(ORG_ID, USER_ID) is True
    assert await has_pin(ORG_ID, OTHER_USER_ID) is False


async def test_verify_pin_correct(patch_uow, config_store):
    from brain.systems.vault import set_pin, verify_pin
    await set_pin(ORG_ID, USER_ID, "1234")
    assert await verify_pin(ORG_ID, USER_ID, "1234") is True
    assert await verify_pin(ORG_ID, OTHER_USER_ID, "1234") is False


async def test_verify_pin_wrong(patch_uow, config_store):
    from brain.systems.vault import set_pin, verify_pin
    await set_pin(ORG_ID, USER_ID, "1234")
    assert await verify_pin(ORG_ID, USER_ID, "wrong") is False
    assert config_store[f"{PIN_SCOPE}:failures"] == '1'


async def test_verify_pin_lockout_after_3_failures(patch_uow, config_store):
    from brain.systems.vault import set_pin, verify_pin
    await set_pin(ORG_ID, USER_ID, "1234")
    await verify_pin(ORG_ID, USER_ID, "wrong1")
    await verify_pin(ORG_ID, USER_ID, "wrong2")
    await verify_pin(ORG_ID, USER_ID, "wrong3")
    assert f"{PIN_SCOPE}:lockout" in config_store
    assert config_store[f"{PIN_SCOPE}:lockout"] != ''
    # Should still fail even with correct pin during lockout
    assert await verify_pin(ORG_ID, USER_ID, "1234") is False


async def test_verify_pin_lockout_expires(patch_uow, config_store):
    from brain.systems.vault import set_pin, verify_pin
    await set_pin(ORG_ID, USER_ID, "1234")
    await verify_pin(ORG_ID, USER_ID, "wrong1")
    await verify_pin(ORG_ID, USER_ID, "wrong2")
    await verify_pin(ORG_ID, USER_ID, "wrong3")
    # Set lockout to past
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    config_store[f"{PIN_SCOPE}:lockout"] = past
    assert await verify_pin(ORG_ID, USER_ID, "1234") is True


async def test_verify_pin_no_pin_set(patch_uow):
    from brain.systems.vault import verify_pin
    assert await verify_pin(ORG_ID, USER_ID, "anything") is False


async def test_generate_vault_token(patch_uow, session_store):
    from brain.systems.vault import generate_vault_token
    token, expires = await generate_vault_token(ORG_ID, USER_ID)
    assert token is not None
    assert len(token) > 10
    assert expires > datetime.now(timezone.utc)
    assert len(session_store) == 1
    stored = next(iter(session_store.values()))
    assert stored.org_id == ORG_ID
    assert stored.actor_user_id == USER_ID
    assert stored.expires_at == expires
    assert stored.created_at.tzinfo is not None
    assert stored.expires_at.tzinfo is not None


async def test_validate_vault_token_valid(patch_uow, session_store):
    from brain.systems.vault import generate_vault_token, validate_vault_token
    token, _ = await generate_vault_token(ORG_ID, USER_ID)
    assert await validate_vault_token(ORG_ID, USER_ID, token) is True
    stored = next(iter(session_store.values()))
    assert stored.last_seen_at is not None
    assert stored.last_seen_at.tzinfo is not None


async def test_validate_vault_token_expired(patch_uow, session_store):
    from brain.systems.vault import generate_vault_token, validate_vault_token
    token, _ = await generate_vault_token(ORG_ID, USER_ID)
    # Set expiry to past
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    stored = next(iter(session_store.values()))
    stored.expires_at = datetime.fromisoformat(past)
    assert await validate_vault_token(ORG_ID, USER_ID, token) is False
    assert stored.revoked_at is not None
    assert stored.revoked_at.tzinfo is not None


async def test_validate_vault_token_accepts_naive_utc_session_in_non_utc_db_timezone(patch_uow, session_store):
    from brain.systems.vault import generate_vault_token, validate_vault_token

    token, expires = await generate_vault_token(ORG_ID, USER_ID)
    stored = next(iter(session_store.values()))

    # Test doubles and old rows can still surface naive UTC values. These must
    # be treated as UTC timestamps, not as local server wall time.
    stored.expires_at = expires.replace(tzinfo=None)
    assert await validate_vault_token(ORG_ID, USER_ID, token) is True


async def test_validate_vault_token_empty(patch_uow):
    from brain.systems.vault import validate_vault_token
    assert await validate_vault_token(ORG_ID, USER_ID, "") is False
    assert await validate_vault_token(ORG_ID, USER_ID, None) is False


async def test_validate_vault_token_unknown(patch_uow):
    from brain.systems.vault import validate_vault_token
    assert await validate_vault_token(ORG_ID, USER_ID, "nonexistent-token") is False


async def test_validate_vault_token_rejects_other_org(patch_uow):
    from brain.systems.vault import generate_vault_token, validate_vault_token
    token, _ = await generate_vault_token(ORG_ID, USER_ID)
    assert await validate_vault_token("org-2", USER_ID, token) is False


async def test_validate_vault_token_rejects_other_user(patch_uow):
    from brain.systems.vault import generate_vault_token, validate_vault_token
    token, _ = await generate_vault_token(ORG_ID, USER_ID)
    assert await validate_vault_token(ORG_ID, OTHER_USER_ID, token) is False


async def test_revoke_vault_token(patch_uow, session_store):
    from brain.systems.vault import generate_vault_token, revoke_vault_token, validate_vault_token
    token, _ = await generate_vault_token(ORG_ID, USER_ID)
    await revoke_vault_token(ORG_ID, USER_ID, token)
    stored = next(iter(session_store.values()))
    assert stored.revoked_at is not None
    assert stored.revoked_at.tzinfo is not None
    assert await validate_vault_token(ORG_ID, USER_ID, token) is False


async def test_require_secret_raises_when_missing(patch_uow):
    from brain.systems.vault import require_secret
    with pytest.raises(ValueError, match="not found"):
        await require_secret("NONEXISTENT_KEY", actor_user_id=USER_ID, org_id=ORG_ID)


async def test_require_secret_returns_value(patch_uow, monkeypatch):
    monkeypatch.setenv("SOME_SECRET", "hello")
    from brain.systems.vault import require_secret
    assert (
        await require_secret(
            "SOME_SECRET",
            actor_user_id=USER_ID,
            org_id=ORG_ID,
            allow_env_fallback=True,
        )
        == "hello"
    )


async def test_set_pin_change_requires_current(patch_uow, config_store):
    from brain.systems.vault import set_pin
    await set_pin(ORG_ID, USER_ID, "1234")
    # Try changing without correct current PIN
    assert await set_pin(ORG_ID, USER_ID, "5678", "wrong") is False
    # Change with correct current PIN
    assert await set_pin(ORG_ID, USER_ID, "5678", "1234") is True
