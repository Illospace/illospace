from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.external_agent import (
    ExternalAgentConnectionRow,
    ExternalAgentConnectionTokenRow,
)
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundEventRow
from brain.platform.db.models.org import Org, User
from brain.systems.external_agents import service as external_agents
from brain.systems.inbound import service as inbound
from brain.systems.inbound.preservation import PRESERVATION_MISSING_REASON
from brain.systems.runs.events import run_event
from brain.systems.runs.failure_diagnostic import (
    RunFailureStage,
    failure_diagnostic_metadata,
)
from brain.systems.runs.failures import (
    DEFAULT_FAILED_RUN_MESSAGE,
    PRESERVATION_SETUP_FAILED_RUN_MESSAGE,
)
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AsyncAgentRunStore


pytestmark = pytest.mark.asyncio

ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
CONNECTION_ID = "33333333-3333-4333-8333-333333333333"
TOKEN_ID = "44444444-4444-4444-8444-444444444444"
RAW_TOKEN = "illo_conn_test_webhook_token"


def _patch_sqlite_for_pg_types():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"
    for name in ("visit_VECTOR", "visit_Vector"):
        if not hasattr(SQLiteTypeCompiler, name):
            setattr(SQLiteTypeCompiler, name, lambda self, type_, **kw: "TEXT")

    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_inbound_preservation_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    patched._inbound_preservation_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    return await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            ExternalAgentConnectionRow.__table__,
            ExternalAgentConnectionTokenRow.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
            InboundEventRow.__table__,
            InboundDecisionReceiptRow.__table__,
        ]
    )


async def _seed_connection(session) -> external_agents.AgentBridgePrincipal:
    if await session.get(Org, ORG_ID) is None:
        session.add(Org(id=ORG_ID, name="Test Org", slug="test-org"))
    if await session.get(User, USER_ID) is None:
        session.add(User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com"))
    session.add_all(
        [
            ExternalAgentConnectionRow(
                id=CONNECTION_ID,
                org_id=ORG_ID,
                owner_user_id=USER_ID,
                display_name="Codex",
                agent_kind="codex",
                transport="mcp",
                status="online",
                remote_agent_card={},
                capabilities={
                    "illo_submit": True,
                    "illo_read": True,
                    "illo_act": True,
                    "illo_get_result": True,
                },
                auth_metadata={},
                metadata_={},
            ),
            ExternalAgentConnectionTokenRow(
                id=TOKEN_ID,
                connection_id=CONNECTION_ID,
                org_id=ORG_ID,
                owner_user_id=USER_ID,
                token_hash=external_agents.hash_connection_token(RAW_TOKEN),
                token_prefix=external_agents.token_prefix(RAW_TOKEN),
                name="MCP token",
                scopes=[external_agents.SCOPE_SIGNAL_SUBMIT],
            ),
        ]
    )
    await session.flush()
    return external_agents.AgentBridgePrincipal(
        connection_id=CONNECTION_ID,
        org_id=ORG_ID,
        owner_user_id=USER_ID,
        token_id=TOKEN_ID,
        scopes=frozenset([external_agents.SCOPE_SIGNAL_SUBMIT]),
        connection_display_name="Codex",
        agent_kind="codex",
    )


async def _assert_queued_submission(session, outcome: dict) -> dict:
    assert outcome["operation"] == "queued"
    assert outcome["message"] == "Submission accepted and queued for Illo handling."
    handling = outcome["handling"]
    assert handling["status"] == "queued"
    assert handling["event_id"]
    assert handling["run_id"]

    run = await session.get(AgentRunRow, handling["run_id"])
    assert run is not None
    assert run.thread_id == f"inbound:{CONNECTION_ID}:{handling['event_id']}"
    assert run.status == "queued"
    assert run.metadata_["producer"] == "inbound"
    assert run.target_ref["kind"] == "inbound_submission"
    assert run.target_ref["event_id"] == handling["event_id"]
    assert run.metadata_["submission"]["message"]
    return handling


async def _finish_triage_run(
    session,
    triage: dict,
    *,
    status: RunStatus,
    final_answer: str,
) -> None:
    store = AsyncAgentRunStore(session)
    run_id = int(triage["run_id"])
    await store.set_status(run_id, RunStatus.STARTING)
    await store.set_status(run_id, RunStatus.RUNNING)
    await store.append_final_answer_once(run_id, final_answer, root_run_id=run_id)
    await store.set_status(run_id, status)


async def test_preservation_submission_gets_curator_prompt_and_contract(session):
    principal = await _seed_connection(session)
    envelope = {
        "kind": "submission",
        "origin": "codex.submit",
        "desired_outcome": "preserve_knowledge",
        "message": "Please preserve this Roman methodology note as durable Illo knowledge.",
        "parts": [{"type": "text", "title": "Roman methodology", "content": "# Roman PDP Methodology"}],
        "source": {"source_tool": "codex", "repo": "uwear-backend"},
        "constraints": {"preserve_as": "durable methodology note", "visibility": "org"},
        "idempotency_key": "codex:submission:preserve-contract",
    }

    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope=envelope,
        ingress_context={"surface": "test"},
    )

    handling = await _assert_queued_submission(session, result["ilo_outcome"])
    run = await session.get(AgentRunRow, handling["run_id"])
    event = await session.get(InboundEventRow, result["event_id"])

    assert run is not None
    # The mandatory preservation preamble is now a soft hint, not a storage mandate,
    # so the operator's real request is no longer buried under ceremony.
    assert "Possible preservation workflow:" in run.input_message
    assert "Treat this as a hint, not a storage mandate." in run.input_message
    assert "Final answer must list" not in run.input_message
    # The raw operator message leads the run prompt; source/constraints are metadata only.
    assert run.input_message.startswith(envelope["message"])
    assert "Source metadata:" not in run.input_message
    preservation = run.metadata_["submission"]["preservation"]
    assert preservation["requires_durable_evidence"] is True
    assert preservation["intent"] == "preserve_knowledge"
    assert preservation["detection_source"] == "desired_outcome"
    # Origin/source/constraints are carried as structured run metadata, not prompt text.
    assert run.metadata_["submission"]["source"] == envelope["source"]
    assert run.metadata_["submission"]["constraints"] == envelope["constraints"]
    assert event is not None
    assert event.action_result["preservation"]["requires_durable_evidence"] is True


async def test_language_only_preservation_match_is_prompt_hint_not_evidence_contract(session):
    principal = await _seed_connection(session)
    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "kind": "submission",
            "origin": "codex.submit",
            "message": "Store this process note so Illo can reuse it later.",
            "parts": [{"type": "text", "content": "Long enough process note for a future workflow."}],
            "source": {"source_tool": "codex"},
            "idempotency_key": "codex:submission:language-hint",
        },
        ingress_context={"surface": "test"},
    )
    handling = await _assert_queued_submission(session, result["ilo_outcome"])
    run = await session.get(AgentRunRow, handling["run_id"])
    assert run is not None

    preservation = run.metadata_["submission"]["preservation"]
    assert preservation["intent"] == "possible_preservation"
    assert preservation["requires_durable_evidence"] is False
    assert "Possible preservation workflow:" in run.input_message


async def test_submission_tags_human_message_for_introspection_routing(session):
    # Regression for issue #249: the headless submission wrapper used to embed
    # boilerplate ("Handle this external coordination submission.", "Source metadata:")
    # that could trip the self-context heuristic and hijack the final answer with a
    # runtime self-description. The prompt now leads with the operator's raw message and
    # keeps origin/source in metadata, and the clean message is still tagged as
    # human_message so required-introspection routing evaluates the request.
    from brain.systems.runs import introspection as run_introspection

    principal = await _seed_connection(session)
    message = (
        "Post-deploy SEO health check for uwear.ai: confirm the 301 redirects resolve "
        "and keyword rankings are retained since the deploy."
    )
    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "kind": "submission",
            "origin": "claude-code.submit",
            "desired_outcome": "seo_post_deploy_health_report",
            "message": message,
            "source": {"source_tool": "claude-code", "repo": "uwear-website"},
            "idempotency_key": "claude-code:submission:seo-249",
        },
        ingress_context={"surface": "test"},
    )

    handling = await _assert_queued_submission(session, result["ilo_outcome"])
    run = await session.get(AgentRunRow, handling["run_id"])
    assert run is not None
    # The wrapper envelope is gone: the run prompt leads with the raw operator message
    # and no longer embeds the "Source metadata:" boilerplate that caused the false-match.
    assert run.input_message.startswith(message)
    assert "Handle this external coordination submission." not in run.input_message
    assert "Source metadata:" not in run.input_message
    # The clean operator message is tagged for introspection routing...
    assert run.metadata_["human_message"] == message
    # ...and routing resolves to it rather than the wrapper prompt persisted as
    # input_message, so the coordination boilerplate can no longer force a detour.
    assert (
        run_introspection.message_for_required_introspection(run.input_message, run.metadata_)
        == message
    )


async def test_preservation_submission_without_durable_evidence_stays_actionable(session):
    principal = await _seed_connection(session)
    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "kind": "submission",
            "origin": "codex.submit",
            "desired_outcome": "preserve_knowledge",
            "message": "Store this process note so Illo can reuse it later.",
            "parts": [{"type": "text", "content": "Long enough process note for a future workflow."}],
            "source": {"source_tool": "codex"},
            "idempotency_key": "codex:submission:missing-evidence",
        },
        ingress_context={"surface": "test"},
    )
    handling = await _assert_queued_submission(session, result["ilo_outcome"])

    await _finish_triage_run(
        session,
        handling,
        status=RunStatus.COMPLETED,
        final_answer="I reviewed the note and decided it was useful.",
    )

    event = await session.get(InboundEventRow, result["event_id"])
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()

    assert event is not None
    assert event.status == "review_required"
    assert event.action_result["handling"]["status"] == "needs_action"
    assert event.action_result["handling"]["run_status"] == "completed"
    evidence = event.action_result["handling"]["evidence_contract"]
    assert evidence["status"] == "missing"
    assert evidence["reason"] == PRESERVATION_MISSING_REASON
    assert "non_durable_target_refs" not in evidence
    assert "did not produce durable storage evidence" in event.error
    assert receipt.status == "review_required"
    assert receipt.tool_use["status"] == "needs_action"
    assert receipt.tool_use["evidence_contract"]["status"] == "missing"


async def test_preservation_submission_with_github_comment_names_non_durable_evidence(session):
    principal = await _seed_connection(session)
    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "kind": "submission",
            "origin": "codex.session-preserve",
            "desired_outcome": "preserve_knowledge",
            "message": "Preserve this diagnosis for later use.",
            "source": {"source_tool": "codex"},
            "idempotency_key": "codex:submission:github-comment-evidence",
        },
        ingress_context={"surface": "test"},
    )
    handling = await _assert_queued_submission(session, result["ilo_outcome"])
    store = AsyncAgentRunStore(session)
    run_id = int(handling["run_id"])
    await store.set_status(run_id, RunStatus.STARTING)
    await store.set_status(run_id, RunStatus.RUNNING)
    await store.append_event(
        run_event(
            run_id,
            "run.tool_completed",
            {
                "tool_name": "add_github_issue_comment",
                "args": {
                    "repo": "uwear-ai/uwear-backend",
                    "issue_number": 1884,
                    "body": "Durable diagnosis.",
                },
                "result": json.dumps(
                    {
                        "repo": "uwear-ai/uwear-backend",
                        "issue_number": 1884,
                        "comment": {
                            "id": 5440364747,
                            "node_id": "IC_kwDOLtZ_Ds8AAAABREVgyw",
                            "html_url": (
                                "https://github.com/uwear-ai/uwear-backend/issues/1884"
                                "#issuecomment-5440364747"
                            ),
                        },
                    }
                ),
            },
            root_run_id=run_id,
        )
    )
    await store.append_final_answer_once(
        run_id,
        "Preserved the diagnosis on GitHub issue #1884.",
        root_run_id=run_id,
    )
    await store.set_status(run_id, RunStatus.COMPLETED)

    event = await session.get(InboundEventRow, result["event_id"])
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()

    assert event is not None
    assert event.status == "review_required"
    evidence = event.action_result["handling"]["evidence_contract"]
    expected_ref = {
        "kind": "github_issue_comment",
        "id": "uwear-ai/uwear-backend#1884:comment:5440364747",
        "source": "add_github_issue_comment",
    }
    assert evidence["status"] == "missing"
    assert evidence["mutated_target_refs"] == []
    assert evidence["non_durable_target_refs"] == [expected_ref]
    assert evidence["reason"] != PRESERVATION_MISSING_REASON
    assert "non-durable surface" in evidence["reason"]
    assert "Illo-owned" in evidence["reason"]
    assert receipt.status == "review_required"
    assert receipt.tool_use["evidence_contract"]["non_durable_target_refs"] == [expected_ref]
    assert receipt.tool_use["attribution"]["mutated_target_refs"] == [expected_ref]


async def test_preservation_submission_with_explicit_memory_evidence_reconciles_processed(session):
    principal = await _seed_connection(session)
    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "kind": "submission",
            "origin": "codex.submit",
            "desired_outcome": "preserve_knowledge",
            "message": "Preserve this reusable ArtDirection playbook in Illo memory.",
            "parts": [{"type": "text", "content": "A reusable playbook with enough detail for ingestion."}],
            "source": {"source_tool": "codex"},
            "idempotency_key": "codex:submission:memory-evidence",
        },
        ingress_context={"surface": "test"},
    )
    handling = await _assert_queued_submission(session, result["ilo_outcome"])
    store = AsyncAgentRunStore(session)
    run_id = int(handling["run_id"])
    await store.set_status(run_id, RunStatus.STARTING)
    await store.set_status(run_id, RunStatus.RUNNING)
    await store.append_event(
        run_event(
            run_id,
            "run.tool_completed",
            {
                "tool_name": "memory_ingest_source",
                "args": {"content_kind": "procedure", "source_kind": "inbound_submission"},
                "result": json.dumps(
                    {
                        "operation": "created",
                        "mutated_target_refs": [
                            {"kind": "memory_source", "id": 91},
                            {"kind": "memory_node", "id": 93},
                        ],
                    }
                ),
            },
            root_run_id=run_id,
        )
    )
    await store.append_final_answer_once(run_id, "Stored the playbook in reconstructive memory.", root_run_id=run_id)
    await store.set_status(run_id, RunStatus.COMPLETED)

    event = await session.get(InboundEventRow, result["event_id"])
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()

    assert event is not None
    assert event.status == "processed"
    assert event.error is None
    evidence = event.action_result["handling"]["evidence_contract"]
    assert evidence["status"] == "satisfied"
    assert "non_durable_target_refs" not in evidence
    assert {"kind": "memory_source", "id": "91", "source": "memory_ingest_source"} in evidence["mutated_target_refs"]
    assert receipt.status == "processed"
    assert "memory_source" in {ref["kind"] for ref in receipt.tool_use["attribution"]["mutated_target_refs"]}


async def test_preservation_submission_with_memory_supersede_source_reconciles_processed(session):
    principal = await _seed_connection(session)
    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "kind": "submission",
            "origin": "codex.memory",
            "desired_outcome": "preserve_knowledge",
            "message": "Preserve this correction to an existing memory.",
            "source": {"source_tool": "codex"},
            "idempotency_key": "codex:submission:memory-supersede-evidence",
        },
        ingress_context={"surface": "test"},
    )
    handling = await _assert_queued_submission(session, result["ilo_outcome"])
    store = AsyncAgentRunStore(session)
    run_id = int(handling["run_id"])
    await store.set_status(run_id, RunStatus.STARTING)
    await store.set_status(run_id, RunStatus.RUNNING)
    await store.append_event(
        run_event(
            run_id,
            "run.tool_completed",
            {
                "tool_name": "memory_supersede",
                "args": {
                    "old_node": 2153,
                    "new_content": "Corrected durable content for the superseded memory.",
                },
                "result": json.dumps(
                    {
                        "action": "supersede",
                        "old_node": 2153,
                        "new_node": 2160,
                        "edge_id": 11658,
                        "curation_source_id": 11659,
                        "replacement_source_id": 11660,
                    }
                ),
            },
            root_run_id=run_id,
        )
    )
    await store.append_final_answer_once(run_id, "Superseded the stale memory.", root_run_id=run_id)
    await store.set_status(run_id, RunStatus.COMPLETED)

    event = await session.get(InboundEventRow, result["event_id"])
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()

    assert event is not None
    assert event.status == "processed"
    assert event.error is None
    evidence = event.action_result["handling"]["evidence_contract"]
    assert evidence["status"] == "satisfied"
    assert "memory_supersede" in evidence["acceptable_tools"]
    assert "memory_edge" not in evidence["acceptable_target_kinds"]
    assert evidence["tool_names"] == ["memory_supersede"]
    assert evidence["mutated_target_refs"] == [
        {"kind": "memory_source", "id": "11659", "source": "memory_supersede"},
        {"kind": "memory_source", "id": "11660", "source": "memory_supersede"},
    ]
    assert receipt.status == "processed"


async def test_preservation_submission_with_memory_supersede_edge_only_stays_actionable(session):
    principal = await _seed_connection(session)
    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "kind": "submission",
            "origin": "codex.memory",
            "desired_outcome": "preserve_knowledge",
            "message": "Preserve this correction to an existing memory.",
            "source": {"source_tool": "codex"},
            "idempotency_key": "codex:submission:memory-supersede-edge-only",
        },
        ingress_context={"surface": "test"},
    )
    handling = await _assert_queued_submission(session, result["ilo_outcome"])
    store = AsyncAgentRunStore(session)
    run_id = int(handling["run_id"])
    await store.set_status(run_id, RunStatus.STARTING)
    await store.set_status(run_id, RunStatus.RUNNING)
    await store.append_event(
        run_event(
            run_id,
            "run.tool_completed",
            {
                "tool_name": "memory_supersede",
                "args": {"old_node": 2153, "new_node": 2160},
                "result": json.dumps(
                    {
                        "action": "supersede",
                        "old_node": 2153,
                        "new_node": 2160,
                        "edge_id": 11658,
                    }
                ),
            },
            root_run_id=run_id,
        )
    )
    await store.append_final_answer_once(run_id, "Superseded the stale memory.", root_run_id=run_id)
    await store.set_status(run_id, RunStatus.COMPLETED)

    event = await session.get(InboundEventRow, result["event_id"])
    receipt = (await session.scalars(select(InboundDecisionReceiptRow))).one()

    assert event is not None
    assert event.status == "review_required"
    evidence = event.action_result["handling"]["evidence_contract"]
    assert evidence["status"] == "missing"
    assert "memory_supersede" in evidence["acceptable_tools"]
    assert "memory_edge" not in evidence["acceptable_target_kinds"]
    assert evidence["tool_names"] == ["memory_supersede"]
    assert evidence["mutated_target_refs"] == []
    assert receipt.status == "review_required"
    assert receipt.tool_use["attribution"]["mutated_target_refs"] == [
        {"kind": "memory_edge", "id": "11658", "source": "memory_supersede"}
    ]


async def test_get_result_lazily_reconciles_completed_submission_run(session):
    principal = await _seed_connection(session)
    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "kind": "submission",
            "origin": "codex.submit",
            "message": "Review this context and decide what to do.",
            "source": {"source_tool": "codex"},
            "idempotency_key": "codex:submission:lazy-reconcile",
        },
        ingress_context={"surface": "test"},
    )
    handling = await _assert_queued_submission(session, result["ilo_outcome"])
    run = await session.get(AgentRunRow, handling["run_id"])
    assert run is not None
    run.status = RunStatus.COMPLETED.value
    run.completed_at = datetime.now(timezone.utc)
    await session.flush()

    from brain.app.api.routers.agent_mcp import _tool_get_result

    payload = await _tool_get_result(session, principal, {"event_id": result["event_id"]})

    assert "_mutates_inbound" not in payload
    assert payload["status"] == "processed"
    assert payload["handling_status"] == "completed"
    assert payload["run_status"] == "completed"
    assert payload["evidence_status"] == "not_required"


async def test_get_result_failed_preservation_reports_exception_class_states(session):
    from brain.app.api.routers.agent_mcp import _tool_get_result
    from brain.systems.runs.engine import AsyncAgentRunEngine

    principal = await _seed_connection(session)
    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "kind": "submission",
            "origin": "codex.memory",
            "desired_outcome": "preserve_knowledge",
            "message": "Preserve this durable finding.",
            "source": {"source_tool": "codex"},
            "idempotency_key": "codex:submission:failed-result-diagnostic",
        },
        ingress_context={"surface": "test"},
    )
    handling = await _assert_queued_submission(session, result["ilo_outcome"])
    run_id = int(handling["run_id"])
    store = AsyncAgentRunStore(session)
    await store.set_status(run_id, RunStatus.STARTING)
    await store.set_status(run_id, RunStatus.RUNNING)
    await store.append_event(
        run_event(run_id, "run.tool_started", {"tool_name": "workspace_search"})
    )
    raw_diagnostic = "provider failed with token=receipt-secret"
    exception_type = ValueError
    await AsyncAgentRunEngine(session, recipes={}).fail(
        run_id,
        raw_diagnostic,
        failure_stage=RunFailureStage.AGENT_EXECUTION,
        exception_type=exception_type,
    )

    payload = await _tool_get_result(session, principal, {"event_id": result["event_id"]})

    public_failure = {
        "status": "failed",
        "category": "internal",
        "message": DEFAULT_FAILED_RUN_MESSAGE,
    }
    assert payload["failure"] == {
        **public_failure,
        "diagnostic": {
            "stage": "agent_execution",
            "stage_state": "known",
            "exception_class": exception_type.__name__,
            "exception_class_state": "known",
            "tool_execution_started": True,
            "terminal": True,
            "retry_scheduled": False,
        },
    }
    assert payload["event"]["failure"] == public_failure
    assert payload["run_status"] == "failed"
    assert payload["evidence_status"] == "failed"
    assert payload["retry_attempt"] is None
    assert payload["replacement_run_id"] is None
    assert payload["retry_lineage"] is None
    assert raw_diagnostic not in json.dumps(payload)

    synthesized_exception = type(
        "sk_live_abc123DEF456token",
        (Exception,),
        {},
    )
    synthesized_metadata = failure_diagnostic_metadata(
        stage=RunFailureStage.AGENT_EXECUTION,
        exception_type=synthesized_exception,
    )
    assert "exception_class" not in synthesized_metadata
    await store.update_metadata(
        run_id,
        {"failure": {"category": "internal", **synthesized_metadata}},
    )

    redacted_payload = await _tool_get_result(
        session,
        principal,
        {"event_id": result["event_id"]},
    )

    assert redacted_payload["failure"]["diagnostic"]["exception_class"] is None
    assert (
        redacted_payload["failure"]["diagnostic"]["exception_class_state"]
        == "redacted"
    )

    absent_metadata = failure_diagnostic_metadata(
        stage=RunFailureStage.AGENT_EXECUTION,
    )
    await store.update_metadata(
        run_id,
        {"failure": {"category": "internal", **absent_metadata}},
    )

    absent_payload = await _tool_get_result(
        session,
        principal,
        {"event_id": result["event_id"]},
    )

    assert absent_payload["failure"]["diagnostic"]["exception_class"] is None
    assert (
        absent_payload["failure"]["diagnostic"]["exception_class_state"]
        == "unknown"
    )

    await store.update_metadata(
        run_id,
        {
            "failure": {
                "category": "internal",
                "diagnostic_schema": "typed_v1",
                "stage": "sk_live_abc123DEF456token",
                "exception_class": "private_exception_token",
            }
        },
    )

    withheld_payload = await _tool_get_result(
        session,
        principal,
        {"event_id": result["event_id"]},
    )

    assert withheld_payload["failure"]["diagnostic"] == {
        "stage": "unknown",
        "stage_state": "redacted",
        "exception_class": None,
        "exception_class_state": "redacted",
        "tool_execution_started": True,
        "terminal": True,
        "retry_scheduled": False,
    }
    assert "sk_live_abc123DEF456token" not in json.dumps(withheld_payload)
    assert "private_exception_token" not in json.dumps(withheld_payload)


async def test_codex_session_preservation_pre_tool_failure_is_actionable(session):
    from brain.app.api.routers.agent_mcp import _tool_get_result
    from brain.systems.runs.engine import AsyncAgentRunEngine

    principal = await _seed_connection(session)
    result = await inbound.submit_inbound_envelope(
        session,
        connection=principal,
        envelope={
            "kind": "submission",
            "origin": "codex.session-preserve",
            "desired_outcome": "preserve_knowledge",
            "message": "Preserve the reusable result of Illospace issue #868.",
            "source": {
                "source_tool": "codex",
                "repo": "Illospace/illospace",
                "branch": "illo-qa/868-preserve-knowledge-preflight",
                "session_id": "session-868",
                "run_id": "run-868",
                "task_title": "Fix preserve_knowledge pre-tool failures",
                "files_touched": ["brain/systems/inbound/preservation.py"],
                "repository": "https://github.com/Illospace/illospace",
                "issue": "https://github.com/Illospace/illospace/issues/868",
                "pull_request": "https://github.com/Illospace/illospace/pull/866",
            },
            "idempotency_key": "codex:session-preserve:issue-868",
        },
        ingress_context={"surface": "test"},
    )
    handling = await _assert_queued_submission(session, result["ilo_outcome"])
    run_id = int(handling["run_id"])
    run = await session.get(AgentRunRow, run_id)
    assert run is not None
    assert run.metadata_["submission"]["source"]["source_tool"] == "codex"
    assert "memory_ingest_source" in run.metadata_["submission"]["preservation"]["acceptable_tools"]
    store = AsyncAgentRunStore(session)
    await store.set_status(run_id, RunStatus.STARTING)
    await store.set_status(run_id, RunStatus.RUNNING)
    await AsyncAgentRunEngine(session, recipes={}).fail(
        run_id,
        "direct agent failed before its first tool call",
        final_output=DEFAULT_FAILED_RUN_MESSAGE,
        failure_stage=RunFailureStage.AGENT_EXECUTION,
        exception_type=RuntimeError,
    )

    payload = await _tool_get_result(session, principal, {"event_id": result["event_id"]})

    assert payload["failure"]["category"] == "preservation_setup"
    assert payload["failure"]["message"] == PRESERVATION_SETUP_FAILED_RUN_MESSAGE
    assert payload["failure"]["message"] != DEFAULT_FAILED_RUN_MESSAGE
    assert payload["failure"]["diagnostic"] == {
        "stage": "agent_execution",
        "stage_state": "known",
        "exception_class": "RuntimeError",
        "exception_class_state": "known",
        "tool_execution_started": False,
        "terminal": True,
        "retry_scheduled": False,
    }
    assert payload["evidence_status"] == "failed"
