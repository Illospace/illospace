from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from brain.systems.meetings import client as meetbot_client
from brain.systems.runs.tool_catalog.handlers import meetings as meeting_handlers
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers.meetings import (
    _handle_join_meeting,
    _handle_leave_meeting,
    _handle_meeting_status,
    _handle_send_meeting_chat,
)


class _HTTPClient:
    def __init__(
        self,
        responses: list[httpx.Response],
        persistence_state: dict[str, Any],
    ) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.persistence_state = persistence_state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs):
        if method == "POST" and url.endswith("/join"):
            assert self.persistence_state["attempted"] is True
            if not self.persistence_state["write_failed"]:
                assert self.persistence_state["committed"] is True
                assert len(self.persistence_state["rows"]) == 1
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


def _patch_http(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[httpx.Response],
    *,
    persistence_error: SQLAlchemyError | None = None,
) -> _HTTPClient:
    requested_session_id = str(responses[0].json().get("session_id") or "session-requested")
    persistence_state: dict[str, Any] = {
        "attempted": False,
        "committed": False,
        "rows": [],
        "write_failed": persistence_error is not None,
    }

    class _UnitOfWork:
        session = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, *_exc) -> None:
            persistence_state["committed"] = exc_type is None

    async def create_requested(_session, **kwargs) -> None:
        persistence_state["attempted"] = True
        if persistence_error is not None:
            raise persistence_error
        persistence_state["rows"].append(kwargs)

    monkeypatch.setattr(meeting_handlers, "UnitOfWork", _UnitOfWork)
    monkeypatch.setattr(
        meeting_handlers,
        "create_requested_meetbot_session",
        create_requested,
    )
    monkeypatch.setattr(meeting_handlers, "uuid4", lambda: requested_session_id)
    client = _HTTPClient(responses, persistence_state)
    monkeypatch.setattr(meetbot_client, "async_http_client", lambda **_kwargs: client)
    monkeypatch.setenv("ILLO_MEETBOT_URL", "http://meetbot:8010/")
    monkeypatch.setenv("ILLO_MEETBOT_TOKEN", "shared-secret")
    return client


@pytest.mark.asyncio
async def test_join_continues_when_request_record_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    http = _patch_http(
        monkeypatch,
        [
            httpx.Response(
                202,
                json={"session_id": "session-write-failed", "status": "captions_flowing"},
            )
        ],
        persistence_error=SQLAlchemyError("database unavailable"),
    )

    result = json.loads(
        await _handle_join_meeting("https://meet.google.com/abc-defg-hij")
    )

    assert result["ok"] is True
    assert result["session_id"] == "session-write-failed"
    assert http.persistence_state["committed"] is False
    assert http.requests[0]["url"] == "http://meetbot:8010/join"
    assert "could not be recorded; continuing to join" in caplog.text


@pytest.mark.asyncio
async def test_join_meeting_passes_slack_origin_and_reports_captions_flowing(monkeypatch):
    http = _patch_http(
        monkeypatch,
        [
            httpx.Response(202, json={"session_id": "session-1", "status": "starting"}),
            httpx.Response(
                200,
                json={
                    "session_id": "session-1",
                    "status": "captions_flowing",
                    "caption_lines": 3,
                },
            ),
        ],
    )
    run = SimpleNamespace(
        id=42,
        metadata_={
            "slack_trigger": {
                "channel_id": "C-source",
                "thread_ts": "100.1",
                "slack_user_id": "U-requester",
                "response_target": {
                    "channel_id": "C-follow-up",
                    "thread_ts": "100.2",
                },
            }
        },
        target_ref={},
    )

    with bind_agent_context({"run": run}):
        result = json.loads(
            await _handle_join_meeting(
                "https://meet.google.com/abc-defg-hij",
                display_name="Illo test notetaker",
            )
        )

    assert result == {
        "ok": True,
        "session_id": "session-1",
        "status": "captions_flowing",
        "message": "Meetbot is admitted and live captions are flowing.",
        "caption_lines": 3,
    }
    assert http.requests[0] == {
        "method": "POST",
        "url": "http://meetbot:8010/join",
        "headers": {"X-Meetbot-Token": "shared-secret"},
        "json": {
            "session_id": "session-1",
            "meeting_url": "https://meet.google.com/abc-defg-hij",
            "origin": {"channel": "C-follow-up", "thread_ts": "100.2"},
            "display_name": "Illo test notetaker",
            "requested_by": "U-requester",
        },
    }
    assert http.requests[1]["method"] == "GET"
    assert http.requests[1]["url"] == "http://meetbot:8010/sessions/session-1"
    assert http.persistence_state["rows"] == [
        {
            "session_id": "session-1",
            "meeting_url": "https://meet.google.com/abc-defg-hij",
            "requesting_run_id": 42,
        }
    ]


@pytest.mark.asyncio
async def test_join_meeting_without_slack_context_sends_empty_origin(monkeypatch):
    http = _patch_http(
        monkeypatch,
        [
            httpx.Response(
                202,
                json={"session_id": "session-cycle", "status": "captions_flowing"},
            )
        ],
    )

    with bind_agent_context({"execution_metadata": {"origin": "cycle"}}):
        result = json.loads(
            await _handle_join_meeting("https://meet.google.com/abc-defg-hij")
        )

    assert result["status"] == "captions_flowing"
    assert http.requests[0]["json"]["origin"] == {}
    assert "requested_by" not in http.requests[0]["json"]


@pytest.mark.asyncio
async def test_join_meeting_reads_slack_origin_from_projected_run_metadata(monkeypatch):
    http = _patch_http(
        monkeypatch,
        [
            httpx.Response(
                202,
                json={"session_id": "session-projected", "status": "captions_flowing"},
            )
        ],
    )
    execution_metadata = {
        "slack_trigger": {
            "slack_user_id": "U-requester",
            "response_target": {
                "channel_id": "C-projected",
                "thread_ts": "100.3",
            },
        }
    }

    with bind_agent_context({"execution_metadata": execution_metadata}):
        await _handle_join_meeting("https://meet.google.com/abc-defg-hij")

    assert http.requests[0]["json"]["origin"] == {
        "channel": "C-projected",
        "thread_ts": "100.3",
    }
    assert http.requests[0]["json"]["requested_by"] == "U-requester"


@pytest.mark.asyncio
async def test_meeting_tools_return_actionable_error_when_not_configured(monkeypatch):
    monkeypatch.delenv("ILLO_MEETBOT_URL", raising=False)
    monkeypatch.delenv("ILLO_MEETBOT_TOKEN", raising=False)

    result = json.loads(await _handle_meeting_status("session-1"))

    assert result["ok"] is False
    assert "meetbot service is not configured" in result["error"]
    assert "ILLO_MEETBOT_URL" in result["error"]


@pytest.mark.asyncio
async def test_join_meeting_surfaces_active_session_conflict(monkeypatch):
    _patch_http(
        monkeypatch,
        [httpx.Response(409, json={"active_session_id": "session-active"})],
    )

    result = json.loads(
        await _handle_join_meeting("https://meet.google.com/abc-defg-hij")
    )

    assert result["ok"] is False
    assert result["status_code"] == 409
    assert result["active_session_id"] == "session-active"
    assert "already in session session-active" in result["error"]


@pytest.mark.asyncio
async def test_status_leave_and_chat_use_meetbot_contract(monkeypatch):
    http = _patch_http(
        monkeypatch,
        [
            httpx.Response(
                200,
                json={
                    "session_id": "session-2",
                    "status": "admitted",
                    "warning": "No captions observed yet",
                },
            ),
            httpx.Response(202, json={"status": "ended"}),
            httpx.Response(202, json={"status": "accepted"}),
        ],
    )

    status = json.loads(await _handle_meeting_status("session-2"))
    leave = json.loads(await _handle_leave_meeting("session-2"))
    chat = json.loads(await _handle_send_meeting_chat("session-2", "I am taking notes."))

    assert status["status"] == "admitted"
    assert "no caption flow" in status["message"]
    assert "No captions observed yet" in status["message"]
    assert leave["status"] == "ended"
    assert chat["status"] == "accepted"
    assert [(item["method"], item["url"]) for item in http.requests] == [
        ("GET", "http://meetbot:8010/sessions/session-2"),
        ("POST", "http://meetbot:8010/sessions/session-2/leave"),
        ("POST", "http://meetbot:8010/sessions/session-2/chat"),
    ]
    assert http.requests[2]["json"] == {"text": "I am taking notes."}


def test_meeting_tools_are_registered_and_exposed():
    from brain.systems.runs.tool_catalog.registry import get_tool_registration
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    names = {"join_meeting", "meeting_status", "leave_meeting", "send_meeting_chat"}
    assert names <= {tool["name"] for tool in COORDINATOR_TOOLS}
    assert names <= {tool["name"] for tool in WORKER_TOOLS}
    assert names <= set(_get_tool_handlers())
    assert all(get_tool_registration(name) is not None for name in names)
