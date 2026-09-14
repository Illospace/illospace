from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.systems.inbound import service as inbound
from brain.systems.runs.events import run_event
from brain.systems.runs.failure_diagnostic import RunFailureStage, run_tool_execution_started
from brain.systems.runs.failures import RunFailureCategory
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AsyncAgentRunStore
from tests.inbound_preservation_support import (
    _assert_queued_submission,
    _seed_connection,
    session,
)


pytestmark = pytest.mark.asyncio


async def _seed_preservation_setup_failure(session):
    principal = await _seed_connection(session)
    envelope = {
        "kind": "submission",
        "origin": "codex.memory",
        "desired_outcome": "preserve_knowledge",
        "message": "Preserve the completed investigation and its reusable findings.",
        "source": {"source_tool": "codex", "repo": "Illospace/illospace"},
        "idempotency_key": "codex:preservation:setup-retry",
    }
    result = await inbound.submit_inbound_envelope(
        session, connection=principal, envelope=envelope,
    )
    handling = await _assert_queued_submission(session, result["ilo_outcome"])
    run_id = handling["run_id"]
    store = AsyncAgentRunStore(session)
    await store.set_status(run_id, RunStatus.STARTING)
    await store.set_status(run_id, RunStatus.RUNNING)
    await store.fail_run(
        run_id,
        category=RunFailureCategory.PRESERVATION_SETUP,
        stage=RunFailureStage.AGENT_EXECUTION,
        reason="Preservation setup failed before tool execution",
    )
    event = await session.get(InboundEventRow, result["event_id"])
    run = await session.get(AgentRunRow, run_id)
    assert event.kind == "submission"
    assert event.status == "failed"
    assert event.action_result["handling"]["run_id"] == run.id
    assert event.action_result["handling"]["run_status"] == "failed"
    assert event.action_result["handling"]["failure"]["category"] == "preservation_setup"
    assert event.action_result["handling"]["attribution"]["mutated_target_refs"] == []
    assert run.status == "failed"
    assert run.org_id == event.org_id
    assert run.metadata_["failure"]["category"] == "preservation_setup"
    assert not await run_tool_execution_started(session, run_id=run.id)
    return principal, envelope, event, run


async def _assert_preservation_failure_replayed(session, principal, envelope, event, run):
    await session.flush()
    stored_outcome = deepcopy(event.action_result)
    stored_status, stored_error = event.status, event.error
    receipt_ids = list((await session.scalars(select(InboundDecisionReceiptRow.id))).all())

    replay = await inbound.submit_inbound_envelope(
        session, connection=principal, envelope=envelope,
    )

    assert replay["idempotent_replay"] is True
    assert replay["replay_body_matches"] is True
    assert replay["event_id"] == str(event.id)
    assert replay["status"] == stored_status
    assert replay["error"] == stored_error
    assert replay["ilo_outcome"] == stored_outcome
    assert list((await session.scalars(select(AgentRunRow.id))).all()) == [run.id]
    assert list((await session.scalars(select(InboundDecisionReceiptRow.id))).all()) == receipt_ids
    await session.refresh(event)
    assert event.action_result == stored_outcome
    assert event.status == stored_status
    assert event.error == stored_error


@pytest.mark.parametrize("replay_site", ["initial_lookup", "insert_conflict"])
async def test_preservation_setup_same_key_retry_starts_new_run_with_distinct_key(
    session, monkeypatch, replay_site,
):
    principal, envelope, event, original_run = await _seed_preservation_setup_failure(session)
    original_outcome = deepcopy(event.action_result)
    original_receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()
    assert original_run.metadata_["idempotency_key"] == f"inbound:submission:{event.id}"
    with monkeypatch.context() as patch:
        if replay_site == "insert_conflict":
            # Simulate an event inserted after the first lookup. The real insert
            # hits the unique constraint and the second lookup finds the failed run.
            lookup = AsyncMock(side_effect=[None, event])
            patch.setattr(inbound, "_find_idempotent_event", lookup)

        retry = await inbound.submit_inbound_envelope(
            session, connection=principal, envelope=envelope,
        )

        if replay_site == "insert_conflict":
            assert lookup.await_count == 2
    assert retry["idempotent_replay"] is False
    assert retry["replay_body_matches"] is True
    assert retry["event_id"] == str(event.id)
    handling = await _assert_queued_submission(session, retry["ilo_outcome"])
    assert handling["run_id"] != original_run.id
    retry_run = await session.get(AgentRunRow, handling["run_id"])
    assert retry_run.metadata_["idempotency_key"] == (
        f"inbound:submission:{event.id}:retry:{original_run.id}"
    )
    assert list((await session.scalars(select(AgentRunRow.id).order_by(AgentRunRow.id))).all()) == [
        original_run.id, retry_run.id,
    ]
    assert list((await session.scalars(select(InboundEventRow.id))).all()) == [event.id]
    receipts = list((await session.scalars(select(InboundDecisionReceiptRow))).all())
    assert len(receipts) == 2
    retry_receipt = next(receipt for receipt in receipts if receipt.id != original_receipt.id)
    assert retry_receipt.tool_use["run_id"] == retry_run.id
    await session.refresh(original_receipt)
    assert original_receipt.status == "failed"
    assert original_receipt.outcome == original_outcome
    await session.refresh(original_run)
    assert original_run.status == "failed"
    await session.refresh(event)
    assert event.action_result["handling"]["run_id"] == retry_run.id
    assert event.status == "review_required"
    assert event.error is None

    replay = await inbound.submit_inbound_envelope(
        session, connection=principal, envelope=envelope,
    )
    assert replay["idempotent_replay"] is True
    assert replay["ilo_outcome"]["handling"]["run_id"] == retry_run.id
    assert len(list((await session.scalars(select(AgentRunRow.id))).all())) == 2
    assert len(list((await session.scalars(select(InboundDecisionReceiptRow.id))).all())) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("kind", "signal", id="not-submission"),
        pytest.param("status", "processed", id="event-not-failed"),
        pytest.param("action_result", {}, id="missing-handling"),
        pytest.param("action_result", {"handling": None}, id="null-handling"),
        pytest.param("action_result", {"handling": []}, id="malformed-handling"),
    ],
)
async def test_preservation_setup_retry_requires_failed_submission_event(session, field, value):
    principal, envelope, event, run = await _seed_preservation_setup_failure(session)
    setattr(event, field, value)

    await _assert_preservation_failure_replayed(session, principal, envelope, event, run)


_MISSING_HANDLING_FIELD = object()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("run_status", "completed", id="handling-not-failed"),
        pytest.param("failure", {"category": "internal"}, id="different-failure-category"),
        pytest.param("failure", _MISSING_HANDLING_FIELD, id="missing-failure"),
        pytest.param("failure", [], id="malformed-failure"),
        pytest.param("attribution", _MISSING_HANDLING_FIELD, id="missing-attribution"),
        pytest.param("attribution", None, id="null-attribution"),
        pytest.param("attribution", [], id="malformed-attribution"),
        pytest.param("attribution", {}, id="missing-mutated-target-refs"),
        pytest.param("attribution", {"mutated_target_refs": None}, id="null-mutated-target-refs"),
        pytest.param("attribution", {"mutated_target_refs": {}}, id="mapping-mutated-target-refs"),
        pytest.param("attribution", {"mutated_target_refs": "[]"}, id="string-mutated-target-refs"),
        pytest.param(
            "attribution", {"mutated_target_refs": [{"kind": "memory", "id": "source-1"}]},
            id="nonempty-mutated-target-refs",
        ),
        pytest.param("run_id", _MISSING_HANDLING_FIELD, id="missing-run-id"),
        pytest.param("run_id", None, id="null-run-id"),
        pytest.param("run_id", "1", id="string-run-id"),
        pytest.param("run_id", True, id="boolean-run-id"),
        pytest.param("run_id", 999999, id="missing-run-row"),
    ],
)
async def test_preservation_setup_retry_requires_valid_failed_handling(session, field, value):
    principal, envelope, event, run = await _seed_preservation_setup_failure(session)
    handling = deepcopy(event.action_result["handling"])
    if value is _MISSING_HANDLING_FIELD:
        handling.pop(field)
    else:
        handling[field] = value
    event.action_result = {**event.action_result, "handling": handling}
    # Python considers True equal to 1; explicitly persist the JSON type change.
    flag_modified(event, "action_result")

    await _assert_preservation_failure_replayed(session, principal, envelope, event, run)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("status", "completed", id="run-not-failed"),
        pytest.param("org_id", "55555555-5555-4555-8555-555555555555", id="different-org"),
        pytest.param("metadata_", None, id="missing-metadata"),
        pytest.param("metadata_", {}, id="missing-stored-failure"),
        pytest.param("metadata_", {"failure": []}, id="malformed-stored-failure"),
        pytest.param(
            "metadata_", {"failure": {"category": "internal"}}, id="different-stored-failure-category",
        ),
    ],
)
async def test_preservation_setup_retry_requires_matching_failed_run(session, field, value):
    principal, envelope, event, run = await _seed_preservation_setup_failure(session)
    setattr(run, field, value)

    await _assert_preservation_failure_replayed(session, principal, envelope, event, run)


@pytest.mark.parametrize("event_type", ["run.tool_started", "run.tool_completed", "run.tool_failed"])
async def test_preservation_setup_retry_requires_no_tool_execution(session, event_type):
    principal, envelope, event, run = await _seed_preservation_setup_failure(session)
    # Keep the recorded failure and attribution unchanged: tool evidence alone
    # must block a retry, even if the public handling still claims no mutation.
    await AsyncAgentRunStore(session).append_event(run_event(
        run.id, event_type, {"tool_name": "memory_ingest_source", "args": {}},
        root_run_id=run.id,
    ))

    await _assert_preservation_failure_replayed(session, principal, envelope, event, run)


async def test_preservation_setup_retry_rejects_changed_body(session):
    principal, envelope, event, run = await _seed_preservation_setup_failure(session)
    stored_outcome = deepcopy(event.action_result)
    stored_error = event.error
    changed_envelope = {**envelope, "message": "Preserve a different investigation."}

    replay = await inbound.submit_inbound_envelope(
        session, connection=principal, envelope=changed_envelope,
    )

    assert replay["idempotent_replay"] is True
    assert replay["replay_body_matches"] is False
    assert replay["event_id"] == str(event.id)
    assert replay["ilo_outcome"] == {
        "evidence_status": "replay_mismatch",
        "mutated_target_refs": [],
        "reason": "idempotency_key_reused_with_different_body",
    }
    assert replay["stored_ilo_outcome"] == stored_outcome
    assert replay["submitted_envelope_digest"] != replay["stored_envelope_digest"]
    assert list((await session.scalars(select(AgentRunRow.id))).all()) == [run.id]
    assert len(list((await session.scalars(select(InboundDecisionReceiptRow.id))).all())) == 1
    await session.refresh(event)
    assert event.action_result == stored_outcome
    assert event.status == "failed"
    assert event.error == stored_error
