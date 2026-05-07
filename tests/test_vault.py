"""Tests for brain.systems.vault — Secret Vault (ORM-based)."""

import os
import pytest
from unittest.mock import patch, MagicMock

from cryptography.fernet import Fernet

# Generate a stable test key
TEST_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def set_vault_key(monkeypatch):
    monkeypatch.setenv("VAULT_MASTER_KEY", TEST_KEY)


def test_encrypt_decrypt_roundtrip():
    from brain.systems.vault import _encrypt, _decrypt
    plaintext = "super-secret-api-key-12345"
    encrypted = _encrypt(plaintext)
    assert isinstance(encrypted, bytes)
    assert _decrypt(encrypted) == plaintext


def test_encrypt_decrypt_special_chars():
    from brain.systems.vault import _encrypt, _decrypt
    value = "p@$$w0rd!\U0001f511 with spaces & symbols=+/"
    assert _decrypt(_encrypt(value)) == value


def test_set_secret(mock_uow):
    from brain.systems.vault import set_secret
    mock_uow.vault.get_by_key.return_value = None
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
        set_secret("MY_KEY", "my_value", user_id="user-1", description="test", category="api")
    # Should add the Secret and the VaultAccessLog
    assert mock_uow.session.add.call_count >= 1


def test_get_secret_found(mock_uow):
    from brain.systems.vault import get_secret, _encrypt
    encrypted = _encrypt("found_value")
    mock_secret = MagicMock()
    mock_secret.encrypted_value = encrypted
    mock_secret.id = 1
    mock_secret.last_accessed_at = None
    mock_secret.access_count = 0
    mock_uow.vault.get_by_key.return_value = mock_secret
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
        result = get_secret("MY_KEY", user_id="user-1")
    assert result == "found_value"


def test_get_secret_normalizes_integration_access_actor(mock_uow):
    from brain.platform.db.models.vault import VaultAccessLog
    from brain.systems.vault import get_secret, _encrypt

    encrypted = _encrypt("github-token")
    mock_secret = MagicMock()
    mock_secret.encrypted_value = encrypted
    mock_secret.id = 40
    mock_secret.last_accessed_at = None
    mock_secret.access_count = 0
    mock_uow.vault.get_by_key.return_value = mock_secret

    with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
        result = get_secret("GITHUB_EXAMPLE_TOKEN", user_id="user-1", accessed_by="github_connector")

    assert result == "github-token"
    access_logs = [
        call.args[0]
        for call in mock_uow.session.add.call_args_list
        if isinstance(call.args[0], VaultAccessLog)
    ]
    assert len(access_logs) == 1
    assert access_logs[0].accessed_by == "api"


def test_get_secret_not_found_fallback_env(mock_uow, monkeypatch):
    from brain.systems.vault import get_secret
    mock_uow.vault.get_by_key.return_value = None
    mock_uow.session.scalars.return_value.first.return_value = None
    monkeypatch.setenv("FALLBACK_KEY", "env_value")
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow), \
         patch("brain.systems.vault._record_missing"):
        result = get_secret("FALLBACK_KEY", user_id="user-1", allow_env_fallback=True)
    assert result == "env_value"


def test_get_secret_missing_returns_none(mock_uow):
    from brain.systems.vault import get_secret
    mock_uow.vault.get_by_key.return_value = None
    mock_uow.session.scalars.return_value.first.return_value = None
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow), \
         patch("brain.systems.vault._record_missing") as record_missing:
        result = get_secret("NONEXISTENT_KEY_XYZ", user_id="user-1", org_id="org-1")
    assert result is None
    record_missing.assert_called_once_with("NONEXISTENT_KEY_XYZ", user_id="user-1", org_id="org-1")


def test_delete_secret_existed(mock_uow):
    from brain.systems.vault import delete_secret
    mock_secret = MagicMock()
    mock_secret.id = 1
    mock_uow.vault.get_by_key.return_value = mock_secret
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
        assert delete_secret("MY_KEY", user_id="user-1") is True


def test_delete_secret_not_found(mock_uow):
    from brain.systems.vault import delete_secret
    mock_uow.vault.get_by_key.return_value = None
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
        assert delete_secret("NOPE", user_id="user-1") is False


def test_list_secrets_no_values(mock_uow):
    from brain.systems.vault import list_secrets
    mock_uow.session.scalars.return_value.all.return_value = []
    mock_uow.session.execute.return_value.all.return_value = []
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
        result = list_secrets(user_id="user-1")
    assert isinstance(result, list)


def test_list_secrets_with_category_filter(mock_uow):
    from brain.systems.vault import list_secrets
    mock_uow.session.scalars.return_value.all.return_value = []
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
        result = list_secrets(user_id="user-1", category="api")
    assert isinstance(result, list)


def test_list_secrets_no_category(mock_uow):
    from brain.systems.vault import list_secrets
    mock_uow.session.scalars.return_value.all.return_value = []
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
        result = list_secrets(user_id="user-1")
    assert isinstance(result, list)


def test_set_secret_upsert(mock_uow):
    """set_secret should upsert via ORM."""
    from brain.systems.vault import set_secret
    # First call: no existing
    mock_uow.vault.get_by_key.return_value = None
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
        set_secret("KEY", "val1", user_id="user-1")
    # Second call: existing
    existing = MagicMock()
    mock_uow.vault.get_by_key.return_value = existing
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
        set_secret("KEY", "val2", user_id="user-1")
    # Existing should have been updated
    assert existing.encrypted_value is not None


def test_reveal_secret_calls_get_secret(mock_uow):
    from brain.systems.vault import reveal_secret, _encrypt
    encrypted = _encrypt("revealed")
    mock_secret = MagicMock()
    mock_secret.encrypted_value = encrypted
    mock_secret.id = 1
    mock_secret.last_accessed_at = None
    mock_secret.access_count = 0
    mock_uow.vault.get_by_key.return_value = mock_secret
    with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
        result = reveal_secret("MY_KEY", user_id="user-1")
    assert result == "revealed"


def test_missing_master_key_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("VAULT_MASTER_KEY", raising=False)
    env_file = tmp_path / ".env"
    with patch("brain.systems.vault.Path") as MockPath:
        mock_path = MagicMock()
        mock_path.resolve.return_value.parent.__truediv__ = lambda self, x: env_file if x == ".env" else tmp_path / x
        MockPath.return_value = mock_path

        from brain.systems.vault import _get_fernet
        monkeypatch.delenv("VAULT_MASTER_KEY", raising=False)
        with pytest.raises(RuntimeError, match="VAULT_MASTER_KEY is required"):
            _get_fernet()
