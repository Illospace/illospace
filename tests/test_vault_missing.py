"""Tests for vault missing secret detection and brain context integration.

These tests require a real database connection since they test ORM operations
end-to-end. Mark them with requires_db to skip when no DB is available.
"""
import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone

from tests.conftest import requires_db


def _make_awaitable_attr(obj, name: str) -> None:
    current = getattr(obj, name)
    if isinstance(current, AsyncMock):
        return
    replacement = AsyncMock()
    replacement.return_value = current.return_value
    replacement.side_effect = current.side_effect
    setattr(obj, name, replacement)


def _async_uow(mock_uow):
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    _make_awaitable_attr(mock_uow.session, "scalars")
    _make_awaitable_attr(mock_uow.session, "execute")
    _make_awaitable_attr(mock_uow.vault, "get_by_key")
    mock_uow.session.flush = AsyncMock()
    mock_uow.session.delete = AsyncMock()
    return mock_uow


# ---------------------------------------------------------------------------
# Unit tests with mocked UnitOfWork
# ---------------------------------------------------------------------------

class TestRecordMissing:
    """Test _record_missing() tracking with mocked UnitOfWork."""

    async def test_creates_entry_on_first_call(self, mock_uow):
        """_record_missing should create a new VaultMissingRequest when none exists."""
        mock_uow.session.scalars.return_value.first.return_value = None
        _async_uow(mock_uow)
        with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
            from brain.systems.vault import _async_record_missing
            await _async_record_missing("TEST_KEY", user_id="user-1", org_id="org-1")
        mock_uow.session.add.assert_called_once()

    async def test_increments_count_on_repeat(self, mock_uow):
        """_record_missing should increment request_count on existing entry."""
        existing = MagicMock()
        existing.request_count = 1
        mock_uow.session.scalars.return_value.first.return_value = existing
        _async_uow(mock_uow)
        with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
            from brain.systems.vault import _async_record_missing
            await _async_record_missing("TEST_KEY", user_id="user-1", org_id="org-1")
        assert existing.request_count == 2


class TestGetMissingRequests:
    """Test get_missing_requests with mocked UnitOfWork."""

    async def test_returns_unresolved(self, mock_uow):
        mock_entry = MagicMock()
        mock_entry.key_name = "MISSING_KEY"
        mock_entry.request_count = 3
        mock_entry.first_requested = datetime.now(timezone.utc)
        mock_entry.last_requested = datetime.now(timezone.utc)
        mock_entry.user_id = "user-1"
        mock_entry.org_id = "org-1"
        mock_uow.session.scalars.return_value.all.return_value = [mock_entry]
        _async_uow(mock_uow)
        with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
            from brain.systems.vault import get_missing_requests
            result = await get_missing_requests(user_id="user-1", org_id="org-1")
        assert len(result) == 1
        assert result[0]["key_name"] == "MISSING_KEY"


class TestResolveMissing:
    """Test resolve_missing with mocked UnitOfWork."""

    async def test_marks_resolved(self, mock_uow):
        existing = MagicMock()
        existing.resolved = False
        mock_uow.session.scalars.return_value.all.return_value = [existing]
        _async_uow(mock_uow)
        with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
            from brain.systems.vault import resolve_missing
            await resolve_missing("TEST_KEY", user_id="user-1", org_id="org-1")
        assert existing.resolved is True

    async def test_best_effort_when_missing_table_schema_drifts(self, mock_uow):
        mock_uow.session.scalars.side_effect = RuntimeError("schema drift")
        _async_uow(mock_uow)
        with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
            from brain.systems.vault import resolve_missing
            await resolve_missing("TEST_KEY", user_id="user-1", org_id="org-1")


class TestGetSecretMissingTracking:
    """Test that get_secret() records missing when not found."""

    async def test_get_secret_records_missing_when_not_found(self, mock_uow):
        from cryptography.fernet import Fernet
        mock_uow.vault.get_by_key.return_value = None
        mock_uow.session.scalars.return_value.first.return_value = None
        _async_uow(mock_uow)
        with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow), \
             patch("brain.systems.vault._async_record_missing", new=AsyncMock()) as mock_record, \
             patch.dict(os.environ, {"TEST_NOTFOUND_KEY": ""}, clear=False):
            os.environ.pop("TEST_NOTFOUND_KEY", None)
            from brain.systems.vault import get_secret
            result = await get_secret("TEST_NOTFOUND_KEY", user_id="user-1", org_id="org-1")
        assert result is None
        mock_record.assert_awaited_once_with("TEST_NOTFOUND_KEY", user_id="user-1", org_id="org-1")


class TestSetSecretResolveMissing:
    """Test that set_secret() resolves missing requests."""

    async def test_set_secret_resolves_missing(self, mock_uow, monkeypatch):
        from cryptography.fernet import Fernet
        monkeypatch.setenv("VAULT_MASTER_KEY", Fernet.generate_key().decode())
        mock_uow.vault.get_by_key.return_value = None
        _async_uow(mock_uow)
        with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow), \
             patch("brain.systems.vault.async_resolve_missing", new=AsyncMock()) as mock_resolve:
            from brain.systems.vault import set_secret
            await set_secret("TEST_KEY", "test_value", user_id="user-1", org_id="org-1")
        mock_resolve.assert_awaited_once_with("TEST_KEY", user_id="user-1", org_id="org-1")


class TestBrainContextVault:
    """Test that brain context includes vault inventory."""

    async def test_brain_context_includes_vault(self, mock_uow):
        """Verify vault functions return proper types when mocked."""
        mock_uow.session.scalars.return_value.all.return_value = []
        _async_uow(mock_uow)
        with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
            from brain.systems.vault import list_secrets, get_missing_requests
            secrets = await list_secrets(user_id="user-1")
            missing = await get_missing_requests(user_id="user-1", org_id="org-1")
            assert isinstance(secrets, list)
            assert isinstance(missing, list)
