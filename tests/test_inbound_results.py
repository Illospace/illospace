"""Inbound submission result visibility tests."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from brain.app.api.routers.agent_mcp import _tool_get_result
from brain.systems.inbound.results import (
    InboundSubmissionResultState,
    read_inbound_submission_result,
)
from tests.test_inbound_reconciliation import _CONN, _ORG, _seed_slack_lane, session


async def test_submission_result_absent_event_has_not_found_state(session):
    result = await read_inbound_submission_result(
        session,
        org_id=_ORG,
        connection_id=_CONN,
        event_id=str(uuid.uuid4()),
    )

    assert result.state is InboundSubmissionResultState.NOT_FOUND
    assert result.payload is None


async def test_submission_result_cross_org_event_has_not_found_state(session):
    event_id, _ = await _seed_slack_lane(session, tool_results=[])

    result = await read_inbound_submission_result(
        session,
        org_id=str(uuid.uuid4()),
        connection_id=_CONN,
        event_id=event_id,
    )

    assert result.state is InboundSubmissionResultState.NOT_FOUND
    assert result.payload is None


async def test_submission_result_owned_by_another_connection_has_distinct_state(session):
    event_id, _ = await _seed_slack_lane(session, tool_results=[])
    other_connection_id = str(uuid.uuid4())

    result = await read_inbound_submission_result(
        session,
        org_id=_ORG,
        connection_id=other_connection_id,
        event_id=event_id,
    )

    assert result.state is InboundSubmissionResultState.NOT_VISIBLE_TO_CONNECTION
    assert result.payload is None

    wire_payload = await _tool_get_result(
        session,
        SimpleNamespace(org_id=_ORG, connection_id=other_connection_id),
        {"event_id": event_id},
    )
    assert wire_payload == {
        "event_id": event_id,
        "state": InboundSubmissionResultState.NOT_VISIBLE_TO_CONNECTION.value,
        "owned_by_another_connection": True,
    }


async def test_submission_result_visible_event_has_found_state(session):
    event_id, _ = await _seed_slack_lane(session, tool_results=[])

    result = await read_inbound_submission_result(
        session,
        org_id=_ORG,
        connection_id=_CONN,
        event_id=event_id,
    )

    assert result.state is InboundSubmissionResultState.FOUND
    assert result.payload is not None
    assert result.payload["event_id"] == event_id
