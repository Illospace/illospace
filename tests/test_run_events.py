"""Tests for the canonical AgentRun event stream."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.systems.runs.event_log import (
    list_run_events_after_for_principal,
    record_run_event,
    run_event_backbone_status,
    run_event_to_message,
)
from brain.systems.cortex.events import run_event_scope
from brain.platform.db.models.run import RunEvent


def test_record_run_event_allocates_next_sequence_and_normalizes_payload():
    session = MagicMock()
    session.get.return_value = SimpleNamespace(id=42, root_run_id=42)
    session.get_bind.return_value.dialect.name = "sqlite"
    session.scalar.return_value = 4

    event = record_run_event(
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

def test_publish_live_fans_out_without_durable_storage(monkeypatch):
    from brain.systems.cortex import events

    published = []

    monkeypatch.setattr(events, "_publisher", lambda event_type, data: published.append((event_type, data)))
    monkeypatch.setattr(events, "record_cortex_event", MagicMock(side_effect=AssertionError("live events are not durable")))

    events.publish_live("browser_session_frame", {"idea_id": "idea-1", "delta": "hello"})

    assert published == [("browser_session_frame", {"idea_id": "idea-1", "delta": "hello"})]


def test_browser_run_events_live_publish_after_durable_record(monkeypatch):
    import brain.systems.cortex.events as cortex_events

    durable_records = []
    live_events = []

    monkeypatch.setattr(
        "brain.systems.runs.event_log.record_run_event",
        lambda *args, **kwargs: durable_records.append((args, kwargs)),
    )
    monkeypatch.setattr(
        cortex_events,
        "record_cortex_event",
        MagicMock(side_effect=AssertionError("run event should not duplicate into cortex_events")),
    )
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

    assert durable_records
    assert durable_records[0][0][:3] == (42, "browser_session_state", payload)
    assert live_events == [("browser_session_state", payload)]


def test_non_browser_run_events_skip_live_publish_after_durable_record(monkeypatch):
    import brain.systems.cortex.events as cortex_events

    monkeypatch.setattr("brain.systems.runs.event_log.record_run_event", lambda *args, **kwargs: None)
    publisher = MagicMock()
    monkeypatch.setattr(cortex_events, "_publisher", publisher)

    with run_event_scope(42, idea_id="idea-1", session=object()):
        cortex_events.publish("run.activity", {"idea_id": "idea-1", "activity": "Working"})

    publisher.assert_not_called()


def test_run_replay_query_scopes_human_principal_to_authenticated_org():
    session = MagicMock()
    session.execute.return_value.all.return_value = []

    list_run_events_after_for_principal(
        session,
        {
            "id": "user-1",
            "org_id": "00000000-0000-0000-0000-000000000001",
            "principal_type": "human",
        },
        last_event_id=7,
        limit=5,
    )

    stmt = session.execute.call_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "JOIN agent_runs" in sql
    assert "agent_run_events.id > 7" in sql
    assert "agent_runs.org_id = '00000000000000000000000000000001'" in sql


def test_run_replay_query_allows_internal_service_permission_scope():
    session = MagicMock()
    session.execute.return_value.all.return_value = []

    list_run_events_after_for_principal(
        session,
        {
            "id": "service:worker",
            "principal_type": "service",
            "permissions": ["run:manage"],
        },
        last_event_id=7,
        limit=5,
    )

    stmt = session.execute.call_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "JOIN agent_runs" in sql
    assert "agent_run_events.id > 7" in sql
    assert "agent_runs.org_id =" not in sql


def test_run_event_backbone_status_reports_lag_and_health(monkeypatch):
    import brain.app.api.ws.run_events as run_events

    session = MagicMock()
    session.scalar.return_value = 11
    monkeypatch.setattr(run_events, "_last_event_id", 8)

    status = run_event_backbone_status(
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
    mock_uow = MagicMock()
    mock_uow.__enter__.return_value = mock_uow
    mock_uow.__exit__.return_value = False
    mock_uow.session.execute.return_value.all.return_value = [(event, run)]
    monkeypatch.setattr(run_events, "UnitOfWork", MagicMock(return_value=mock_uow))
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
    mock_uow = MagicMock()
    mock_uow.__enter__.return_value = mock_uow
    mock_uow.__exit__.return_value = False
    mock_uow.session.execute.return_value.all.return_value = [(event, run)]
    monkeypatch.setattr(run_events, "UnitOfWork", MagicMock(return_value=mock_uow))
    monkeypatch.setattr(run_events, "_last_event_id", 0)
    ws_manager = SimpleNamespace(broadcast_run_event=AsyncMock())

    delivered, had_error = await run_events.fanout_run_events_once(ws_manager)

    assert delivered == 1
    assert had_error is False
    ws_manager.broadcast_run_event.assert_not_awaited()
    assert run_events._last_event_id == 11
