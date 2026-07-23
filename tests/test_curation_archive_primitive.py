"""Archive curation entry points share one audit-and-archive primitive."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import brain.systems.reconstructive_memory.curation as curation

pytestmark = pytest.mark.asyncio


def _record_archive_repository_calls(monkeypatch, *, source_id, span_id):
    calls: list[tuple[str, object]] = []
    source = SimpleNamespace(id=source_id)
    spans = [SimpleNamespace(id=span_id)]

    class SourceRepository:
        def __init__(self, session):
            pass

        async def create_with_spans(self, **kwargs):
            calls.append(("create_with_spans", kwargs))
            return source, spans

    class CompatibilityRepository:
        def __init__(self, session):
            pass

        async def archive_many(self, node_ids):
            calls.append(("archive_many", node_ids))

    monkeypatch.setattr(curation, "MemorySourceRepository", SourceRepository)
    monkeypatch.setattr(
        curation,
        "ReconstructiveMemoryCompatibilityRepository",
        CompatibilityRepository,
    )
    return calls


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
    monkeypatch.setattr(curation.uuid, "uuid4", lambda: "evidence-uuid")
    calls = _record_archive_repository_calls(
        monkeypatch,
        source_id=41,
        span_id=42,
    )

    session = SimpleNamespace()
    result = await curation.archive_memories(
        session,
        node_ids=[7, 7, 9],
        reason="  Obsolete   duplicate memory. ",
        user_id="user:test",
        org_id="org:test",
        run_id=123,
    )

    assert calls == [
        (
            "create_with_spans",
            {
                "source_kind": curation.CURATION_CREATED_BY,
                "source_ref": "123:archive:evidence-uuid",
                "raw_content": "Obsolete duplicate memory.",
                "spans": [
                    curation.SourceSpanDraft(
                        text="Obsolete duplicate memory.",
                        locator={"kind": "curation_reason", "action": "archive"},
                    )
                ],
                "org_id": "org:test",
                "user_id": "user:test",
                "visibility": "private",
                "structured_payload": {
                    "action": "archive",
                    "reason": "Obsolete duplicate memory.",
                    "created_by": curation.CURATION_CREATED_BY,
                    "run_id": "123",
                    "node_ids": [7, 9],
                },
                "authority_principal": "user:test",
                "sensitivity": "low",
            },
        ),
        ("archive_many", [7, 9]),
    ]
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
    calls = _record_archive_repository_calls(
        monkeypatch,
        source_id=51,
        span_id=52,
    )
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

    audit_text = (
        "Rule: expired transient fact. Policy version: v1. Target node: 17. "
        "Run ID: 456. Reason: Expired by policy."
    )
    assert calls == [
        (
            "create_with_spans",
            {
                "source_kind": curation.MEMORY_CURATOR_SOURCE_KIND,
                "source_ref": "nightly-memory-maintenance:456:archive:17",
                "raw_content": audit_text,
                "spans": [
                    curation.SourceSpanDraft(
                        text=audit_text,
                        locator={
                            "kind": "memory_curation",
                            "action": "archive",
                            "rule": "expired transient fact",
                            "policy_version": "v1",
                            "target_node": 17,
                            "run_id": "456",
                        },
                    )
                ],
                "org_id": "org:test",
                "user_id": "user:test",
                "visibility": "team",
                "structured_payload": {
                    "action": "archive",
                    "rule": "expired transient fact",
                    "policy_version": "v1",
                    "target_node": 17,
                    "run_id": "456",
                    "reason": "Expired by policy.",
                    "created_by": curation.MEMORY_CURATOR_SOURCE_KIND,
                },
                "authority_principal": curation.MEMORY_CURATOR_SOURCE_KIND,
                "sensitivity": "high",
            },
        ),
        ("archive_many", [17]),
    ]
    assert result == {
        "node_id": 17,
        "curation_source_id": 51,
        "curation_span_id": 52,
    }


async def test_link_persists_the_shared_agent_curation_evidence_contract(monkeypatch):
    monkeypatch.setattr(
        curation,
        "_visible_nodes_exact",
        AsyncMock(
            return_value=[
                SimpleNamespace(visibility="org"),
                SimpleNamespace(visibility="team"),
            ]
        ),
    )
    monkeypatch.setattr(curation.uuid, "uuid4", lambda: "link-uuid")
    source_repository = SimpleNamespace(
        create_with_spans=AsyncMock(
            return_value=(SimpleNamespace(id=61), [SimpleNamespace(id=62)])
        )
    )
    edge_repository = SimpleNamespace(
        upsert_edge=AsyncMock(
            return_value=SimpleNamespace(id=63, created_by=curation.CURATION_CREATED_BY)
        )
    )
    monkeypatch.setattr(
        curation,
        "MemorySourceRepository",
        lambda session: source_repository,
    )
    monkeypatch.setattr(
        curation,
        "MemoryEdgeRepository",
        lambda session: edge_repository,
    )

    await curation.link_memories(
        SimpleNamespace(),
        source_node_id=7,
        target_node_id=9,
        relationship=" supports guidance ",
        reason="  Same   verified guidance. ",
        user_id="user:test",
        org_id="org:test",
        run_id=234,
    )

    source_repository.create_with_spans.assert_awaited_once_with(
        source_kind=curation.CURATION_CREATED_BY,
        source_ref="234:link:link-uuid",
        raw_content="Same verified guidance.",
        spans=[
            curation.SourceSpanDraft(
                text="Same verified guidance.",
                locator={"kind": "curation_reason", "action": "link"},
            )
        ],
        org_id="org:test",
        user_id="user:test",
        visibility="team",
        structured_payload={
            "action": "link",
            "reason": "Same verified guidance.",
            "created_by": curation.CURATION_CREATED_BY,
            "run_id": "234",
            "source_node": 7,
            "target_node": 9,
            "relationship": "supports_guidance",
        },
        authority_principal="user:test",
    )


async def test_supersede_persists_the_shared_agent_curation_evidence_contract(monkeypatch):
    old_node = SimpleNamespace(visibility="org")
    monkeypatch.setattr(
        curation,
        "_visible_nodes_exact",
        AsyncMock(
            side_effect=[
                [old_node],
                [old_node, SimpleNamespace(visibility="private")],
            ]
        ),
    )
    monkeypatch.setattr(curation.uuid, "uuid4", lambda: "supersede-uuid")
    source_repository = SimpleNamespace(
        create_with_spans=AsyncMock(
            return_value=(SimpleNamespace(id=71), [SimpleNamespace(id=72)])
        )
    )
    node_repository = SimpleNamespace(mark_superseded=AsyncMock())
    assertion_repository = SimpleNamespace(mark_superseded_for_node=AsyncMock())
    edge_repository = SimpleNamespace(
        upsert_edge=AsyncMock(
            return_value=SimpleNamespace(id=73, created_by=curation.CURATION_CREATED_BY)
        )
    )
    monkeypatch.setattr(
        curation,
        "MemorySourceRepository",
        lambda session: source_repository,
    )
    monkeypatch.setattr(
        curation,
        "MemoryNodeRepository",
        lambda session: node_repository,
    )
    monkeypatch.setattr(
        curation,
        "MemoryAssertionRepository",
        lambda session: assertion_repository,
    )
    monkeypatch.setattr(
        curation,
        "MemoryEdgeRepository",
        lambda session: edge_repository,
    )
    session = SimpleNamespace()
    session.scalar = AsyncMock(return_value=None)

    await curation.supersede_memory(
        session,
        old_node_id=23,
        new_node_id=29,
        reason="  Replaced   by verified guidance. ",
        user_id="user:test",
        org_id="org:test",
        run_id=None,
    )

    source_repository.create_with_spans.assert_awaited_once_with(
        source_kind=curation.CURATION_CREATED_BY,
        source_ref="direct:supersede:supersede-uuid",
        raw_content="Replaced by verified guidance.",
        spans=[
            curation.SourceSpanDraft(
                text="Replaced by verified guidance.",
                locator={"kind": "curation_reason", "action": "supersede"},
            )
        ],
        org_id="org:test",
        user_id="user:test",
        visibility="private",
        structured_payload={
            "action": "supersede",
            "reason": "Replaced by verified guidance.",
            "created_by": curation.CURATION_CREATED_BY,
            "run_id": None,
            "old_node": 23,
            "new_node": 29,
        },
        authority_principal="user:test",
    )
