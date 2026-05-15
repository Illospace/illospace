import pytest
import pytest_asyncio
from unittest.mock import patch
from httpx import ASGITransport, AsyncClient
from fastapi import HTTPException
from brain.app.api.main import app


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_me_without_session_ignores_localhost_human_fallback(client):
    with patch(
        "brain.app.api.auth._get_localhost_user",
        return_value={
            "id": "user-1",
            "principal_type": "human",
            "name": "Alex",
            "email": "alex@example.com",
            "role": "owner",
            "color": "#111111",
            "org_id": "org-1",
            "org_name": "Example",
            "approved": True,
            "attribution_enabled": True,
            "default_provider": "anthropic",
        },
    ) as get_localhost_user:
        resp = await client.get("/api/me")

    assert resp.status_code == 200
    assert resp.json() is None
    get_localhost_user.assert_not_called()


@pytest.mark.asyncio
async def test_setup_check_exposes_workspace_context(client):
    with patch("brain.app.api.routers.auth.async_has_any_users", return_value=True), \
        patch(
            "brain.app.api.routers.auth.async_get_default_org_summary",
            return_value={"id": "org-1", "name": "Main Workspace", "slug": "main"},
        ), \
        patch(
            "brain.app.api.routers.auth.async_get_org_summary_by_slug",
            return_value={"id": "org-2", "name": "Design Studio", "slug": "design"},
        ):
        resp = await client.get("/api/auth/setup-check?workspace=design")

    assert resp.status_code == 200
    data = resp.json()
    assert data["setup_required"] is False
    assert data["default_org"]["name"] == "Main Workspace"
    assert data["requested_org"]["name"] == "Design Studio"


@pytest.mark.asyncio
async def test_register_can_request_access_to_invited_workspace(client):
    created = {
        "id": "user-2",
        "name": "Jamie",
        "email": "jamie@example.com",
        "role": "member",
        "color": "#111111",
        "org_id": "org-2",
        "org_name": "Design Studio",
        "org_slug": "design",
        "approved": False,
        "attribution_enabled": True,
        "default_provider": None,
    }
    with patch("brain.app.api.routers.auth.async_get_user_by_email", return_value=None), \
        patch("brain.app.api.routers.auth.async_has_any_users", return_value=True), \
        patch(
            "brain.app.api.routers.auth.async_get_org_summary_by_slug",
            return_value={"id": "org-2", "name": "Design Studio", "slug": "design"},
        ) as get_org, \
        patch("brain.app.api.routers.auth.async_create_user", return_value=created) as create_user:
        resp = await client.post(
            "/api/register",
            json={
                "name": "Jamie",
                "email": "jamie@example.com",
                "password": "password123",
                "workspace_mode": "join",
                "workspace_slug": "design",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["approved"] is False
    get_org.assert_called_once_with("design")
    create_user.assert_called_once()
    assert create_user.call_args.args[:4] == ("Jamie", "jamie@example.com", "password123", "org-2")


@pytest.mark.asyncio
async def test_register_join_requires_invite_workspace(client):
    with patch("brain.app.api.routers.auth.async_get_user_by_email", return_value=None), \
        patch("brain.app.api.routers.auth.async_has_any_users", return_value=True), \
        patch("brain.app.api.routers.auth.async_create_user") as create_user:
        resp = await client.post(
            "/api/register",
            json={
                "name": "Jamie",
                "email": "jamie@example.com",
                "password": "password123",
                "workspace_mode": "join",
            },
        )

    assert resp.status_code == 400
    assert resp.json()["error"] == "Invite link required to join a workspace"
    create_user.assert_not_called()


@pytest.mark.asyncio
async def test_register_can_create_workspace_after_first_setup(client):
    created = {
        "id": "owner-2",
        "name": "Morgan",
        "email": "morgan@example.com",
        "role": "owner",
        "color": "#111111",
        "org_id": "org-new",
        "org_name": "New Lab",
        "org_slug": "new-lab",
        "approved": True,
        "attribution_enabled": True,
        "default_provider": None,
    }
    with patch("brain.app.api.routers.auth.async_get_user_by_email", return_value=None), \
        patch("brain.app.api.routers.auth.async_has_any_users", return_value=True), \
        patch("brain.app.api.routers.auth.async_create_workspace_owner", return_value=created) as create_owner:
        resp = await client.post(
            "/api/register",
            json={
                "name": "Morgan",
                "email": "morgan@example.com",
                "password": "password123",
                "workspace_mode": "create",
                "org_name": "New Lab",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["role"] == "owner"
    assert resp.json()["approved"] is True
    create_owner.assert_called_once_with("Morgan", "morgan@example.com", "password123", "New Lab")


@pytest.mark.asyncio
async def test_register_workspace_name_wins_over_stale_invite_state(client):
    created = {
        "id": "owner-3",
        "name": "Taylor",
        "email": "taylor@example.com",
        "role": "owner",
        "color": "#111111",
        "org_id": "org-newer",
        "org_name": "Fresh Workspace",
        "org_slug": "fresh-workspace",
        "approved": True,
        "attribution_enabled": True,
        "default_provider": None,
    }
    with patch("brain.app.api.routers.auth.async_get_user_by_email", return_value=None), \
        patch("brain.app.api.routers.auth.async_has_any_users", return_value=True), \
        patch("brain.app.api.routers.auth.async_create_workspace_owner", return_value=created) as create_owner, \
        patch("brain.app.api.routers.auth.async_create_user") as create_user:
        resp = await client.post(
            "/api/register",
            json={
                "name": "Taylor",
                "email": "taylor@example.com",
                "password": "password123",
                "workspace_mode": "join",
                "workspace_slug": "uwear-test",
                "org_name": "Fresh Workspace",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["role"] == "owner"
    assert resp.json()["approved"] is True
    create_owner.assert_called_once_with(
        "Taylor",
        "taylor@example.com",
        "password123",
        "Fresh Workspace",
    )
    create_user.assert_not_called()


class _Client:
    def __init__(self, host: str):
        self.host = host


class _Request:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        session: dict[str, str] | None = None,
        host: str = "203.0.113.10",
    ):
        self.headers = headers or {}
        self.session = session or {}
        self.client = _Client(host)


@pytest.mark.asyncio
async def test_internal_bearer_uses_service_principal_not_owner_fallback():
    from brain.app.api import auth

    request = _Request(headers={"Authorization": "Bearer test-token"})
    with patch.object(auth, "INTERNAL_BEARER_TOKENS", {"test-token"}), \
        patch.object(auth, "INTERNAL_BEARER_TOKEN_SOURCES", {"test-token": "illo_api_token"}), \
        patch.object(auth, "_get_localhost_user") as mock_localhost_user:
        user = await auth.get_current_user(request)

    assert user["id"] == "service:internal-api"
    assert user["principal_type"] == "service"
    assert user["role"] == "service"
    assert user["internal"] is True
    assert user["audit"]["token_source"] == "illo_api_token"
    assert "run:manage" in user["permissions"]
    mock_localhost_user.assert_not_called()


@pytest.mark.asyncio
async def test_localhost_fallback_rejected_when_disabled():
    from brain.app.api import auth

    request = _Request(host="127.0.0.1")
    with patch.object(auth, "AUTH_DEV_FALLBACK_ENABLED", False), \
        patch.object(auth, "_get_localhost_user") as mock_localhost_user:
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(request)

    assert exc_info.value.status_code == 401
    mock_localhost_user.assert_not_called()


@pytest.mark.asyncio
async def test_localhost_fallback_requires_local_request_host():
    from brain.app.api import auth

    request = _Request(host="127.0.0.1", headers={"host": "staging.example.com"})
    with patch.object(auth, "AUTH_DEV_FALLBACK_ENABLED", True), \
        patch.object(auth, "_get_localhost_user") as mock_localhost_user:
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(request)

    assert exc_info.value.status_code == 401
    mock_localhost_user.assert_not_called()


@pytest.mark.asyncio
async def test_localhost_fallback_returns_explicit_dev_service_principal_when_enabled_without_user():
    from brain.app.api import auth

    request = _Request(host="127.0.0.1")
    with patch.object(auth, "AUTH_DEV_FALLBACK_ENABLED", True), \
        patch.object(auth, "_get_localhost_user", return_value=None):
        user = await auth.get_current_user(request)

    assert user["id"] == "service:dev-localhost"
    assert user["principal_type"] == "service"
    assert user["role"] == "service"
    assert user["internal"] is True
    assert user["audit"]["token_source"] == "localhost"


@pytest.mark.asyncio
async def test_session_auth_preserves_existing_user_context_with_identity_metadata():
    from brain.app.api import auth

    db_user = {
        "id": "user-1",
        "name": "Alex",
        "email": "alex@example.com",
        "role": "owner",
        "color": "#111111",
        "org_id": "org-1",
        "org_name": "Example",
        "approved": True,
        "attribution_enabled": False,
        "default_provider": "anthropic",
    }
    request = _Request(session={"user_id": "user-1"})

    with patch("brain.systems.auth.users.async_get_user_by_id", return_value=db_user), \
        patch("brain.systems.auth.users.safe_user_context", return_value=db_user):
        user = await auth.get_current_user(request)

    assert user["id"] == "user-1"
    assert user["role"] == "owner"
    assert user["org_id"] == "org-1"
    assert user["principal_type"] == "human"
    assert user["audit"]["principal_type"] == "human"
    assert "run:manage" in user["permissions"]
