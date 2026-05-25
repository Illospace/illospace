"""Tests for the canonical AgentRun event stream."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.systems.runs.event_log import (
    async_record_run_event,
    async_run_event_backbone_status,
    list_run_events_after_for_principal_async,
    run_event_to_message,
)
from brain.systems.runs.events import async_record_tool_call
from brain.systems.cortex.events import run_event_scope
from brain.platform.db.models.run import RunEvent


class _AsyncUoW:
    def __init__(self, rows):
        self.session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_record_run_event_allocates_next_sequence_and_normalizes_payload():
    session = MagicMock()
    session.get = AsyncMock(return_value=SimpleNamespace(id=42, root_run_id=42))
    session.get_bind.return_value.dialect.name = "sqlite"
    session.scalar = AsyncMock(return_value=4)
    session.flush = AsyncMock()

    event = await async_record_run_event(
        42,
        "run.activity",
        {"label": "Reading README", "nested": {"value": 1}},
        producer="fast",
        session=session,
    )

    assert isinstance(event, RunEvent)
    assert event.run_id == 42
    assert event.root_run_id == 42
    assert event.sequence_no == 5
    assert event.payload == {"label": "Reading README", "nested": {"value": 1}}
    session.add.assert_called_once()
    added = session.add.call_args.args[0]
    assert added.event_type == "run.activity"
    assert added.producer == "fast"
    session.flush.assert_awaited_once()


def test_run_event_projection_skips_headless_runs():
    from brain.systems.runs.ui_events import run_event_to_ui_message

    event = SimpleNamespace(
        id=10,
        run_id=42,
        root_run_id=42,
        sequence_no=1,
        event_type="run.activity",
        payload={"label": "Reporting blocker"},
    )
    run = SimpleNamespace(
        id=42,
        org_id="org-1",
        thread_id="headless-worker:1:abc",
        profile="fast",
        metadata_={"headless": True},
    )

    assert run_event_to_ui_message(event, run=run, org_id="org-1") is None


@pytest.mark.asyncio
async def test_async_record_tool_call_persists_redacted_tool_trace(monkeypatch):
    recorded = []

    async def _record(run_id, event_type, payload, **kwargs):
        recorded.append((run_id, event_type, payload, kwargs))

    monkeypatch.setattr("brain.systems.runs.event_log.async_record_run_event", _record)

    await async_record_tool_call(
        42,
        "idea-1",
        "brain_vault",
        {"key": "OPENAI_API_KEY"},
        '{"key":"OPENAI_API_KEY","value":"sk-secret"}',
        source="runner:test",
    )

    assert recorded == [
        (
            42,
            "run.tool_completed",
            {
                "idea_id": "idea-1",
                "tool_name": "brain_vault",
                "args": {"key": "OPENAI_API_KEY"},
                "result": "[secret redacted]",
                "source": "runner:test",
            },
            {"producer": "runner:test"},
        )
    ]


def test_run_event_to_message_handles_projected_rows_and_replay_flag():
    event = SimpleNamespace(
        id=7,
        run_id=42,
        root_run_id=None,
        sequence_no=3,
        event_type="run.text_delta",
        payload={"delta": "hello"},
        created_at=None,
        _agent_run_thread_id="idea-1",
        _agent_run_profile="fast",
        _agent_run_org_id="org-1",
    )

    message = run_event_to_message(event, replayed=True)

    assert message["type"] == "text_delta"
    assert message["source_event_type"] == "run.text_delta"
    assert message["run_id"] == 42
    assert message["root_run_id"] == 42
    assert message["sequence_no"] == 3
    assert message["event_cursor"] == 7
    assert message["replayed"] is True
    assert message["idea_id"] == "idea-1"
    assert message["profile"] == "fast"
    assert message["org_id"] == "org-1"
    assert message["delta"] == "hello"




def test_run_event_to_message_projects_non_streaming_final_text():
    event = SimpleNamespace(
        id=8,
        run_id=42,
        root_run_id=None,
        sequence_no=4,
        event_type="run.text_completed",
        payload={"text": "Hi! How can I help?"},
        created_at=None,
        _agent_run_thread_id="idea-1",
        _agent_run_profile="fast",
        _agent_run_org_id="org-1",
    )

    message = run_event_to_message(event)

    assert message["type"] == "text_delta"
    assert message["source_event_type"] == "run.text_completed"
    assert message["delta"] == "Hi! How can I help?"
    assert message["idea_id"] == "idea-1"
    assert message["profile"] == "fast"


def test_run_event_to_message_projects_public_tool_display_without_raw_script():
    event = SimpleNamespace(
        id=9,
        run_id=42,
        root_run_id=None,
        sequence_no=5,
        event_type="run.tool_started",
        payload={
            "tool_name": "run_script",
            "args": {
                "description": "Check GitHub token identity and visible repos.",
                "script": "TOKEN='ghp_abcdefghijklmnopqrstuvwxyz1234567890'",
            },
        },
        created_at=None,
        _agent_run_thread_id="idea-1",
        _agent_run_profile="fast",
        _agent_run_org_id="org-1",
    )

    message = run_event_to_message(event)

    assert message["type"] == "tool_started"
    assert message["args"] == {"description": "Check GitHub token identity and visible repos."}
    assert message["tool_display"]["icon"] == "🔧"
    assert message["tool_display"]["label"] == "Check GitHub token identity and visible repos"
    assert "ghp_" not in json.dumps(message)


def test_public_tool_projection_strips_sensitive_url_parts():
    from brain.systems.runs.presentation import public_tool_event_payload

    message = public_tool_event_payload(
        {
            "tool_name": "fetch_url",
            "args": {
                "url": "https://user:secret@example.com/private/file?token=abc123&X-Amz-Signature=sig#fragment",
            },
        },
        "run.tool_started",
    )

    serialized = json.dumps(message)
    assert message["args"]["url"] == "example.com/private/file"
    assert message["tool_display"]["target"] == "example.com/private/file"
    assert "secret" not in serialized
    assert "abc123" not in serialized
    assert "X-Amz-Signature" not in serialized


def test_publish_live_fans_out_without_durable_storage(monkeypatch):
    from brain.systems.cortex import events

    published = []

    monkeypatch.setattr(events, "_publisher", lambda event_type, data: published.append((event_type, data)))

    events.publish_live("browser_session_frame", {"idea_id": "idea-1", "delta": "hello"})

    assert published == [("browser_session_frame", {"idea_id": "idea-1", "delta": "hello"})]


@pytest.mark.asyncio
async def test_publish_records_cortex_event_async_inside_running_loop(monkeypatch):
    from brain.systems.cortex import events

    async_record = AsyncMock()
    publisher = MagicMock()

    monkeypatch.setattr(events, "record_cortex_event_async", async_record)
    monkeypatch.setattr(events, "_publisher", publisher)

    payload = {"idea_id": "idea-1", "new_status": "active"}
    events.publish("status_change", payload)
    await asyncio.sleep(0)

    async_record.assert_awaited_once_with("status_change", payload)
    publisher.assert_called_once_with("status_change", payload)


@pytest.mark.asyncio
async def test_browser_run_events_live_publish_after_durable_record(monkeypatch):
    import brain.systems.cortex.events as cortex_events

    durable_records = []
    live_events = []

    async def _record(*args, **kwargs):
        durable_records.append((args, kwargs))

    monkeypatch.setattr("brain.systems.runs.event_log.async_record_run_event", _record)
    monkeypatch.setattr(
        cortex_events,
        "_publisher",
        lambda event_type, payload: live_events.append((event_type, payload)),
    )

    payload = {
        "idea_id": "idea-1",
        "session_id": "session-1",
        "state": {"id": "session-1", "run_id": 42},
    }
    with run_event_scope(42, idea_id="idea-1", session=object()):
        cortex_events.publish("browser_session_state", payload)
    await asyncio.sleep(0)

    assert durable_records
    assert durable_records[0][0][:3] == (42, "browser_session_state", payload)
    assert live_events == [("browser_session_state", payload)]


@pytest.mark.asyncio
async def test_non_browser_run_events_skip_live_publish_after_durable_record(monkeypatch):
    import brain.systems.cortex.events as cortex_events

    async_record = AsyncMock()
    monkeypatch.setattr("brain.systems.runs.event_log.async_record_run_event", async_record)
    publisher = MagicMock()
    monkeypatch.setattr(cortex_events, "_publisher", publisher)

    with run_event_scope(42, idea_id="idea-1", session=object()):
        cortex_events.publish("run.activity", {"idea_id": "idea-1", "activity": "Working"})
    await asyncio.sleep(0)

    async_record.assert_awaited_once()
    publisher.assert_not_called()


@pytest.mark.asyncio
async def test_vault_secret_prompt_publishes_live_after_durable_record(monkeypatch):
    import brain.systems.cortex.events as cortex_events

    durable_records = []
    live_events = []

    async def _record(*args, **kwargs):
        durable_records.append((args, kwargs))

    monkeypatch.setattr("brain.systems.runs.event_log.async_record_run_event", _record)
    monkeypatch.setattr(
        cortex_events,
        "_publisher",
        lambda event_type, payload: live_events.append((event_type, payload)),
    )

    payload = {
        "idea_id": "idea-1",
        "run_id": 42,
        "prompt": {"id": "prompt-1", "idea_id": "idea-1", "key_name": "GITHUB_TOKEN"},
    }
    with run_event_scope(42, idea_id="idea-1", session=object()):
        cortex_events.publish("vault_secret_prompt", payload)
    await asyncio.sleep(0)

    assert durable_records
    assert live_events == [("vault_secret_prompt", payload)]


@pytest.mark.asyncio
async def test_run_replay_query_scopes_human_principal_to_authenticated_org():
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: []))

    await list_run_events_after_for_principal_async(
        session,
        {
            "id": "user-1",
            "org_id": "00000000-0000-0000-0000-000000000001",
            "principal_type": "human",
        },
        last_event_id=7,
        limit=5,
    )

    stmt = session.execute.await_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "JOIN agent_runs" in sql
    assert "agent_run_events.id > 7" in sql
    assert "agent_runs.org_id = '00000000000000000000000000000001'" in sql


@pytest.mark.asyncio
async def test_run_replay_query_allows_internal_service_permission_scope():
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(all=lambda: []))

    await list_run_events_after_for_principal_async(
        session,
        {
            "id": "service:worker",
            "principal_type": "service",
            "permissions": ["run:manage"],
        },
        last_event_id=7,
        limit=5,
    )

    stmt = session.execute.await_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "JOIN agent_runs" in sql
    assert "agent_run_events.id > 7" in sql
    assert "agent_runs.org_id =" not in sql


@pytest.mark.asyncio
async def test_run_event_backbone_status_reports_lag_and_health(monkeypatch):
    import brain.app.api.ws.run_events as run_events

    session = MagicMock()
    session.scalar = AsyncMock(return_value=11)
    monkeypatch.setattr(run_events, "_last_event_id", 8)

    status = await async_run_event_backbone_status(
        session,
        "api.websocket_fanout",
        consumer_running=True,
    )

    assert status["consumer_name"] == "api.websocket_fanout"
    assert status["consumer_running"] is True
    assert status["health"] == "lagging"
    assert status["lag"] == 3
    assert status["caught_up"] is False
    assert status["replay_safe"] is True


@pytest.mark.asyncio
async def test_fanout_run_events_once_broadcasts_run_events(monkeypatch):
    import brain.app.api.ws.run_events as run_events

    event = SimpleNamespace(
        id=10,
        run_id=42,
        root_run_id=42,
        sequence_no=1,
        event_type="run.activity",
        payload={"label": "Reading"},
    )
    run = SimpleNamespace(id=42, org_id="org-1", thread_id="idea-1", profile="fast")
    monkeypatch.setattr(run_events, "UnitOfWork", lambda: _AsyncUoW([(event, run)]))
    monkeypatch.setattr(run_events, "_last_event_id", 0)
    ws_manager = SimpleNamespace(broadcast_run_event=AsyncMock())

    delivered, had_error = await run_events.fanout_run_events_once(ws_manager)

    assert delivered == 1
    assert had_error is False
    ws_manager.broadcast_run_event.assert_awaited_once_with(
        "step_started",
        {
            "label": "Reading",
            "activity": "Reading",
            "source_event_type": "run.activity",
            "event_channel": "run",
            "event_cursor": 10,
            "run_event_id": 10,
            "run_id": 42,
            "root_run_id": 42,
            "sequence_no": 1,
            "event_id": 10,
            "org_id": "org-1",
            "thread_id": "idea-1",
            "idea_id": "idea-1",
            "profile": "fast",
            "execution_profile": "fast",
        },
        org_id="org-1",
    )
    assert run_events._last_event_id == 10


@pytest.mark.asyncio
async def test_fanout_run_events_once_skips_unscoped_run_events(monkeypatch):
    import brain.app.api.ws.run_events as run_events

    event = SimpleNamespace(
        id=11,
        run_id=43,
        root_run_id=43,
        sequence_no=1,
        event_type="run.activity",
        payload={"label": "Reading"},
    )
    run = SimpleNamespace(id=43, org_id=None, thread_id="idea-1", profile="fast")
    monkeypatch.setattr(run_events, "UnitOfWork", lambda: _AsyncUoW([(event, run)]))
    monkeypatch.setattr(run_events, "_last_event_id", 0)
    ws_manager = SimpleNamespace(broadcast_run_event=AsyncMock())

    delivered, had_error = await run_events.fanout_run_events_once(ws_manager)

    assert delivered == 1
    assert had_error is False
    ws_manager.broadcast_run_event.assert_not_awaited()
    assert run_events._last_event_id == 11
