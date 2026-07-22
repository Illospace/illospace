"""Archive curation entry points share one audit-and-archive primitive."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import brain.systems.reconstructive_memory.curation as curation

pytestmark = pytest.mark.asyncio


async def test_user_archive_supplies_its_existing_audit_contract(monkeypatch):
    visible_nodes = [
        SimpleNamespace(visibility="org"),
        SimpleNamespace(visibility="private"),
    ]
    monkeypatch.setattr(
        curation,
        "_visible_nodes_exact",
        AsyncMock(return_value=visible_nodes),
    )
    archive_with_curation = AsyncMock(
        return_value=(SimpleNamespace(id=41), [SimpleNamespace(id=42)])
    )
    monkeypatch.setattr(curation, "_archive_with_curation", archive_with_curation)

    result = await curation.archive_memories(
        SimpleNamespace(),
        node_ids=[7, 7, 9],
        reason="  Obsolete   duplicate memory. ",
        user_id="user:test",
        org_id="org:test",
        run_id=123,
    )

    context = archive_with_curation.await_args.kwargs["context"]
    assert context.node_ids == [7, 9]
    assert context.source_kind == curation.CURATION_CREATED_BY
    assert context.source_ref.startswith("123:archive:")
    assert context.raw_content == "Obsolete duplicate memory."
    assert context.span_drafts[0].text == context.raw_content
    assert context.span_drafts[0].locator == {
        "kind": "curation_reason",
        "action": "archive",
    }
    assert context.structured_payload == {
        "action": "archive",
        "reason": "Obsolete duplicate memory.",
        "created_by": curation.CURATION_CREATED_BY,
        "run_id": "123",
        "node_ids": [7, 9],
    }
    assert context.user_id == "user:test"
    assert context.org_id == "org:test"
    assert context.authority_principal == "user:test"
    assert context.visibility == "private"
    assert context.sensitivity == "low"
    assert result == {
        "memory_system": "reconstructive",
        "action": "archive",
        "node_ids": [7, 9],
        "archived_count": 2,
        "created_by": curation.CURATION_CREATED_BY,
        "reason": "Obsolete duplicate memory.",
        "curation_source_id": 41,
    }


async def test_policy_archive_supplies_its_existing_audit_contract(monkeypatch):
    archive_with_curation = AsyncMock(
        return_value=(SimpleNamespace(id=51), [SimpleNamespace(id=52)])
    )
    monkeypatch.setattr(curation, "_archive_with_curation", archive_with_curation)
    node = SimpleNamespace(
        id=17,
        archived_at=None,
        org_id="org:test",
        user_id="user:test",
        visibility="team",
        sensitivity="high",
    )

    result = await curation.archive_memory_by_policy(
        SimpleNamespace(),
        node=node,
        rule=" expired transient fact ",
        policy_version=" v1 ",
        run_id=456,
        reason="  Expired   by policy. ",
    )

    context = archive_with_curation.await_args.kwargs["context"]
    assert context.node_ids == [17]
    assert context.source_kind == curation.MEMORY_CURATOR_SOURCE_KIND
    assert context.source_ref == "nightly-memory-maintenance:456:archive:17"
    assert context.raw_content == (
        "Rule: expired transient fact. Policy version: v1. Target node: 17. "
        "Run ID: 456. Reason: Expired by policy."
    )
    assert context.span_drafts[0].text == context.raw_content
    assert context.span_drafts[0].locator == {
        "kind": "memory_curation",
        "action": "archive",
        "rule": "expired transient fact",
        "policy_version": "v1",
        "target_node": 17,
        "run_id": "456",
    }
    assert context.structured_payload == {
        "action": "archive",
        "rule": "expired transient fact",
        "policy_version": "v1",
        "target_node": 17,
        "run_id": "456",
        "reason": "Expired by policy.",
        "created_by": curation.MEMORY_CURATOR_SOURCE_KIND,
    }
    assert context.user_id == "user:test"
    assert context.org_id == "org:test"
    assert context.authority_principal == curation.MEMORY_CURATOR_SOURCE_KIND
    assert context.visibility == "team"
    assert context.sensitivity == "high"
    assert result == {
        "node_id": 17,
        "curation_source_id": 51,
        "curation_span_id": 52,
    }


async def test_archive_primitive_records_evidence_before_soft_archive(monkeypatch):
    calls: list[tuple[str, object]] = []
    source = SimpleNamespace(id=61)
    spans = [SimpleNamespace(id=62)]

    class SourceRepository:
        def __init__(self, session):
            calls.append(("source_repository", session))

        async def create_with_spans(self, **kwargs):
            calls.append(("create_with_spans", kwargs))
            return source, spans

    class CompatibilityRepository:
        def __init__(self, session):
            calls.append(("compatibility_repository", session))

        async def archive_many(self, node_ids):
            calls.append(("archive_many", node_ids))

    monkeypatch.setattr(curation, "MemorySourceRepository", SourceRepository)
    monkeypatch.setattr(
        curation,
        "ReconstructiveMemoryCompatibilityRepository",
        CompatibilityRepository,
    )
    session = SimpleNamespace()
    context = curation._ArchiveAuditContext(
        node_ids=[23],
        source_kind="audit-kind",
        source_ref="audit-ref",
        raw_content="audit text",
        span_drafts=[],
        structured_payload={"action": "archive"},
        user_id="user:test",
        org_id="org:test",
        authority_principal="actor:test",
        visibility="org",
        sensitivity="low",
    )

    result = await curation._archive_with_curation(session, context=context)

    assert result == (source, spans)
    assert calls == [
        ("source_repository", session),
        (
            "create_with_spans",
            {
                "source_kind": "audit-kind",
                "source_ref": "audit-ref",
                "raw_content": "audit text",
                "spans": [],
                "org_id": "org:test",
                "user_id": "user:test",
                "visibility": "org",
                "structured_payload": {"action": "archive"},
                "authority_principal": "actor:test",
                "sensitivity": "low",
            },
        ),
        ("compatibility_repository", session),
        ("archive_many", [23]),
    ]
