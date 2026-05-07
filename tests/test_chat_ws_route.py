"""Focused websocket route checks for chat authorization guards."""
from __future__ import annotations

from starlette.testclient import TestClient

from brain.app.api.main import app
from brain.app.api.ws.auth import create_ws_token


def _ws_token(user_id: str = "user-1", org_id: str = "org-1") -> str:
    token, _ = create_ws_token(
        {"id": user_id, "org_id": org_id},
        session_id=f"session-{user_id}",
    )
    return token


def test_ws_rejects_unauthorized_chat_conversation_subscription(monkeypatch):
    from brain.app.api.routers import ws as ws_router

    def fake_authorize(
        user_id: str,
        *,
        org_id: str,
        conversation_id: str,
        thread_root_message_id: int | None = None,
    ) -> str | None:
        assert user_id == "user-1"
        assert org_id == "org-1"
        assert thread_root_message_id is None
        if conversation_id == "blocked-room":
            return "CHAT_CONVERSATION_FORBIDDEN"
        return None

    monkeypatch.setattr(ws_router, "_authorize_chat_subscription", fake_authorize)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "connected"
            ws.send_json({"type": "auth", "token": _ws_token()})
            assert ws.receive_json()["type"] == "authenticated"

            ws.send_json(
                {
                    "type": "chat_subscribe_conversation",
                    "conversation_id": "blocked-room",
                }
            )

            assert ws.receive_json() == {
                "type": "chat_error",
                "code": "CHAT_CONVERSATION_FORBIDDEN",
            }
