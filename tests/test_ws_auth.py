from __future__ import annotations

from types import SimpleNamespace

from starlette.testclient import TestClient

from brain.app.api.auth import get_current_user
from brain.app.api.main import app
from brain.app.api.ws.auth import create_ws_token, verify_ws_token


def _user_context(user_id: str = "user-1", org_id: str = "org-1") -> dict:
    return {
        "id": user_id,
        "org_id": org_id,
        "principal_type": "human",
        "role": "member",
        "name": "Test User",
    }


def _token(
    user_id: str = "user-1",
    org_id: str = "org-1",
    *,
    ttl_seconds: int = 60,
) -> str:
    token, _ = create_ws_token(
        _user_context(user_id, org_id),
        session_id=f"session-{user_id}",
        ttl_seconds=ttl_seconds,
    )
    return token


def test_ws_token_endpoint_binds_claims_to_current_session_user():
    app.dependency_overrides[get_current_user] = lambda: _user_context(
        "user-1",
        "org-1",
    )
    try:
        with TestClient(app) as client:
            first = client.post("/api/auth/ws-token", json={"tab_id": "tab-a"})
            second = client.post("/api/auth/ws-token", json={"tab_id": "tab-b"})
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    first_claims = verify_ws_token(first_payload["token"])
    second_claims = verify_ws_token(second_payload["token"])
    assert first_claims.user_id == "user-1"
    assert first_claims.org_id == "org-1"
    assert first_claims.session_id == first_payload["session_id"]
    assert first_claims.session_id == second_claims.session_id
    assert first_claims.tab_id == "tab-a"
    assert second_claims.tab_id == "tab-b"


def test_ws_token_endpoint_rejects_service_principals():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "service:internal-api",
        "org_id": None,
        "principal_type": "service",
    }
    try:
        with TestClient(app) as client:
            response = client.post("/api/auth/ws-token", json={})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


def test_ws_token_preserves_signed_principal_permissions():
    token, _ = create_ws_token(
        {
            "id": "service:worker",
            "org_id": "org-1",
            "principal_type": "service",
            "permissions": ["run:manage", "internal:api"],
        },
        session_id="session-service",
    )

    claims = verify_ws_token(token)

    assert claims.user_id == "service:worker"
    assert claims.org_id == "org-1"
    assert claims.principal_type == "service"
    assert claims.permissions == ("run:manage", "internal:api")


def test_ws_rejects_client_asserted_user_without_token():
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "connected"
            ws.send_json({"type": "auth", "user_id": "user-1"})
            assert ws.receive_json() == {
                "type": "error",
                "code": "WS_TOKEN_REQUIRED",
            }


def test_ws_rejects_malformed_token():
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "connected"
            ws.send_json({"type": "auth", "token": "not-a-token"})
            assert ws.receive_json() == {
                "type": "error",
                "code": "WS_TOKEN_INVALID",
            }


def test_ws_rejects_expired_token():
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "connected"
            ws.send_json({"type": "auth", "token": _token(ttl_seconds=-1)})
            assert ws.receive_json() == {
                "type": "error",
                "code": "WS_TOKEN_EXPIRED",
            }


def test_ws_rejects_cross_user_spoof_attempt():
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "connected"
            ws.send_json(
                {
                    "type": "auth",
                    "token": _token("user-1", "org-1"),
                    "user_id": "user-2",
                }
            )
            assert ws.receive_json() == {
                "type": "error",
                "code": "WS_TOKEN_CLAIM_MISMATCH",
            }


def test_ws_rejects_cross_org_spoof_attempt():
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "connected"
            ws.send_json(
                {
                    "type": "auth",
                    "token": _token("user-1", "org-1"),
                    "org_id": "org-2",
                }
            )
            assert ws.receive_json() == {
                "type": "error",
                "code": "WS_TOKEN_CLAIM_MISMATCH",
            }


def test_ws_binds_authenticated_socket_to_token_claims():
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "connected"
            ws.send_json({"type": "auth", "token": _token("user-1", "org-1")})
            authenticated = ws.receive_json()

    assert authenticated["type"] == "authenticated"
    assert authenticated["user_id"] == "user-1"
    assert authenticated["org_id"] == "org-1"
    assert authenticated["session_id"] == "session-user-1"


def test_chat_subscription_authorization_rejects_cross_org_conversation(monkeypatch):
    from brain.app.api.routers import ws as ws_router

    class FakeConversationRepository:
        def __init__(self, session):
            self.session = session

        def get_for_user(self, conversation_id: str, user_id: str):
            assert conversation_id == "conversation-1"
            assert user_id == "user-1"
            return SimpleNamespace(id=conversation_id, org_id="org-2")

    fake_session = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(ws_router, "SessionFactory", lambda: fake_session)
    monkeypatch.setattr(ws_router, "ChatConversationRepository", FakeConversationRepository)

    assert (
        ws_router._authorize_chat_subscription(
            "user-1",
            org_id="org-1",
            conversation_id="conversation-1",
        )
        == "CHAT_CONVERSATION_FORBIDDEN"
    )
