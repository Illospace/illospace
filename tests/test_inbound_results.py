"""Inbound submission result visibility tests."""

from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brain.app.api.routers.agent_mcp import MCP_TOOLS, _tool_get_result
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.systems.inbound import results as inbound_results
from brain.systems.inbound.results import (
    InboundSubmissionResultState,
    project_inbound_submission_result,
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


@pytest.fixture
def submission_response(monkeypatch):
    """Exercise the real builder and serializers with no database or network."""
    answer = "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(141))[:9000]
    timestamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    refs = ["memory_node:918"]
    contract = {
        "required": True,
        "status": "satisfied",
        "mutated_target_refs": refs,
        "reason": "Durable evidence recorded. " * 80,
    }
    attribution = {"tags": ["knowledge_preserved"], "mutated_target_refs": refs}
    handling = {
        "status": "completed",
        "run_id": 918,
        "run_status": "completed",
        "completed_at": timestamp.isoformat(),
        "reconciled_at": timestamp.isoformat(),
        "final_answer": answer,
        "result": {"status": "completed", "final_answer": answer},
        "evidence_contract": contract,
        "attribution": attribution,
    }
    event = InboundEventRow(
        id="evt-918", org_id=_ORG, connection_id=_CONN,
        kind="agent_signal", origin="codex", status="processed",
        action_result={"handling": handling}, created_at=timestamp,
        raw_payload={"message": "Preserve context " * 500},
        normalized_payload={"message": "Preserve context " * 500},
    )
    receipt = InboundDecisionReceiptRow(
        id="receipt-918", event_id=event.id, org_id=_ORG, connection_id=_CONN,
        status="processed", outcome={"handling": handling},
        tool_use={"type": "illo_submit", **handling}, created_at=timestamp,
    )
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(status="completed")
    monkeypatch.setattr(inbound_results.inbound_admin, "require_event_for_org", AsyncMock(return_value=event))
    monkeypatch.setattr(inbound_results.inbound_admin, "list_receipts", AsyncMock(return_value=[receipt]))
    monkeypatch.setattr(inbound_results, "reconcile_inbound_triage_run", AsyncMock(return_value=None))
    monkeypatch.setattr(inbound_results, "read_run_failure_diagnostic", AsyncMock(return_value=None))
    principal = SimpleNamespace(org_id=_ORG, connection_id=_CONN)

    async def response(**arguments):
        return await _tool_get_result(db, principal, {"event_id": event.id, "limit": 1, **arguments})

    async def internal():
        return (await read_inbound_submission_result(
            db, org_id=_ORG, connection_id=_CONN, event_id=event.id,
            include_payload=False, limit=1,
        )).payload

    return SimpleNamespace(
        answer=answer, handling=handling, event=event, receipt=receipt,
        response=response, internal=internal,
    )


async def test_completed_mcp_result_contains_long_answer_once(submission_response):
    fixture = submission_response
    response = await fixture.response(include_payload=False)
    assert len(fixture.answer) == 9000
    assert json.dumps(response).count(fixture.answer[400:460]) == 1
    assert response["final_answer"] == fixture.answer
    assert json.dumps(response).count('"evidence_contract":') == 1
    assert json.dumps(response).count('"attribution":') == 1


async def test_completed_mcp_result_size_bound(submission_response):
    fixture = submission_response
    before = len(json.dumps(await fixture.internal()))
    after = len(json.dumps(await fixture.response(include_payload=False)))
    # Measured 13,367 chars: 9,000 answer + 4,367 metadata. Reserve 5 KB overhead.
    bound = 2 * len(fixture.answer) + 5000
    assert before > bound
    assert after < bound
    print(f"fixture non-compact: before={before}, after={after}")


async def test_compact_running_mcp_result_is_small(submission_response):
    fixture = submission_response
    fixture.handling.update(status="running", run_status="running")
    for key in ("completed_at", "reconciled_at", "final_answer", "result"):
        fixture.handling.pop(key)
    fixture.handling["evidence_contract"]["status"] = "pending"
    response = await fixture.response(compact=True)
    assert len(json.dumps(response)) < 2000
    assert "final_answer" not in response
    assert response["run_status"] == "running"
    assert response["evidence_status"] == "pending"
    assert set(response) == {
        "event_id", "run_id", "run_status", "handling_status", "evidence_status",
        "completed_at", "reconciled_at", "mutated_target_refs", "attribution",
    }
    assert set(response["attribution"]) == {"tags"}
    print(f"fixture compact running: {len(json.dumps(response))}")


async def test_compact_completed_mcp_result_has_answer_and_evidence(submission_response):
    fixture = submission_response
    response = await fixture.response(compact=True, include_payload=True)
    assert response["final_answer"] == fixture.answer
    assert json.dumps(response).count(fixture.answer[400:460]) == 1
    assert response["evidence_status"] == "satisfied"
    assert response["mutated_target_refs"] == ["memory_node:918"]
    assert response["attribution"] == {"tags": ["knowledge_preserved"]}
    assert "event" not in response
    assert "evidence_contract" not in response


async def test_noncompact_mcp_result_preserves_external_contract(submission_response):
    fixture = submission_response
    response = await fixture.response(include_payload=False)
    assert response["run_status"] == "completed"
    assert response["handling_status"] == "completed"
    assert response["run_id"] == 918
    assert response["evidence_status"] == "satisfied"
    assert response["evidence_contract"]["status"] == "satisfied"
    assert response["evidence_contract"]["mutated_target_refs"] == ["memory_node:918"]
    assert response["mutated_target_refs"] == ["memory_node:918"]
    assert response["attribution"]["tags"] == ["knowledge_preserved"]
    assert response["final_answer"] == fixture.answer
    for key in ("completed_at", "reconciled_at"):
        assert response[key] == fixture.handling[key]
        assert response["event"]["action_result"]["handling"][key] == fixture.handling[key]
        assert response["latest_receipt"]["outcome"]["handling"][key] == fixture.handling[key]


async def test_mcp_limit_one_returns_only_one_receipt(submission_response):
    response = await submission_response.response(include_payload=False)
    assert response["latest_receipt"]["id"] == "receipt-918"
    assert response["receipts"] == []
    assert json.dumps(response).count('"id": "receipt-918"') == 1


async def test_projection_keeps_internal_shape_and_older_receipts(submission_response):
    internal = await submission_response.internal()
    older = deepcopy(internal["latest_receipt"])
    older["id"] = "older-receipt"
    internal["receipts"].append(older)
    original = deepcopy(internal)
    response = project_inbound_submission_result(internal)
    assert internal == original
    assert internal["latest_receipt"] == internal["receipts"][0]
    assert [receipt["id"] for receipt in response["receipts"]] == ["older-receipt"]
    assert json.dumps(response).count(submission_response.answer[400:460]) == 1


async def test_mcp_result_defaults_keep_event_payload(submission_response):
    response = await submission_response.response()
    assert response["event"]["raw_payload"] == submission_response.event.raw_payload
    without_payload = await submission_response.response(include_payload=False)
    assert "raw_payload" not in without_payload["event"]


def test_mcp_result_schema_documents_projection_and_poll_defaults():
    schema = MCP_TOOLS["illo_get_result"]
    assert schema["inputSchema"]["properties"]["compact"]["default"] is False
    assert schema["inputSchema"]["properties"]["include_payload"]["default"] is True
    assert "final_answer" in schema["description"]
    assert "compact: true" in schema["description"]
