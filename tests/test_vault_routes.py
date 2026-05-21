"""FastAPI route coverage for the org-owned vault."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from brain.app.api.main import app


USER_A = {
    "id": "aaaa0000-0000-0000-0000-000000000001",
    "email": "alice@example.test",
    "name": "Alice",
    "role": "owner",
    "color": "#6366f1",
    "org_id": "org00000-0000-0000-0000-000000000001",
    "org_name": "Example",
    "org_slug": "example",
    "attribution_enabled": True,
}


@pytest.fixture()
def client():
    """TestClient with auth and DB dependencies overridden."""
    from brain.app.api.auth import get_current_user
    from brain.app.api.deps import get_db

    app.dependency_overrides[get_current_user] = lambda: USER_A
    app.dependency_overrides[get_db] = lambda: MagicMock()
    with patch("brain.systems.vault.async_has_pin", return_value=True), \
         patch("brain.systems.vault.async_validate_vault_token", return_value=True):
        yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _secret():
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=1,
        key_name="TEST_KEY",
        description="",
        category="general",
        created_at=now,
        updated_at=now,
        last_accessed_at=None,
        access_count=0,
        org_id=USER_A["org_id"],
        created_by_user_id=USER_A["id"],
        updated_by_user_id=USER_A["id"],
        agent_access_level="ask",
    )


class TestVaultOrgRoutes:
    def test_list_secrets_returns_org_metadata(self, client):
        with patch("brain.systems.vault.async_list_secrets", return_value=[]) as mock_list:
            resp = client.get("/api/vault/")

        assert resp.status_code == 200
        mock_list.assert_called_once_with(USER_A["id"], category=None, org_id=USER_A["org_id"])

    def test_create_secret_calls_org_set_secret(self, client):
        with patch("brain.systems.vault.async_set_secret") as mock_set, \
             patch("brain.systems.vault.async_get_secret_record", return_value=_secret()):
            resp = client.post("/api/vault/", json={"key_name": "TEST_KEY", "value": "secret123"})

        assert resp.status_code == 201
        _, kwargs = mock_set.call_args
        assert kwargs["actor_user_id"] == USER_A["id"]
        assert kwargs["org_id"] == USER_A["org_id"]

    def test_create_secret_reports_missing_vault_master_key(self, client):
        with patch(
            "brain.systems.vault.async_set_secret",
            side_effect=RuntimeError("VAULT_MASTER_KEY is required. Refusing to auto-generate a vault key."),
        ):
            resp = client.post("/api/vault/", json={"key_name": "TEST_KEY", "value": "secret123"})

        assert resp.status_code == 503
        assert "VAULT_MASTER_KEY" in resp.json()["detail"]

    def test_reveal_passes_actor_and_org(self, client):
        with patch("brain.systems.vault.async_reveal_secret", return_value="secret_value") as mock_reveal:
            resp = client.get("/api/vault/MY_KEY")

        assert resp.status_code == 200
        mock_reveal.assert_called_once_with("MY_KEY", actor_user_id=USER_A["id"], org_id=USER_A["org_id"])

    def test_delete_passes_actor_and_org(self, client):
        with patch("brain.systems.vault.async_delete_secret", return_value=True) as mock_del:
            resp = client.delete("/api/vault/MY_KEY")

        assert resp.status_code == 200
        mock_del.assert_called_once_with("MY_KEY", actor_user_id=USER_A["id"], org_id=USER_A["org_id"])

    def test_vault_log_uses_org_scope(self, client):
        with patch("brain.systems.vault.async_get_vault_access_log") as mock_log:
            mock_log.return_value = [
                {"id": 1, "key_name": "TEST", "action": "read", "accessed_at": "2026-03-13T00:00:00"}
            ]
            resp = client.get("/api/vault/log")

        assert resp.status_code == 200
        assert len(resp.json()) == 1
        mock_log.assert_called_once_with(USER_A["id"], org_id=USER_A["org_id"], limit=100)

    @pytest.mark.asyncio
    async def test_update_secret_metadata_uses_async_session(self):
        from brain.app.api.routers import vault as vault_router

        secret = SimpleNamespace(description="old", category="general", agent_access_level="ask")
        db = MagicMock()
        db.flush = AsyncMock()

        with patch.object(vault_router, "_async_require_unlocked", new=AsyncMock()), \
             patch("brain.platform.db.repositories.vault.VaultRepository.a_get_by_key", new=AsyncMock(return_value=secret)):
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
