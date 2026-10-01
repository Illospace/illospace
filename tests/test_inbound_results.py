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
from brain.systems.runs.failures import public_run_failure
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
    attribution = {
        "tags": ["knowledge_preserved"],
        "summary": "Stored the requested knowledge.",
        "tool_names": ["memory.write"],
        "target_refs": refs,
        "run_event_ids": ["run-event-918"],
        "mutated_target_refs": refs,
    }
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
        tool_use={"type": "illo_submit", "attribution": attribution, "evidence_contract": contract},
        created_at=timestamp,
    )
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(status="completed")
    monkeypatch.setattr(inbound_results.inbound_admin, "require_event_for_org", AsyncMock(return_value=event))
    receipts = [receipt]
    monkeypatch.setattr(inbound_results.inbound_admin, "list_receipts", AsyncMock(return_value=receipts))
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
        response=response, internal=internal, receipts=receipts, db=db,
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
    # Keep the original bound meaningful against the real serialized shape.
    bound = 2 * len(fixture.answer) + 5000
    assert before > bound
    assert after < bound
    print(f"fixture non-compact: before={before}, after={after}")


async def test_compact_running_mcp_result_is_small(submission_response):
    fixture = submission_response
    fixture.handling.update(status="running", run_status="running")
    # Even a stale stored answer must not make a running poll large.
    for key in ("completed_at", "reconciled_at"):
        fixture.handling.pop(key)
    fixture.handling["evidence_contract"]["status"] = "pending"
    response = await fixture.response(compact=True)
    assert len(json.dumps(response)) < 2000
    assert "final_answer" not in response
    assert response["run_status"] == "running"
    assert response["terminal"] is False
    assert response["evidence_status"] == "pending"
    assert set(response) == {
        "event_id", "run_id", "run_status", "handling_status", "evidence_status",
        "completed_at", "reconciled_at", "mutated_target_refs", "attribution", "terminal",
    }
    assert set(response["attribution"]) == {"tags"}
    print(f"fixture compact running: {len(json.dumps(response))}")


async def test_compact_completed_mcp_result_has_answer_and_evidence(submission_response):
    fixture = submission_response
    response = await fixture.response(compact=True, include_payload=True)
    assert response["final_answer"] == fixture.answer
    assert response["terminal"] is True
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


async def test_projection_preserves_distinct_older_receipt(submission_response):
    internal = await submission_response.internal()
    older = deepcopy(internal["latest_receipt"])
    older["id"] = "older-receipt"
    older_handling = older["outcome"]["handling"]
    older_handling["final_answer"] = "A different earlier answer."
    older_handling["result"]["final_answer"] = older_handling["final_answer"]
    older_handling["attribution"] = {"tags": ["earlier_run"]}
    older_handling["evidence_contract"] = {"status": "missing"}
    older["tool_use"].update(
        attribution=older_handling["attribution"], evidence_contract=older_handling["evidence_contract"],
    )
    internal["receipts"].append(older)
    original = deepcopy(internal)
    response = project_inbound_submission_result(internal)
    assert internal == original
    assert internal["latest_receipt"] == internal["receipts"][0]
    assert response["receipts"] == [older]
    assert response["final_answer"] == submission_response.answer


async def test_projection_preserves_unrelated_nested_keys(submission_response):
    internal = await submission_response.internal()
    unrelated = {"attribution": {"owner": "another tool"}, "final_answer": "Tool answer", "evidence_contract": {"status": "other"}}
    internal["latest_receipt"]["outcome"]["preservation"] = deepcopy(unrelated)
    internal["event"]["action_result"]["handling"]["result"]["tool_result"] = deepcopy(unrelated)
    response = project_inbound_submission_result(internal)
    assert response["latest_receipt"]["outcome"]["preservation"] == unrelated
    assert response["event"]["action_result"]["handling"]["result"]["tool_result"] == unrelated


async def test_projection_keeps_first_receipt_when_id_differs(submission_response):
    internal = await submission_response.internal()
    internal["receipts"][0] = {**internal["receipts"][0], "id": "different-receipt"}
    response = project_inbound_submission_result(internal)
    assert response["receipts"] == internal["receipts"]


async def test_projection_keeps_distinct_data_in_same_id_receipt(submission_response):
    internal = await submission_response.internal()
    internal["receipts"][0] = deepcopy(internal["receipts"][0])
    internal["receipts"][0]["outcome"]["handling"]["final_answer"] = "Distinct answer"
    response = project_inbound_submission_result(internal)
    assert response["receipts"][0]["outcome"]["handling"]["final_answer"] == "Distinct answer"
    assert "final_answer" not in response["receipts"][0]["outcome"]["handling"]["result"]


async def test_triage_only_uses_one_current_block(submission_response):
    fixture = submission_response
    fixture.event.action_result = {"triage": fixture.handling}
    fixture.receipt.outcome = {"triage": deepcopy(fixture.handling)}
    fixture.receipt.outcome["triage"]["result"]["final_answer"] = "Different nested answer"
    fixture.receipt.outcome["triage"]["evidence_contract"] = {"status": "missing"}
    fixture.receipt.tool_use["attribution"] = {"tags": ["different"]}
    internal = await fixture.internal()
    assert internal["run_status"] == "completed"
    assert internal["evidence_contract"] == fixture.handling["evidence_contract"]
    fixture.db.get.assert_awaited_with(inbound_results.AgentRunRow, 918)
    response = await fixture.response(include_payload=False)
    for key in ("final_answer", "evidence_contract", "attribution", "completed_at", "reconciled_at"):
        assert response[key] == fixture.handling[key]
    assert response["mutated_target_refs"] == ["memory_node:918"]
    assert "final_answer" not in response["event"]["action_result"]["triage"]
    assert "evidence_contract" not in response["event"]["action_result"]["triage"]
    assert response["latest_receipt"]["outcome"]["triage"]["evidence_contract"] == {"status": "missing"}
    assert response["latest_receipt"]["outcome"]["triage"]["result"]["final_answer"] == "Different nested answer"
    assert response["latest_receipt"]["tool_use"]["attribution"] == {"tags": ["different"]}
    compact = await fixture.response(compact=True)
    assert compact["terminal"] is True
    assert compact["final_answer"] == fixture.answer


async def test_current_handling_takes_precedence_over_triage_and_receipt(submission_response):
    fixture = submission_response
    fixture.event.action_result["triage"] = {
        "run_id": 123, "run_status": "running", "final_answer": "Earlier triage answer",
        "attribution": {"tags": ["triage"]}, "evidence_contract": {"status": "pending"},
    }
    fixture.receipt.outcome = {"handling": deepcopy(fixture.event.action_result["triage"])}
    response = await fixture.response(include_payload=False)
    assert response["run_id"] == 918
    assert response["run_status"] == "completed"
    assert response["final_answer"] == fixture.answer
    assert response["attribution"] == fixture.handling["attribution"]
    assert response["evidence_contract"] == fixture.handling["evidence_contract"]
    assert response["event"]["action_result"]["triage"] == fixture.event.action_result["triage"]
    assert response["latest_receipt"]["outcome"] == fixture.receipt.outcome


@pytest.mark.parametrize("shape", ["result_text", "triage_text", "attribution_list", "latest_none", "receipts_empty", "no_handling"])
async def test_projection_tolerates_builder_shapes(submission_response, shape):
    fixture = submission_response
    if shape == "result_text":
        fixture.handling["result"] = "text result"
    elif shape == "triage_text":
        fixture.event.action_result = {"triage": "queued"}
    elif shape == "attribution_list":
        fixture.handling["attribution"] = ["tag"]
    elif shape in {"latest_none", "receipts_empty"}:
        fixture.receipts.clear()
    elif shape == "no_handling":
        fixture.event.action_result = {}
    internal = await fixture.internal()
    original = deepcopy(internal)
    for compact in (False, True):
        response = project_inbound_submission_result(internal, compact=compact)
        assert {"run_status", "handling_status", "evidence_status"} <= response.keys()
        if shape == "attribution_list":
            assert response["attribution"] == ["tag"]
    response = project_inbound_submission_result(internal)
    assert internal == original
    if shape == "result_text":
        assert response["event"]["action_result"]["handling"]["result"] == "text result"
    elif shape == "triage_text":
        assert response["event"]["action_result"]["triage"] == "queued"
    elif shape == "attribution_list":
        assert response["event"]["action_result"]["handling"]["attribution"] == ["tag"]
    elif shape == "latest_none":
        assert response["latest_receipt"] is None
    elif shape == "receipts_empty":
        assert response["receipts"] == []
    elif shape == "no_handling":
        assert response["event"]["action_result"] == {}
        assert response["latest_receipt"]["outcome"] == internal["latest_receipt"]["outcome"]


async def test_projection_does_not_remove_values_for_empty_top_level_copies(submission_response):
    fixture = submission_response
    fixture.event.action_result = {"handling": {"final_answer": "", "attribution": {}, "evidence_contract": {}}}
    internal = await fixture.internal()
    response = project_inbound_submission_result(internal)
    assert response["event"] == internal["event"]
    assert response["latest_receipt"] == internal["latest_receipt"]


@pytest.mark.parametrize("status", ["failed", "canceled", "expired"])
async def test_compact_terminal_failure_keeps_public_reason(submission_response, status):
    fixture = submission_response
    fixture.handling.update(status=status, run_status=status, failure={"category": "upstream", "message": "private diagnostic"})
    fixture.handling["evidence_contract"]["status"] = "missing"
    internal = await fixture.internal()
    response = await fixture.response(compact=True)
    assert response["terminal"] is True
    assert response["failure"] == internal["failure"] == public_run_failure(status, "upstream")
    assert "private diagnostic" not in json.dumps(response)
    assert "final_answer" not in response  # The public serializer removes unsafe failed answers.
    if status == "failed":
        print(f"fixture compact failed: {len(json.dumps(response))}; JSON={json.dumps(response)}")


@pytest.mark.parametrize("status", [" COMPLETED ", " FAILED ", " CANCELED ", " EXPIRED "])
async def test_compact_keeps_available_answer_for_every_terminal_status(submission_response, status):
    internal = await submission_response.internal()
    internal["run_status"] = status
    response = project_inbound_submission_result(internal, compact=True)
    assert response["terminal"] is True
    assert response["final_answer"] == submission_response.answer


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
    assert "terminal" in schema["description"]
    assert "failure (category and message)" in schema["description"]
