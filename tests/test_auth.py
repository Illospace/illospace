"""
Tests for Phase 1 multiplayer auth: user identity, session login, route protection.

Tests use FastAPI TestClient with mocked DB — no live DB required.
"""
from __future__ import annotations

import os
import sys
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import bcrypt
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Patch runtime side effects before importing server.
with patch("brain.systems.runs.cortex.ensure_schema"), \
     patch("brain.systems.runs.cortex.start_runner"):
    import brain.app.api.main as server_module
    from brain.app.api.main import app

from starlette.testclient import TestClient


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_user(
    user_id="aaaaaaaa-0000-0000-0000-000000000001",
    email="alex@illo.ai",
    name="Alex Example",
    role="owner",
    password="hunter2",
):
    return {
        "id":                  user_id,
        "email":               email,
        "name":                name,
        "role":                role,
        "color":               "#6366f1",
        "org_id":              "bbbbbbbb-0000-0000-0000-000000000001",
        "org_name":            "Example",
        "org_slug":            "illo",
        "attribution_enabled": True,
        "default_provider":    None,
        "password_hash":       bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
    }


@pytest.fixture
def client():
    return TestClient(app)


# ──────────────────────────────────────────────────────────────────────────────
# Unit tests: authenticate()
# ──────────────────────────────────────────────────────────────────────────────

class TestAuthenticate:
    async def test_correct_password_returns_user(self):
        user = _make_user(password="correct")
        with patch("brain.systems.auth.users.async_get_user_by_email", new=AsyncMock(return_value=user)):
            from brain.systems.auth.users import async_authenticate
            result = await async_authenticate("alex@illo.ai", "correct")
            assert result is not None
            assert result["email"] == "alex@illo.ai"

    async def test_wrong_password_returns_none(self):
        user = _make_user(password="correct")
        with patch("brain.systems.auth.users.async_get_user_by_email", new=AsyncMock(return_value=user)):
            from brain.systems.auth.users import async_authenticate
            result = await async_authenticate("alex@illo.ai", "wrong")
            assert result is None

    async def test_unknown_email_returns_none(self):
        with patch("brain.systems.auth.users.async_get_user_by_email", new=AsyncMock(return_value=None)):
            from brain.systems.auth.users import async_authenticate
            result = await async_authenticate("nobody@example.com", "anything")
            assert result is None

    async def test_no_password_hash_returns_none(self):
        user = _make_user()
        user["password_hash"] = None
        with patch("brain.systems.auth.users.async_get_user_by_email", new=AsyncMock(return_value=user)):
            from brain.systems.auth.users import async_authenticate
            result = await async_authenticate("alex@illo.ai", "hunter2")
            assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# Route tests: /api/login
# ──────────────────────────────────────────────────────────────────────────────

class TestLoginRoute:
    def test_post_valid_credentials_succeeds(self, client):
        user = _make_user(password="hunter2")
        with patch("brain.app.api.routers.auth.async_authenticate", return_value=user):
            resp = client.post("/api/login",
                               json={"email": "alex@illo.ai", "password": "hunter2"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True

    def test_post_invalid_credentials_returns_401(self, client):
        with patch("brain.app.api.routers.auth.async_authenticate", return_value=None):
            resp = client.post("/api/login",
                               json={"email": "alex@illo.ai", "password": "wrong"})
            assert resp.status_code == 401
            data = resp.json()
            assert "credentials" in data.get("error", "").lower()


# ──────────────────────────────────────────────────────────────────────────────
# Route tests: auth protection
# ──────────────────────────────────────────────────────────────────────────────

class TestRouteProtection:
    def test_health_endpoint_accessible(self, client):
        """GET /api/health returns 200 without auth."""
        resp = client.get("/api/health")
        assert resp.status_code == 200
        # Health returns ok or degraded depending on DB availability
        assert resp.json()["status"] in ("ok", "degraded")

    def test_me_returns_null_when_unauthenticated(self, client):
        """GET /api/me returns 200 with null body when no session."""
        with patch("brain.app.api.auth.AUTH_DEV_FALLBACK_ENABLED", False):
            resp = client.get("/api/me")
        assert resp.status_code == 200
        assert resp.json() is None


# ──────────────────────────────────────────────────────────────────────────────
# Session tests
# ──────────────────────────────────────────────────────────────────────────────

class TestSession:
    def test_login_then_me_returns_user(self, client):
        """Login sets session; subsequent /api/me returns user info."""
        user = _make_user()
        with patch("brain.app.api.routers.auth.async_authenticate", return_value=user):
            resp = client.post("/api/login",
                               json={"email": "alex@illo.ai", "password": "hunter2"})
            assert resp.status_code == 200

        # Session cookie is preserved by TestClient — mock get_user_by_id for /api/me
        with patch("brain.app.api.routers.auth.async_get_user_by_id", return_value=user):
            resp = client.get("/api/me")
            assert resp.status_code == 200
            data = resp.json()
            assert data is not None
            assert data["email"] == "alex@illo.ai"

    def test_logout_clears_session(self, client):
        """POST /api/logout clears the session."""
        user = _make_user()
        with patch("brain.app.api.routers.auth.async_authenticate", return_value=user):
            client.post("/api/login",
                        json={"email": "alex@illo.ai", "password": "hunter2"})

        resp = client.post("/api/logout")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Session should be cleared
        with patch("brain.app.api.auth.AUTH_DEV_FALLBACK_ENABLED", False):
            resp = client.get("/api/me")
        assert resp.status_code == 200
        assert resp.json() is None


# ──────────────────────────────────────────────────────────────────────────────
# Cross-user isolation (DB-level)
# ──────────────────────────────────────────────────────────────────────────────

class TestCrossUserIsolation:
    """
    Verifies that the auth module never returns user B's data when querying for user A.
    These are unit tests against the query functions — full integration tests
    require a live DB with two users and should run in the migration acceptance suite.
    """

    async def test_get_user_by_email_returns_none_for_unknown(self):
        """DB layer: unknown email -> None (not another user's record)."""
        mock_uow = MagicMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.team.get_by_email = AsyncMock(return_value=None)

        with patch("brain.systems.auth.users.UnitOfWork", return_value=mock_uow):
            from brain.systems.auth.users import async_get_user_by_email
            result = await async_get_user_by_email("nobody@example.com")
            assert result is None

    async def test_get_user_by_id_returns_none_for_wrong_id(self):
        """DB layer: wrong user_id -> None."""
        mock_uow = MagicMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.team.get_by_id = AsyncMock(return_value=None)

        with patch("brain.systems.auth.users.UnitOfWork", return_value=mock_uow):
            from brain.systems.auth.users import async_get_user_by_id
            result = await async_get_user_by_id("ffffffff-ffff-ffff-ffff-ffffffffffff")
            assert result is None

    def test_safe_user_context_strips_password_hash(self):
        """password_hash must NEVER appear in session context."""
        from brain.systems.auth.users import safe_user_context
        user = _make_user()
        ctx = safe_user_context(user)
        assert "password_hash" not in ctx
        assert "vault_salt" not in ctx
