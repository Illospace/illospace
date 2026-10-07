"""Attribution: durable-ref extraction and durable-work classification.

The gap under test (2026-07-16 fix): a run whose entire outcome is a filed
GitHub issue reported NO mutated refs — the connector payload carries
``{"repo", "issue": {"number"}}``, not ``*_id`` keys — which left the
durable-work classification blind to the most common actionable outcome.
"""

from __future__ import annotations

import json

import pytest

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.systems.inbound.attribution import (
    WORK_ITEM_REF_KINDS,
    collect_result_refs,
    summarize_inbound_run_attribution,
)
from brain.systems.inbound.preservation import preservation_evidence_result
from brain.systems.runs.status import RunStatus


@pytest.fixture
async def session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    return await async_sqlite_session_factory([
        AgentRunRow.__table__,
        AgentRunEventRow.__table__,
    ])


def _tool_event(run_id: int, seq: int, tool: str, result: dict) -> AgentRunEventRow:
    return AgentRunEventRow(
        run_id=run_id,
        sequence_no=seq,
        event_type="run.tool_completed",
        payload={"tool_name": tool, "args": {}, "result": json.dumps(result)},
    )


async def _seed_run(session) -> int:
    run = AgentRunRow(
        org_id=None,
        thread_id="t-1",
        profile="fast",
        recipe="illo",
        status="completed",
        input_message="x",
    )
    session.add(run)
    await session.flush()
    return int(run.id)


async def test_created_github_issue_becomes_a_mutated_ref(session):
    run_id = await _seed_run(session)
    session.add(_tool_event(run_id, 1, "create_github_issue", {
        "repo": "uwear-ai/uwear-backend",
        "issue": {"type": "issue", "number": 616, "title": "Rollbar: boom",
                  "html_url": "https://github.com/uwear-ai/uwear-backend/issues/616"},
        "token_source": "github_app",
    }))
    await session.flush()

    attribution = await summarize_inbound_run_attribution(
        session, run_id=run_id, status=RunStatus.COMPLETED
    )
    refs = attribution["mutated_target_refs"]
    assert {"kind": "github_issue", "id": "uwear-ai/uwear-backend#616",
            "source": "create_github_issue"} in refs


async def test_created_pull_request_ref_kind(session):
    run_id = await _seed_run(session)
    session.add(_tool_event(run_id, 1, "create_github_issue", {
        "repo": "uwear-ai/uwear-website",
        "issue": {"type": "pull_request", "number": 88},
    }))
    await session.flush()

    attribution = await summarize_inbound_run_attribution(
        session, run_id=run_id, status=RunStatus.COMPLETED
    )
    kinds = {ref["kind"] for ref in attribution["mutated_target_refs"]}
    assert "github_pull_request" in kinds


async def test_created_pull_request_top_level_payload_ref_kind(session):
    # async_create_repo_pull_request returns {"repo", "pull_request": {...}},
    # not the nested {"issue": {...}} shape the other PR test covers.
    run_id = await _seed_run(session)
    session.add(_tool_event(run_id, 1, "create_github_pull_request", {
        "repo": "uwear-ai/uwear-website",
        "pull_request": {"type": "pull_request", "number": 220},
    }))
    await session.flush()

    attribution = await summarize_inbound_run_attribution(
        session, run_id=run_id, status=RunStatus.COMPLETED
    )

    assert {
        "kind": "github_pull_request",
        "id": "uwear-ai/uwear-website#220",
        "source": "create_github_pull_request",
    } in attribution["mutated_target_refs"]


async def test_github_issue_comment_becomes_a_mutated_ref(session):
    run_id = await _seed_run(session)
    session.add(_tool_event(run_id, 1, "add_github_issue_comment", {
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
    }))
    await session.flush()

    attribution = await summarize_inbound_run_attribution(
        session, run_id=run_id, status=RunStatus.COMPLETED
    )

    assert attribution["mutated_target_refs"] == [
        {
            "kind": "github_issue_comment",
            "id": "uwear-ai/uwear-backend#1884:comment:5440364747",
            "source": "add_github_issue_comment",
        }
    ]


def test_work_item_vocabulary_is_the_expected_set():
    assert WORK_ITEM_REF_KINDS == {
        "github_issue", "github_pull_request", "idea", "domain_record",
        "agent_run", "launch_handoff", "thread",
    }


async def test_truncated_result_preview_recovers_refs_from_result_refs(session):
    """Run events store a 1000-char result PREVIEW; the executor extracts
    refs from the FULL result into payload.result_refs first. A truncated
    (unparseable) preview must not blind attribution to what the tool
    created (live illo-dev finding, 2026-07-16: tracker record invisible
    to durable-work classification)."""
    run_id = await _seed_run(session)
    big_result = json.dumps({"record": {"id": 1823, "domain_id": 30, "pad": "x" * 5000}})
    session.add(AgentRunEventRow(
        run_id=run_id,
        sequence_no=1,
        event_type="run.tool_completed",
        payload={
            "tool_name": "manage_domain",
            "args": {"action": "create_record"},
            "result": big_result[:1000],  # what tools.py persists
            "result_refs": [
                {"kind": "domain_record", "id": "1823", "source": "manage_domain"},
                {"kind": "domain", "id": "30", "source": "manage_domain"},
            ],
        },
    ))
    await session.flush()

    attribution = await summarize_inbound_run_attribution(
        session, run_id=run_id, status=RunStatus.COMPLETED
    )
    assert {"kind": "domain_record", "id": "1823", "source": "manage_domain"} in (
        attribution["mutated_target_refs"]
    )


def test_event_payload_carries_full_result_refs_beside_preview():
    from brain.systems.runs.tools import _event_payload

    big_result = json.dumps({"record": {"id": 99, "pad": "y" * 3000}})
    payload = _event_payload("manage_domain", {"action": "create_record"}, result=big_result)
    assert len(payload["result"]) == 1000  # bounded preview
    assert {"kind": "domain_record", "id": "99", "source": "manage_domain"} in payload["result_refs"]

    small = _event_payload("post_slack_reply", {}, result=json.dumps({"ok": True, "channel_id": "C1"}))
    assert "result_refs" not in small  # no refs → no key


def test_oversized_ref_ids_are_dropped_never_truncated():
    """A clipped id is a wrong ref, and unbounded ids would balloon the
    persisted result_refs payload — drop anything id-shaped that is really
    content (cross-family review finding, 2026-07-16)."""
    from brain.systems.inbound.attribution import collect_result_refs

    refs = collect_result_refs(
        json.dumps({"record_id": "z" * 1_000_000, "idea_id": "idea-ok"}),
        source="s" * 500,
    )
    assert refs == [{"kind": "idea", "id": "idea-ok", "source": "s" * 80}]


def test_memory_annotations_preserve_ref_identities_and_evidence_status():
    # Same refs as the existing explicit-memory preservation fixture.
    legacy = {"mutated_target_refs": [
        {"kind": "memory_source", "id": 91},
        {"kind": "memory_node", "id": 93},
    ]}
    annotated = {
        **legacy,
        "content_node_id": 93,
        "visibility": "private",
        "knowledge_index": {"eligible": False, "reason": "private_visibility"},
        "mutated_target_refs": [legacy["mutated_target_refs"][0], {
            "kind": "memory_node", "id": 93, "role": "content",
            "visibility": "private", "knowledge_get": "private_visibility",
        }],
    }
    old_refs = collect_result_refs(legacy, source="memory_ingest_source")
    new_refs = collect_result_refs(annotated, source="memory_ingest_source")
    expected = {("memory_source", "91"), ("memory_node", "93")}
    assert {(ref["kind"], ref["id"]) for ref in old_refs} == expected
    assert {(ref["kind"], ref["id"]) for ref in new_refs} == expected
    assert len(new_refs) == len(expected)
    assert new_refs[1] == {
        **old_refs[1], "role": "content", "visibility": "private", "knowledge_get": "private_visibility",
    }

    for required, status, kinds, expected_status in [
        (True, "completed", ["memory_node"], "satisfied"),
        (True, "completed", ["project_context"], "missing"),
        (True, "running", ["memory_node"], "running"),
        (True, "failed", ["memory_node"], "failed"),
        (False, "completed", ["memory_node"], "not_required"),
    ]:
        contract = {"requires_durable_evidence": required, "acceptable_target_kinds": kinds}
        for refs in (old_refs, new_refs):
            evidence = preservation_evidence_result(
                contract, run_status=status,
                attribution={"mutated_target_refs": refs, "tool_names": ["memory_ingest_source"]},
            )
            assert evidence["status"] == expected_status
            if expected_status == "satisfied":
                assert evidence["mutated_target_refs"] == [refs[1]]
            elif expected_status == "missing":
                assert evidence["mutated_target_refs"] == []


@pytest.mark.parametrize("truncate", [False, True])
async def test_memory_content_annotations_survive_event_ref_dedup(session, truncate):
    from brain.systems.runs.tools import _event_payload

    run_id = await _seed_run(session)
    result = {
        "content_node_id": 93,
        "visibility": "team",
        "knowledge_index": {"eligible": True, "reason": None},
        "mutated_target_refs": [{
            "kind": "memory_node", "id": 93, "role": "content",
            "visibility": "team", "knowledge_get": "eligible",
        }],
        "cue_node_ids": [94],
        "tag_node_ids": [95],
        "padding": "x" * (2000 if truncate else 0),
    }
    payload = _event_payload("memory_ingest_source", {}, result=json.dumps(result))
    if not truncate:
        # A legacy preview may contain the ID without the new annotations.
        payload["result"] = json.dumps({"content_node_id": 93})
    session.add(AgentRunEventRow(
        run_id=run_id, sequence_no=1, event_type="run.tool_completed", payload=payload,
    ))
    await session.flush()
    attribution = await summarize_inbound_run_attribution(
        session, run_id=run_id, status=RunStatus.COMPLETED,
    )
    assert attribution["mutated_target_refs"] == [
        {"kind": "memory_node", "id": "93", "source": "memory_ingest_source",
         "role": "content", "visibility": "team", "knowledge_get": "eligible"},
        {"kind": "memory_node", "id": "94", "source": "memory_ingest_source"},
        {"kind": "memory_node", "id": "95", "source": "memory_ingest_source"},
    ]


def test_memory_ref_annotations_remain_bounded():
    refs = collect_result_refs({"mutated_target_refs": [
        {"kind": "memory_node", "id": "93", "role": "content", "visibility": "x" * 1000,
         "knowledge_get": "y" * 1000, "extra": "z" * 1000},
    ]}, source="memory_ingest_source")
    assert refs == [{
        "kind": "memory_node", "id": "93", "source": "memory_ingest_source",
    }]


def test_ref_cap_precedes_value_conversion():
    refs = collect_result_refs({"cue_node_ids": [*range(1, 21), 10**5000]}, source="x")
    assert refs == [
        {"kind": "memory_node", "id": str(node_id), "source": "x"}
        for node_id in range(1, 21)
    ]


def test_unconvertible_ref_id_is_dropped_before_cap():
    refs = collect_result_refs({"cue_node_ids": [10**5000, 1, 2]}, source="x")
    assert refs == [
        {"kind": "memory_node", "id": "1", "source": "x"},
        {"kind": "memory_node", "id": "2", "source": "x"},
    ]


def test_996_level_result_keeps_all_collected_refs():
    # A JSON-compatible payload with 996 object levels, including the root.
    nested = None
    for _ in range(995):
        nested = {"nested": nested}
    result = {"cue_node_ids": list(range(1, 21)), "nested": nested}

    assert collect_result_refs(result, source="x") == [
        {"kind": "memory_node", "id": str(node_id), "source": "x"}
        for node_id in range(1, 21)
    ]


def test_junk_annotations_leave_collected_refs_unchanged():
    from brain.systems.inbound.attribution import _annotate_content_memory_refs

    refs = [{"kind": "memory_node", "id": "1", "source": "x"}]
    original = [ref.copy() for ref in refs]
    _annotate_content_memory_refs(refs, {"mutated_target_refs": [
        None, "junk", 42,
        {"kind": "memory_source", "id": 1, "role": "content", "knowledge_get": "eligible"},
        {"kind": "memory_node", "id": 1, "role": "content", "knowledge_get": "unknown"},
    ]})

    assert refs == original


@pytest.mark.parametrize("error", [RecursionError, ValueError, RuntimeError])
def test_annotation_failure_keeps_all_refs_unannotated(error):
    from brain.systems.inbound.attribution import _annotate_content_memory_refs

    class UnconvertibleId:
        def __str__(self):
            raise error("annotation conversion failed")

    refs = collect_result_refs({"cue_node_ids": [1, 2]}, source="x")
    original = [ref.copy() for ref in refs]
    annotation = {
        "kind": "memory_node", "id": 1, "role": "content",
        "visibility": "team", "knowledge_get": "eligible",
    }
    _annotate_content_memory_refs(
        refs, {"mutated_target_refs": [annotation]},
        result_refs=[{**annotation, "id": UnconvertibleId()}],
    )

    assert refs == original


def test_nested_explicit_refs_do_not_supply_annotations():
    result = {"nested": {"mutated_target_refs": [{
        "kind": "memory_node", "id": 1, "role": "content",
        "visibility": "team", "knowledge_get": "eligible",
    }]}}

    assert collect_result_refs(result, source="x") == [
        {"kind": "memory_node", "id": "1", "source": "x"},
    ]


def test_memory_result_without_explicit_annotation_matches_base_bytes():
    result = {
        "source_id": 91,
        "span_ids": [92],
        "content_node_id": 93,
        "cue_node_ids": [94, 93],
        "visibility": "team",
        "knowledge_index": {"eligible": True, "reason": None},
        "mutated_target_refs": [{"kind": "memory_node", "id": 93}],
    }
    # Frozen base output: sibling fields and duplicate refs do not annotate.
    base_bytes = (
        b'[{"kind": "memory_source", "id": "91", "source": "x"}, '
        b'{"kind": "memory_span", "id": "92", "source": "x"}, '
        b'{"kind": "memory_node", "id": "93", "source": "x"}, '
        b'{"kind": "memory_node", "id": "94", "source": "x"}]'
    )
    assert json.dumps(collect_result_refs(result, source="x")).encode() == base_bytes


def test_content_annotation_never_adds_or_reorders_refs_at_cap():
    from brain.systems.inbound.attribution import _annotate_content_memory_refs

    refs = collect_result_refs({"cue_node_ids": list(range(1, 21))}, source="x")
    original_order = [id(ref) for ref in refs]
    original_identities = [(ref["kind"], ref["id"], ref["source"]) for ref in refs]
    _annotate_content_memory_refs(refs, {"mutated_target_refs": [
        {"kind": "memory_node", "id": node_id, "role": "content",
         "visibility": "team", "knowledge_get": "eligible"}
        for node_id in (21, 20, 1)
    ]})

    assert [id(ref) for ref in refs] == original_order
    assert [(ref["kind"], ref["id"], ref["source"]) for ref in refs] == original_identities
    assert len(refs) == 20
    assert refs[0]["knowledge_get"] == refs[-1]["knowledge_get"] == "eligible"
    assert all("role" not in ref for ref in refs[1:-1])
