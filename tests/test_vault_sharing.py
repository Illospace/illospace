"""
Tests for per-user vault with sharing — FastAPI routes.

Tests cover:
- user-scoped vault CRUD (API delegation)
- secret sharing and revocation
- access logging
- cross-user isolation logic
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from brain.app.api.main import app

USER_A = {
    "id": "aaaa0000-0000-0000-0000-000000000001",
    "email": "alice@example.test", "name": "Alice", "role": "owner",
    "color": "#6366f1",
    "org_id": "org00000-0000-0000-0000-000000000001",
    "org_name": "Example", "org_slug": "example", "attribution_enabled": True,
}

USER_B = {
    "id": "bbbb0000-0000-0000-0000-000000000002",
    "email": "bob@example.test", "name": "Bob", "role": "member",
    "color": "#ed8936",
    "org_id": "org00000-0000-0000-0000-000000000001",
    "org_name": "Example", "org_slug": "example", "attribution_enabled": True,
}


@pytest.fixture()
def client():
    """TestClient with auth and DB dependencies overridden."""
    from brain.app.api.auth import get_current_user
    from brain.app.api.deps import get_db

    app.dependency_overrides[get_current_user] = lambda: USER_A
    app.dependency_overrides[get_db] = lambda: MagicMock()
    with patch("brain.systems.vault.async_has_pin", return_value=False):
        yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


# ── Unit tests for vault user scoping ────────────────────────────────────────

class TestVaultUserScoping:

    def test_list_secrets_returns_user_secrets(self, client):
        """GET /api/vault/ should return scoped vault metadata."""
        with patch("brain.systems.vault.async_list_secrets", return_value=[]) as mock_list:
            resp = client.get("/api/vault/")
        assert resp.status_code == 200
        mock_list.assert_called_once_with(USER_A["id"], category=None, org_id=USER_A["org_id"])

    def test_create_secret_calls_set_secret(self, client):
        """POST /api/vault should call vault.async_set_secret with user_id."""
        from datetime import datetime, timezone
        mock_secret = MagicMock()
        mock_secret.id = 1
        mock_secret.key_name = "TEST_KEY"
        mock_secret.description = ""
        mock_secret.category = "general"
        mock_secret.created_at = datetime.now(timezone.utc)
        mock_secret.updated_at = datetime.now(timezone.utc)
        mock_secret.last_accessed_at = None
        mock_secret.access_count = 0
        mock_secret.user_id = USER_A["id"]
        mock_secret.is_shared = False
        mock_secret.shared_by_name = None
        with patch("brain.systems.vault.async_set_secret") as mock_set, \
             patch("brain.systems.vault.async_get_secret_record", return_value=mock_secret):
            resp = client.post("/api/vault/",
                               json={"key_name": "TEST_KEY", "value": "secret123"})
        assert resp.status_code == 201
        mock_set.assert_called_once()
        _, kwargs = mock_set.call_args
        assert kwargs.get("user_id") == USER_A["id"]
        assert kwargs.get("org_id") == USER_A["org_id"]

    def test_create_secret_reports_missing_vault_master_key(self, client):
        with patch(
            "brain.systems.vault.async_set_secret",
            side_effect=RuntimeError("VAULT_MASTER_KEY is required. Refusing to auto-generate a vault key."),
        ):
            resp = client.post("/api/vault/", json={"key_name": "TEST_KEY", "value": "secret123"})

        assert resp.status_code == 503
        assert "VAULT_MASTER_KEY" in resp.json()["detail"]

    def test_reveal_passes_user_id(self, client):
        """GET /api/vault/<key> should pass user_id to vault.async_reveal_secret."""
        with patch("brain.systems.vault.async_reveal_secret", return_value="secret_value") as mock_reveal:
            resp = client.get("/api/vault/MY_KEY")
        assert resp.status_code == 200
        mock_reveal.assert_called_once_with("MY_KEY", user_id=USER_A["id"], org_id=USER_A["org_id"])

    def test_reveal_reports_missing_vault_master_key(self, client):
        with patch(
            "brain.systems.vault.async_reveal_secret",
            side_effect=RuntimeError("VAULT_MASTER_KEY is required. Refusing to auto-generate a vault key."),
        ):
            resp = client.get("/api/vault/MY_KEY")

        assert resp.status_code == 503
        assert "VAULT_MASTER_KEY" in resp.json()["detail"]

    def test_delete_passes_user_id(self, client):
        """DELETE /api/vault/<key> should pass user_id."""
        with patch("brain.systems.vault.async_delete_secret", return_value=True) as mock_del:
            resp = client.delete("/api/vault/MY_KEY")
        assert resp.status_code == 200
        mock_del.assert_called_once_with("MY_KEY", user_id=USER_A["id"])

    @pytest.mark.asyncio
    async def test_update_secret_metadata_uses_async_session(self):
        """PUT /api/vault/{key} updates metadata without a sync session bridge."""
        from brain.app.api.routers import vault as vault_router

        secret = SimpleNamespace(description="old", category="general", agent_access_level="ask")
        scalar_result = MagicMock()
        scalar_result.first.return_value = secret
        db = MagicMock()
        db.scalars = AsyncMock(return_value=scalar_result)
        db.flush = AsyncMock()

        with patch.object(vault_router, "_async_require_unlocked", new=AsyncMock()):
            result = await vault_router.update_secret(
                "MY_KEY",
                vault_router.SecretUpdate(description="new", agent_access_level="available"),
                request=MagicMock(headers={}),
                db=db,
                user=USER_A,
            )

        assert result == {"updated": True}
        assert secret.description == "new"
        assert secret.agent_access_level == "available"
        db.flush.assert_awaited_once()


# ── Sharing endpoints ────────────────────────────────────────────────────────

class TestVaultSharing:

    def test_share_secret_200(self, client):
        share_result = {"id": 1, "secret_id": 42, "shared_at": "2026-03-13T00:00:00"}
        with patch("brain.systems.vault.async_share_secret", return_value=share_result) as share:
            resp = client.post("/api/vault/42/share",
                               json={"shared_with_user_id": USER_B["id"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["secret_id"] == 42
        share.assert_called_once_with(42, USER_B["id"], USER_A["id"], org_id=USER_A["org_id"])

    def test_share_not_found_404(self, client):
        with patch("brain.systems.vault.async_share_secret", return_value=None):
            resp = client.post("/api/vault/999/share",
                               json={"shared_with_user_id": USER_B["id"]})
        assert resp.status_code == 404

    def test_revoke_share_200(self, client):
        with patch("brain.systems.vault.async_revoke_share", return_value=True):
            resp = client.delete("/api/vault/shares/1")
        assert resp.status_code == 200

    def test_vault_log(self, client):
        with patch("brain.systems.vault.async_get_vault_access_log") as mock_log:
            mock_log.return_value = [
                {"id": 1, "key_name": "TEST", "action": "read", "accessed_at": "2026-03-13T00:00:00"}
            ]
            resp = client.get("/api/vault/log")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_org_users(self, client):
        with patch(
            "brain.systems.vault.async_get_org_users",
            return_value=[{"id": USER_B["id"], "name": "Bob", "email": "bob@example.test"}],
        ):
            resp = client.get("/api/vault/org-users")
        assert resp.status_code == 200


# ── Cross-user isolation logic ───────────────────────────────────────────────

class TestVaultIsolation:

    async def test_user_b_cannot_see_user_a_secrets(self):
        """Vault list_secrets with user_id should scope by user via ORM."""
        from brain.systems.vault import list_secrets

        mock_uow = MagicMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        scalars_result = MagicMock()
        scalars_result.all.return_value = []
        execute_result = MagicMock()
        execute_result.all.return_value = []
        mock_uow.session.scalars = AsyncMock(return_value=scalars_result)
        mock_uow.session.execute = AsyncMock(return_value=execute_result)

        with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow):
            result = await list_secrets(user_id=USER_B["id"])
        assert isinstance(result, list)
        mock_uow.session.scalars.assert_awaited()

    async def test_get_secret_checks_user_id(self):
        """get_secret with user_id should use vault repo's user-scoped lookup."""
        from brain.systems.vault import get_secret

        mock_uow = MagicMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.vault.get_by_key = AsyncMock(return_value=None)
        scalars_result = MagicMock()
        scalars_result.first.return_value = None
        mock_uow.session.scalars = AsyncMock(return_value=scalars_result)

        with patch("brain.systems.vault.UnitOfWork", return_value=mock_uow), \
             patch("brain.systems.vault._async_record_missing", new=AsyncMock()), \
             patch.dict(os.environ, {}, clear=False):
            result = await get_secret("SOME_KEY", user_id=USER_A["id"])
        assert result is None
        mock_uow.vault.get_by_key.assert_awaited_once_with(USER_A["id"], "SOME_KEY")
