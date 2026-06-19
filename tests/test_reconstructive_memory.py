"""Tests for the reconstructive memory replacement primitives."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

from brain.platform.db.models.org import Org, User
from brain.platform.db.models.reconstructive_memory import (
    MemoryAssertionNode,
    MemoryEdgeNode,
    MemoryNode,
    MemoryNodeEmbedding,
    MemorySource,
    MemorySpan,
    ReconstructionEvidence,
    ReconstructionFeedback,
    ReconstructionRun,
    ReconstructionStep,
)
from brain.platform.db.repositories.reconstructive_memory import (
    AssertionDraft,
    EdgeDraft,
    MemoryAssertionRepository,
    MemoryEdgeRepository,
    MemoryNodeRepository,
    MemorySourceRepository,
    NodeDraft,
    SourceSpanDraft,
)
from brain.systems.reconstructive_memory.controller import reconstruct_memory
from brain.systems.reconstructive_memory.ingestion import ingest_memory_source


SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"
SQLiteTypeCompiler.visit_VECTOR = lambda self, type_, **kw: "TEXT"


_TEST_ORG_ID = "00000000-0000-0000-0000-000000000021"
_TEST_USER_ID = "00000000-0000-0000-0000-000000000022"
_OTHER_USER_ID = "00000000-0000-0000-0000-000000000099"


async def _session(async_sqlite_session_factory):
    session = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            MemorySource.__table__,
            MemorySpan.__table__,
            MemoryNode.__table__,
            MemoryAssertionNode.__table__,
            MemoryEdgeNode.__table__,
            ReconstructionRun.__table__,
            ReconstructionStep.__table__,
            ReconstructionEvidence.__table__,
            ReconstructionFeedback.__table__,
        ],
        enable_foreign_keys=True,
    )
    await session.execute(text("CREATE TABLE IF NOT EXISTS agent_runs (id INTEGER PRIMARY KEY)"))
    await session.execute(
        text("INSERT INTO orgs (id, name, slug) VALUES (:id, :name, :slug)"),
        {"id": _TEST_ORG_ID, "name": "Reconstructive Org", "slug": "reconstructive-org"},
    )
    await session.execute(
        text("INSERT INTO users (id, org_id, name, email) VALUES (:id, :org_id, :name, :email)"),
        {
            "id": _TEST_USER_ID,
            "org_id": _TEST_ORG_ID,
            "name": "Tester",
            "email": "reconstructive@example.com",
        },
    )
    await session.execute(
        text("INSERT INTO users (id, org_id, name, email) VALUES (:id, :org_id, :name, :email)"),
        {
            "id": _OTHER_USER_ID,
            "org_id": _TEST_ORG_ID,
            "name": "Other Tester",
            "email": "other-reconstructive@example.com",
        },
    )
    await session.commit()
    return session


def test_reconstructive_memory_models_expose_clean_slate_tables():
    assert MemorySource.__tablename__ == "memory_sources"
    assert MemorySpan.__tablename__ == "memory_spans"
    assert MemoryNode.__tablename__ == "memory_nodes"
    assert MemoryNodeEmbedding.__tablename__ == "memory_node_embeddings"
    assert MemoryEdgeNode.__tablename__ == "memory_edges"
    assert MemoryAssertionNode.__tablename__ == "memory_assertions"
    assert ReconstructionRun.__tablename__ == "reconstruction_runs"

    source_cols = {column.key for column in inspect(MemorySource).columns}
    assert {"source_kind", "content_digest", "raw_content", "authority_principal"}.issubset(source_cols)

    node_cols = {column.key for column in inspect(MemoryNode).columns}
    assert {"node_kind", "canonical_label", "normalized_key", "truth_status", "freshness_status"}.issubset(node_cols)

    run_cols = {column.key for column in inspect(ReconstructionRun).columns}
    assert {"query_text", "query_kind", "visibility_context", "policy_version", "final_confidence"}.issubset(run_cols)


def test_reconstructive_memory_tools_are_registered_for_agents():
    from brain.systems.runs.tool_catalog.definitions.brain import BRAIN_TOOLS
    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    tool_names = {tool["name"] for tool in BRAIN_TOOLS}
    assert "memory_reconstruct" in tool_names
    assert "memory_ingest_source" in tool_names
    reconstruct_registration = get_tool_registration("memory_reconstruct")
    ingest_registration = get_tool_registration("memory_ingest_source")
    assert reconstruct_registration is not None
    assert ingest_registration is not None
    assert reconstruct_registration.side_effect_class == "read_only"
    assert ingest_registration.permission == "write_memory"


async def test_source_backed_graph_can_be_written_and_reconstructed(async_sqlite_session_factory):
    session = await _session(async_sqlite_session_factory)
    source_repo = MemorySourceRepository(session)
    node_repo = MemoryNodeRepository(session)
    edge_repo = MemoryEdgeRepository(session)
    assertion_repo = MemoryAssertionRepository(session)

    source, spans = await source_repo.create_with_spans(
        source_kind="thread_message",
        source_ref="thread:memory-rewrite#1",
        raw_content="We decided to replace brain_recall with memory_reconstruct for active evidence reconstruction.",
        spans=[
            SourceSpanDraft(
                text="We decided to replace brain_recall with memory_reconstruct for active evidence reconstruction.",
                locator={"message_index": 0},
            )
        ],
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
        authority_principal="user:test",
    )
    content = await node_repo.upsert_node(
        draft=NodeDraft(
            node_kind="content",
            content_kind="decision",
            canonical_label="Replace brain_recall with memory_reconstruct",
            text="Illo should replace brain_recall with memory_reconstruct.",
            confidence=0.9,
            truth_status="active",
            freshness_status="fresh",
        ),
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
    )
    cue = await node_repo.upsert_node(
        draft=NodeDraft(node_kind="cue", canonical_label="brain_recall", confidence=0.8),
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
    )
    await edge_repo.upsert_edge(
        draft=EdgeDraft(
            source_node_id=cue.id,
            target_node_id=content.id,
            edge_kind="cue_to_content",
            evidence_span_ids=(spans[0].id,),
        ),
        org_id=_TEST_ORG_ID,
        visibility="org",
    )
    assertion = await assertion_repo.create_assertion(
        draft=AssertionDraft(
            node_id=content.id,
            claim_text="Illo should replace brain_recall with memory_reconstruct.",
            confidence=0.9,
            truth_status="active",
            source_span_ids=(spans[0].id,),
        )
    )
    await session.flush()

    pack = await reconstruct_memory(
        session,
        query="What replaces brain_recall?",
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
    )

    assert source.id is not None
    assert pack.confidence == 0.9
    assert pack.supporting_evidence[0].node_id == content.id
    assert pack.supporting_evidence[0].assertion_id == assertion.id
    assert pack.supporting_evidence[0].source_span_id == spans[0].id
    assert "memory_reconstruct" in pack.supporting_evidence[0].text
    assert [step.action_kind for step in pack.trajectory] == ["seed_cues", "summarize_evidence"]

    runs = (await session.execute(text("SELECT status, final_confidence FROM reconstruction_runs"))).all()
    assert runs == [("completed", 0.9)]
    evidence_rows = (await session.execute(text("SELECT node_id, assertion_id, source_span_id FROM reconstruction_evidence"))).all()
    assert evidence_rows == [(content.id, assertion.id, spans[0].id)]


async def test_private_nodes_do_not_cross_user_visibility(async_sqlite_session_factory):
    session = await _session(async_sqlite_session_factory)
    node_repo = MemoryNodeRepository(session)
    await node_repo.upsert_node(
        draft=NodeDraft(
            node_kind="content",
            content_kind="fact",
            canonical_label="Private migration fact",
            text="Private migration fact should not leak.",
            confidence=0.7,
        ),
        org_id=_TEST_ORG_ID,
        user_id=_OTHER_USER_ID,
        visibility="private",
    )
    await session.flush()

    pack = await reconstruct_memory(
        session,
        query="Private migration fact",
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
    )

    assert pack.supporting_evidence == ()
    assert pack.confidence == 0.0
    assert pack.unresolved_questions == ("No source-backed evidence found for: Private migration fact",)


async def test_mcp_memory_reconstruct_returns_evidence_pack(async_sqlite_session_factory, monkeypatch):
    session = await _session(async_sqlite_session_factory)
    source_repo = MemorySourceRepository(session)
    node_repo = MemoryNodeRepository(session)
    assertion_repo = MemoryAssertionRepository(session)

    _, spans = await source_repo.create_with_spans(
        source_kind="inbound_submission",
        raw_content="The replacement memory tool is memory_reconstruct.",
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
    )
    node = await node_repo.upsert_node(
        draft=NodeDraft(
            node_kind="content",
            content_kind="fact",
            canonical_label="memory_reconstruct replacement tool",
            text="The replacement memory tool is memory_reconstruct.",
            confidence=0.82,
            truth_status="active",
        ),
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
    )
    await assertion_repo.create_assertion(
        draft=AssertionDraft(
            node_id=node.id,
            claim_text="The replacement memory tool is memory_reconstruct.",
            confidence=0.82,
            truth_status="active",
            source_span_ids=(spans[0].id,),
        )
    )
    await session.flush()

    class _PatchedUnitOfWork:
        async def __aenter__(self):
            self.session = session
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if exc_type is None:
                await session.flush()
            return False

    from brain.app.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "UnitOfWork", _PatchedUnitOfWork)
    payload = await mcp_server.async_tool_memory_reconstruct(
        query="replacement memory tool",
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
    )

    assert payload["confidence"] == 0.82
    assert payload["supporting_evidence"][0]["source_span_id"] == spans[0].id
    assert payload["trajectory"][0]["action_kind"] == "seed_cues"


async def test_brain_recall_compatibility_alias_reads_reconstructive_memory_without_legacy_table(
    async_sqlite_session_factory,
    monkeypatch,
):
    session = await _session(async_sqlite_session_factory)
    await ingest_memory_source(
        session,
        content="brain_recall now returns evidence packs from reconstructive memory.",
        content_kind="fact",
        source_kind="manual_note",
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
        confidence=0.81,
    )
    await session.flush()

    class _PatchedUnitOfWork:
        async def __aenter__(self):
            self.session = session
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if exc_type is None:
                await session.flush()
            return False

    from brain.app.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "UnitOfWork", _PatchedUnitOfWork)
    payload = await mcp_server.async_tool_brain_recall(
        query="evidence packs from reconstructive memory",
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
    )

    assert payload["compatibility_alias"] == "brain_recall"
    assert payload["memory_system"] == "reconstructive"
    assert payload["evidence_pack"]["confidence"] == 0.81
    assert payload["memories"][0]["memory_system"] == "reconstructive"
    assert payload["memories"][0]["source_span_id"] is not None

    memory_table_exists = await session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='memories'")
    )
    assert memory_table_exists.first() is None


async def test_ingest_memory_source_creates_reconstructable_graph(async_sqlite_session_factory):
    session = await _session(async_sqlite_session_factory)

    ingested = await ingest_memory_source(
        session,
        content="Always cite source spans when answering from team memory.",
        content_kind="procedure",
        source_kind="manual_note",
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
        confidence=0.88,
    )
    await session.flush()

    assert ingested.source_id is not None
    assert ingested.assertion_id is not None
    assert ingested.cue_node_ids
    assert ingested.tag_node_ids
    assert ingested.edge_ids

    pack = await reconstruct_memory(
        session,
        query="cite source spans",
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
    )
    assert pack.confidence == 0.88
    assert "cite source spans" in pack.supporting_evidence[0].text.lower()


async def test_brain_encode_compatibility_alias_writes_reconstructive_memory(async_sqlite_session_factory, monkeypatch):
    session = await _session(async_sqlite_session_factory)

    class _PatchedUnitOfWork:
        async def __aenter__(self):
            self.session = session
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if exc_type is None:
                await session.flush()
            return False

    from brain.app.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "UnitOfWork", _PatchedUnitOfWork)
    payload = await mcp_server.async_tool_brain_encode(
        content="The old brain_encode tool now ingests reconstructive memory sources.",
        memory_type="fact",
        salience=9.0,
        user_id=_TEST_USER_ID,
        org_id=_TEST_ORG_ID,
        visibility="org",
        run_id=123,
        confidence=0.77,
    )

    assert payload["compatibility_alias"] == "brain_encode"
    assert payload["memory_system"] == "reconstructive"
    assert payload["source_id"] is not None
    assert payload["content_node_id"] is not None

    memory_table_exists = await session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='memories'")
    )
    assert memory_table_exists.first() is None
