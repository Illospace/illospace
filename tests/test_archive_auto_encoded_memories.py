"""Tests for the one-off auto-encoded memory cleanup script."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

from brain.platform.db.models.reconstructive_memory import (
    MemoryAssertionNode,
    MemoryEdgeNode,
    MemoryNode,
    MemorySource,
    MemorySpan,
)
from scripts.archive_auto_encoded_memories import archive_auto_encoded_memories

SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"


def _node(node_kind: str, label: str, *, text: str | None = None) -> MemoryNode:
    return MemoryNode(
        node_kind=node_kind,
        content_kind=(
            node_kind
            if node_kind in {"content", "summary", "procedure", "policy"}
            else None
        ),
        canonical_label=label,
        text=text,
        normalized_key=label.lower(),
        scope_key="default",
        visibility="private",
    )


def _edge(source: MemoryNode, target: MemoryNode, edge_kind: str, span_id: int) -> MemoryEdgeNode:
    return MemoryEdgeNode(
        source_node_id=source.id,
        target_node_id=target.id,
        edge_kind=edge_kind,
        visibility="private",
        evidence_span_ids=[span_id],
    )


async def test_archive_is_safe_idempotent_and_retains_provenance(
    async_sqlite_session_factory,
):
    session = await async_sqlite_session_factory(
        [
            MemorySource.__table__,
            MemorySpan.__table__,
            MemoryNode.__table__,
            MemoryAssertionNode.__table__,
            MemoryEdgeNode.__table__,
        ]
    )
    source = MemorySource(
        visibility="private",
        source_kind="agent_auto_encode",
        source_ref="session-1",
        content_digest="source-digest",
        raw_content="[auto-encoded] Routine completion snapshot",
    )
    session.add(source)
    await session.flush()
    span = MemorySpan(
        source_id=source.id,
        text="[auto-encoded] Routine completion snapshot",
        content_digest="span-digest",
    )
    session.add(span)
    await session.flush()

    exhaust_one = _node(
        "content",
        "auto snapshot one",
        text="[auto-encoded] Routine completion snapshot",
    )
    exhaust_two = _node(
        "content",
        "auto snapshot two",
        text="[auto-encoded] Another stale completion snapshot",
    )
    durable_content = _node(
        "content",
        "durable lesson",
        text="Learned: validate the source contract before changing it.",
    )
    durable_summary = _node(
        "summary",
        "durable project summary",
        text="The current project summary still uses the shared routing nodes.",
    )
    orphaned_cue = _node("cue", "routine completion")
    shared_cue = _node("cue", "source contract")
    orphaned_tag = _node("tag", "episode")
    shared_tag = _node("tag", "lesson")
    session.add_all(
        [
            exhaust_one,
            exhaust_two,
            durable_content,
            durable_summary,
            orphaned_cue,
            shared_cue,
            orphaned_tag,
            shared_tag,
        ]
    )
    await session.flush()
    assertion = MemoryAssertionNode(
        node_id=exhaust_one.id,
        claim_text=exhaust_one.text,
        source_span_ids=[span.id],
    )
    session.add(assertion)
    session.add_all(
        [
            _edge(orphaned_tag, exhaust_one, "tag_to_content", span.id),
            _edge(shared_tag, exhaust_one, "tag_to_content", span.id),
            _edge(shared_tag, durable_summary, "tag_to_content", span.id),
            _edge(exhaust_one, orphaned_cue, "content_to_cue", span.id),
            _edge(exhaust_one, shared_cue, "content_to_cue", span.id),
            _edge(durable_summary, shared_cue, "content_to_cue", span.id),
            _edge(orphaned_cue, orphaned_tag, "cue_to_tag", span.id),
        ]
    )
    await session.flush()

    provenance_counts = {
        model: await session.scalar(select(func.count()).select_from(model))
        for model in (MemorySource, MemorySpan, MemoryAssertionNode, MemoryEdgeNode)
    }

    dry_run = await archive_auto_encoded_memories(session)
    assert dry_run.report(applied=False) == {
        "applied": False,
        "archived": 0,
        "would_archive": 4,
        "content_nodes": 2,
        "orphaned_cue_nodes": 1,
        "orphaned_tag_nodes": 1,
    }
    assert not any(
        node.archived_at
        for node in (
            exhaust_one,
            exhaust_two,
            durable_content,
            durable_summary,
            orphaned_cue,
            shared_cue,
            orphaned_tag,
            shared_tag,
        )
    )

    applied = await archive_auto_encoded_memories(session, apply=True)
    await session.refresh(exhaust_one)
    await session.refresh(exhaust_two)
    await session.refresh(durable_content)
    await session.refresh(durable_summary)
    await session.refresh(orphaned_cue)
    await session.refresh(shared_cue)
    await session.refresh(orphaned_tag)
    await session.refresh(shared_tag)

    assert applied.report(applied=True)["archived"] == 4
    assert exhaust_one.archived_at is not None
    assert exhaust_two.archived_at is not None
    assert orphaned_cue.archived_at is not None
    assert orphaned_tag.archived_at is not None
    assert durable_content.archived_at is None
    assert durable_summary.archived_at is None
    assert shared_cue.archived_at is None
    assert shared_tag.archived_at is None
    assert {
        model: await session.scalar(select(func.count()).select_from(model))
        for model in (MemorySource, MemorySpan, MemoryAssertionNode, MemoryEdgeNode)
    } == provenance_counts

    rerun = await archive_auto_encoded_memories(session, apply=True)
    assert rerun.report(applied=True)["archived"] == 0
