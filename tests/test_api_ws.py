from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from brain.app.api.main import app
from brain.app.api.ws.auth import WsTokenClaims, create_ws_token
from brain.app.api.ws.events import ServerEvent


def _ws_token(user_id: str = "user-1", org_id: str = "org-1") -> str:
    token, _ = create_ws_token(
        {"id": user_id, "org_id": org_id},
        session_id=f"session-{user_id}",
    )
    return token


def test_ws_authenticates_then_replays_requested_run_cursor(monkeypatch):
    from brain.app.api.routers import ws as ws_router

    async def fake_replay(socket, claims, cursors, **kwargs):
        assert claims.user_id == "user-1"
        assert claims.org_id == "org-1"
        assert cursors == {"run": 12}
        await socket.send_json(
            {
                "type": ServerEvent.EVENT_REPLAY_COMPLETE,
                "channel": "run",
                "from_cursor": 12,
                "last_event_id": 12,
                "delivered": 0,
                "has_more": False,
                "limit": 100,
            }
        )

    monkeypatch.setattr(ws_router, "_replay_durable_events", fake_replay)

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "auth", "token": _ws_token(), "cursor": 12})
        assert ws.receive_json()["type"] == "authenticated"
        replay_complete = ws.receive_json()

    assert replay_complete == {
        "type": "event_replay_complete",
        "channel": "run",
        "from_cursor": 12,
        "last_event_id": 12,
        "delivered": 0,
        "has_more": False,
        "limit": 100,
    }


def test_ws_replay_cursor_parser_accepts_run_and_cortex_aliases():
    from brain.app.api.routers import ws as ws_router

    cursors, errors = ws_router._parse_event_replay_cursors(
        {
            "type": "auth",
            "cursors": {"run_events": "7", "cortex": 3},
        }
    )

    assert cursors == {"run": 7, "cortex": 3}
    assert errors == []


def test_ws_replay_cursor_parser_reports_invalid_cursor():
    from brain.app.api.routers import ws as ws_router

    cursors, errors = ws_router._parse_event_replay_cursors(
        {
            "type": "auth",
            "cursors": {"run": -1, "cortex": True},
        }
    )

    assert cursors == {}
    assert errors == [
        {"code": "EVENT_REPLAY_CURSOR_INVALID", "channel": "run"},
        {"code": "EVENT_REPLAY_CURSOR_INVALID", "channel": "cortex"},
    ]


async def test_cortex_replay_query_scopes_human_principal_to_authenticated_org():
    from brain.systems.cortex.events import list_cortex_events_after_for_principal_async

    session = MagicMock()
    session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    org_id = "00000000-0000-0000-0000-000000000001"

    await list_cortex_events_after_for_principal_async(
        session,
        {"id": "user-1", "org_id": org_id, "principal_type": "human"},
        last_event_id=4,
        limit=5,
    )

    stmt = session.scalars.call_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "JOIN ideas" in sql
    assert "cortex_events.id > 4" in sql
    assert f"ideas.org_id = '{org_id.replace('-', '')}'" in sql


@pytest.mark.asyncio
async def test_event_replay_caps_old_cursor_with_clear_response(monkeypatch):
    from brain.app.api.routers import ws as ws_router

    created_at = datetime(2026, 4, 23, tzinfo=timezone.utc)
    events = [
        SimpleNamespace(
            id=10,
            event_type="run.activity",
            run_id=42,
            root_run_id=42,
            idea_id="idea-1",
            sequence_no=1,
            created_at=created_at,
            payload={"label": "started"},
            _agent_run_thread_id="idea-1",
            _agent_run_profile="fast",
            _agent_run_org_id="org-1",
        ),
        SimpleNamespace(
            id=11,
            event_type="run.status_changed",
            run_id=42,
            root_run_id=42,
            idea_id="idea-1",
            sequence_no=2,
            created_at=created_at,
            payload={"to_status": "running"},
            _agent_run_thread_id="idea-1",
            _agent_run_profile="fast",
            _agent_run_org_id="org-1",
        ),
    ]

    def fake_load(channel, principal, *, last_event_id, limit):
        assert channel == "run"
        assert principal["org_id"] == "org-1"
        assert last_event_id == 0
        assert limit == 2
        return events

    monkeypatch.setattr(ws_router, "_load_replay_events", fake_load)
    fake_ws = AsyncMock()

    await ws_router._replay_durable_events(
        fake_ws,
        WsTokenClaims(
            user_id="user-1",
            org_id="org-1",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            session_id="session-user-1",
        ),
        {"run": 0},
        limit=1,
    )

    sent = [call.args[0] for call in fake_ws.send_json.await_args_list]
    assert sent[0]["type"] == "step_started"
    assert sent[0]["source_event_type"] == "run.activity"
    assert sent[0]["event_channel"] == "run"
    assert sent[0]["event_cursor"] == 10
    assert sent[0]["replayed"] is True
    assert sent[0]["idea_id"] == "idea-1"
    assert sent[1] == {
        "type": ServerEvent.EVENT_REPLAY_CAPPED,
        "code": "EVENT_REPLAY_CAPPED",
        "channel": "run",
        "from_cursor": 0,
        "last_event_id": 10,
        "limit": 1,
        "message": (
            "Replay was capped to protect the socket; reconnect with "
            "last_event_id or refresh state before requesting more."
        ),
    }
    assert sent[2] == {
        "type": ServerEvent.EVENT_REPLAY_COMPLETE,
        "channel": "run",
        "from_cursor": 0,
        "last_event_id": 10,
        "delivered": 1,
        "has_more": True,
        "limit": 1,
    }
