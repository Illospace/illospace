"""Tests for the reconstructive memory replacement primitives."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from sqlalchemy import inspect, select, text
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
from brain.platform.db.models.system import RetrievalLog
from brain.platform.db.repositories.reconstructive_memory import (
    AssertionDraft,
    EdgeDraft,
    MemoryAssertionRepository,
    MemoryEdgeRepository,
    MemoryNodeEmbeddingRepository,
    MemoryNodeRepository,
    MemorySourceRepository,
    NodeDraft,
    ReconstructiveMemoryCompatibilityRepository,
    SourceSpanDraft,
)
from brain.systems.reconstructive_memory.controller import reconstruct_memory
from brain.systems.reconstructive_memory.embeddings import (
    backfill_memory_node_embeddings,
    embed_recall_query,
    embedding_model_identity,
)
from brain.systems.reconstructive_memory.ingestion import ingest_memory_source
from brain.systems.runtime_settings.memory import EmbeddingRuntimeConfig
from brain.kernel.config import MEMORY_SEMANTIC_EMBEDDING_DIM


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
            RetrievalLog.__table__,
            MemoryNodeEmbedding.__table__,
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


def _runtime_config() -> EmbeddingRuntimeConfig:
    return EmbeddingRuntimeConfig(
        backend="api",
        provider="gemini",
        api_model="test-reconstructive-embedding",
        cpu_model="unused",
        dimensions=MEMORY_SEMANTIC_EMBEDDING_DIM,
        api_key="test-key",
    )


def _unit_vector(axis: int) -> np.ndarray:
    vector = np.zeros(MEMORY_SEMANTIC_EMBEDDING_DIM, dtype=np.float32)
    vector[axis] = 1.0
    return vector


def _mock_embedding_runtime(monkeypatch, *, query_axis: int = 0, document_axis: int = 0) -> EmbeddingRuntimeConfig:
    from brain.systems.memory import embeddings as embedding_client
    from brain.systems.runtime_settings import memory as runtime_settings

    runtime = _runtime_config()

    async def fake_runtime_config(session, *, include_secret=True):
        del session, include_secret
        return runtime

    monkeypatch.setattr(runtime_settings, "async_get_embedding_runtime_config", fake_runtime_config)
    monkeypatch.setattr(
        embedding_client,
        "embed_query",
        lambda text, runtime_config=None: _unit_vector(query_axis),
    )
    monkeypatch.setattr(
        embedding_client,
        "embed_document",
        lambda text, runtime_config=None: _unit_vector(document_axis),
    )
    return runtime


async def _search_content_nodes(session, node_repo, *, query: str):
    query_embedding = await embed_recall_query(session, query)
    assert query_embedding is not None
    return await node_repo.search_content_nodes(
        query=query,
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        query_embedding=query_embedding.vector,
        embedding_model=query_embedding.model,
    )


@pytest.fixture(autouse=True)
def _prevent_live_embeddings(monkeypatch):
    from brain.systems.memory import embeddings as embedding_client
    from brain.systems.runtime_settings import memory as runtime_settings

    async def fake_runtime_config(session, *, include_secret=True):
        del session, include_secret
        return _runtime_config()

    def unavailable(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("embedding backend intentionally unavailable in unit test")

    monkeypatch.setattr(runtime_settings, "async_get_embedding_runtime_config", fake_runtime_config)
    monkeypatch.setattr(embedding_client, "embed_query", unavailable)
    monkeypatch.setattr(embedding_client, "embed_document", unavailable)


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
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    tool_names = {tool["name"] for tool in BRAIN_TOOLS}
    handlers = _get_tool_handlers()
    assert "memory_reconstruct" in tool_names
    assert "memory_ingest_source" in tool_names
    assert {"memory_link", "memory_supersede", "memory_archive"} <= tool_names
    reconstruct_registration = get_tool_registration("memory_reconstruct")
    ingest_registration = get_tool_registration("memory_ingest_source")
    assert reconstruct_registration is not None
    assert ingest_registration is not None
    assert reconstruct_registration.side_effect_class == "read_only"
    assert ingest_registration.permission == "write_memory"
    for tool_name in ("memory_link", "memory_supersede", "memory_archive"):
        assert tool_name in handlers
        registration = get_tool_registration(tool_name)
        assert registration is not None
        assert registration.permission == "write_memory"
        assert registration.side_effect_class == "memory_curation"
        assert registration.output_budget_chars == 4_000


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
    assert pack.confidence == pack.supporting_evidence[0].confidence
    assert pack.confidence != assertion.confidence
    assert pack.supporting_evidence[0].node_id == content.id
    assert pack.supporting_evidence[0].assertion_id == assertion.id
    assert pack.supporting_evidence[0].source_span_id == spans[0].id
    assert "memory_reconstruct" in pack.supporting_evidence[0].text
    assert [step.action_kind for step in pack.trajectory] == ["seed_cues", "summarize_evidence"]

    runs = (await session.execute(text("SELECT status, final_confidence FROM reconstruction_runs"))).all()
    assert runs == [("completed", pack.confidence)]
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


async def test_semantic_match_outranks_newer_high_confidence_off_topic_node(
    async_sqlite_session_factory,
    monkeypatch,
):
    session = await _session(async_sqlite_session_factory)
    runtime = _mock_embedding_runtime(monkeypatch, query_axis=0)
    node_repo = MemoryNodeRepository(session)
    embedding_repo = MemoryNodeEmbeddingRepository(session)

    relevant = await node_repo.upsert_node(
        draft=NodeDraft(
            node_kind="content",
            content_kind="project_fact",
            canonical_label="Luxury retail client pilot",
            text="The client pilot covered a sensor-assisted fitting experience.",
            confidence=0.2,
        ),
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
    )
    off_topic = await node_repo.upsert_node(
        draft=NodeDraft(
            node_kind="content",
            content_kind="receipt",
            canonical_label="Uwear staging deploy receipt",
            text="The staging deployment completed successfully.",
            confidence=0.99,
        ),
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
    )
    off_topic.updated_at = datetime.now(timezone.utc)
    relevant.updated_at = off_topic.updated_at - timedelta(days=30)
    for node, vector in ((relevant, _unit_vector(0)), (off_topic, _unit_vector(1))):
        await embedding_repo.create(
            node_id=node.id,
            embedding_kind="content",
            model=embedding_model_identity(runtime),
            dimension=MEMORY_SEMANTIC_EMBEDDING_DIM,
            embedding=vector.tolist(),
            content_digest=embedding_repo.content_digest(node.text or node.canonical_label),
        )

    results = await _search_content_nodes(
        session,
        node_repo,
        query="What happened with the Aritzia account?",
    )

    assert [node.id for node in results] == [relevant.id]
    assert results[0].retrieval_score > 0.7
    assert results[0].semantic_score == 1.0


async def test_exact_term_node_without_embedding_remains_findable(
    async_sqlite_session_factory,
    monkeypatch,
):
    session = await _session(async_sqlite_session_factory)
    _mock_embedding_runtime(monkeypatch, query_axis=2)
    node_repo = MemoryNodeRepository(session)
    exact = await node_repo.upsert_node(
        draft=NodeDraft(
            node_kind="content",
            content_kind="fact",
            canonical_label="axel-havard",
            text="Axel's GitHub handle is axel-havard.",
            confidence=0.4,
        ),
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
    )

    results = await _search_content_nodes(
        session,
        node_repo,
        query="axel-havard",
    )

    assert [node.id for node in results] == [exact.id]
    assert results[0].semantic_score is None
    assert results[0].lexical_score == 1.0
    assert results[0].retrieval_score == 0.97


async def test_nonsense_query_does_not_fall_back_to_newest_confident_nodes(
    async_sqlite_session_factory,
    monkeypatch,
):
    session = await _session(async_sqlite_session_factory)
    runtime = _mock_embedding_runtime(monkeypatch, query_axis=2)
    node_repo = MemoryNodeRepository(session)
    embedding_repo = MemoryNodeEmbeddingRepository(session)
    for index, label in enumerate(("Newest staging receipt", "Old project decision")):
        node = await node_repo.upsert_node(
            draft=NodeDraft(
                node_kind="content",
                content_kind="fact",
                canonical_label=label,
                text=label,
                confidence=0.99 - index * 0.1,
            ),
            org_id=_TEST_ORG_ID,
            user_id=_TEST_USER_ID,
            visibility="org",
        )
        await embedding_repo.create(
            node_id=node.id,
            embedding_kind="content",
            model=embedding_model_identity(runtime),
            dimension=MEMORY_SEMANTIC_EMBEDDING_DIM,
            embedding=_unit_vector(index).tolist(),
            content_digest=embedding_repo.content_digest(node.text or node.canonical_label),
        )

    results = await _search_content_nodes(
        session,
        node_repo,
        query="zqxjkv blorptastic",
    )

    assert results == []


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

    assert payload["confidence"] == payload["supporting_evidence"][0]["confidence"]
    assert payload["confidence"] != 0.82
    assert payload["supporting_evidence"][0]["source_span_id"] == spans[0].id
    assert payload["trajectory"][0]["action_kind"] == "seed_cues"

    retrieval_log = (
        await session.scalars(
            select(RetrievalLog).where(RetrievalLog.query_text == "replacement memory tool")
        )
    ).one()
    assert retrieval_log.was_relevant is True
    assert retrieval_log.feedback == "hit"
    assert retrieval_log.top_result_id == node.id
    assert retrieval_log.top_score == payload["supporting_evidence"][0]["confidence"]
    feedback_rows = list(
        (
            await session.scalars(
                select(ReconstructionFeedback).where(
                    ReconstructionFeedback.reconstruction_run_id == payload["reconstruction_run_id"]
                )
            )
        ).all()
    )
    assert len(feedback_rows) >= 1
    assert feedback_rows[0].target_node_id == node.id
    assert feedback_rows[0].signal_kind == "selected_evidence_proxy"
    assert feedback_rows[0].details["proxy"] == "selected_into_completed_evidence_pack"
    assert feedback_rows[0].details["retrieval_log_id"] == retrieval_log.id


async def test_agent_memory_curation_tools_persist_provenance_and_hide_superseded_memory(
    async_sqlite_session_factory,
    monkeypatch,
):
    session = await _session(async_sqlite_session_factory)
    ingested = []
    for content, kind in (
        ("Related checkout guidance describes the supported customer flow.", "procedure"),
        ("Related payment guidance describes the supported customer flow.", "procedure"),
        ("Widget deployment guidance says to use the retired legacy rollout path.", "procedure"),
        ("Obsolete widget deployment note has no continuing operational value.", "fact"),
    ):
        ingested.append(
            await ingest_memory_source(
                session,
                content=content,
                content_kind=kind,
                source_kind="manual_note",
                org_id=_TEST_ORG_ID,
                user_id=_TEST_USER_ID,
                visibility="org",
                confidence=0.8,
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
            else:
                await session.rollback()
            return False

    from brain.app.mcp import server as mcp_server
    from brain.systems.runs.execution_context import AgentExecutionContext, bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.common import _wrap_memory_curation

    monkeypatch.setattr(mcp_server, "UnitOfWork", _PatchedUnitOfWork)
    with bind_agent_context(
        AgentExecutionContext(
            user_id=_TEST_USER_ID,
            org_id=_TEST_ORG_ID,
            run=SimpleNamespace(run_id=346),
        )
    ):
        link_result = await _wrap_memory_curation(mcp_server.async_tool_memory_link)(
            source_node=ingested[0].content_node_id,
            target_node=ingested[1].content_node_id,
            relationship="supports guidance",
            reason="Both memories govern the same supported customer flow.",
        )
        supersede_result = await _wrap_memory_curation(mcp_server.async_tool_memory_supersede)(
            old_node=ingested[2].content_node_id,
            new_content="Widget deployment guidance now requires the verified progressive rollout path.",
            reason="Live deployment verification disproved the retired rollout guidance.",
        )
        archive_result = await _wrap_memory_curation(mcp_server.async_tool_memory_archive)(
            node_ids=[ingested[3].content_node_id],
            reason="The note is obsolete and duplicates current verified guidance.",
        )

    assert "error" not in link_result
    assert "error" not in supersede_result
    assert "error" not in archive_result
    edges = list((await session.scalars(select(MemoryEdgeNode))).all())
    deliberate_edges = {edge.id: edge for edge in edges if edge.created_by == "agent_curation"}
    assert link_result["edge_id"] in deliberate_edges
    assert supersede_result["edge_id"] in deliberate_edges
    assert deliberate_edges[link_result["edge_id"]].edge_kind == "supports_guidance"
    assert deliberate_edges[supersede_result["edge_id"]].edge_kind == "superseded_by"
    assert deliberate_edges[link_result["edge_id"]].evidence_span_ids
    assert deliberate_edges[supersede_result["edge_id"]].evidence_span_ids

    old_node = await session.get(MemoryNode, ingested[2].content_node_id)
    archived_node = await session.get(MemoryNode, ingested[3].content_node_id)
    assert old_node.truth_status == "superseded"
    assert old_node.freshness_status == "stale"
    assert archived_node.archived_at is not None
    old_payload = await ReconstructiveMemoryCompatibilityRepository(session).get(old_node.id)
    assert old_payload["superseded_by"] == supersede_result["new_node"]

    curation_sources = [
        source
        for source in (await session.scalars(select(MemorySource))).all()
        if source.source_kind == "agent_curation" and source.structured_payload.get("action")
    ]
    payload_by_action = {source.structured_payload["action"]: source.structured_payload for source in curation_sources}
    assert payload_by_action["link"]["reason"] == link_result["reason"]
    assert payload_by_action["supersede"]["reason"] == supersede_result["reason"]
    assert payload_by_action["archive"]["reason"] == archive_result["reason"]
    assert all(source.authority_principal == _TEST_USER_ID for source in curation_sources)
    assert all(source.structured_payload["created_by"] == "agent_curation" for source in curation_sources)

    recall = await mcp_server.async_tool_memory_reconstruct(
        query="Widget deployment guidance rollout path",
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
    )
    recalled_node_ids = {item["node_id"] for item in recall["supporting_evidence"]}
    assert ingested[2].content_node_id not in recalled_node_ids
    assert supersede_result["new_node"] in recalled_node_ids


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
    assert payload["evidence_pack"]["confidence"] == payload["memories"][0]["similarity"]
    assert payload["evidence_pack"]["confidence"] != 0.81
    assert payload["memories"][0]["memory_system"] == "reconstructive"
    assert payload["memories"][0]["source_span_id"] is not None
    retrieval_log_count = await session.scalar(text("SELECT COUNT(*) FROM retrieval_log"))
    assert retrieval_log_count == 1

    memory_table_exists = await session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='memories'")
    )
    assert memory_table_exists.first() is None


async def test_ingest_memory_source_creates_reconstructable_graph(async_sqlite_session_factory, monkeypatch):
    session = await _session(async_sqlite_session_factory)
    _mock_embedding_runtime(monkeypatch)

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

    embedding = (
        await session.scalars(
            select(MemoryNodeEmbedding).where(MemoryNodeEmbedding.node_id == ingested.content_node_id)
        )
    ).one()
    assert embedding.embedding_kind == "content"

    pack = await reconstruct_memory(
        session,
        query="cite source spans",
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
    )
    assert pack.confidence == pytest.approx(0.9964)
    assert pack.supporting_evidence[0].storage_confidence == 0.88
    assert "cite source spans" in pack.supporting_evidence[0].text.lower()


async def test_embedding_failure_leaves_complete_backfillable_ingest(async_sqlite_session_factory):
    session = await _session(async_sqlite_session_factory)

    ingested = await ingest_memory_source(
        session,
        content="Embedding outages must not leave half-written memory graphs.",
        content_kind="policy",
        source_kind="manual_note",
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
        confidence=0.9,
    )
    await session.flush()

    assert await session.get(MemoryNode, ingested.content_node_id) is not None
    assert await session.get(MemoryAssertionNode, ingested.assertion_id) is not None
    assert ingested.edge_ids
    embeddings = list(
        (
            await session.scalars(
                select(MemoryNodeEmbedding).where(
                    MemoryNodeEmbedding.node_id == ingested.content_node_id
                )
            )
        ).all()
    )
    assert embeddings == []


async def test_embedding_backfill_is_batched_idempotent_and_excludes_cues_and_tags(
    async_sqlite_session_factory,
    monkeypatch,
):
    session = await _session(async_sqlite_session_factory)
    _mock_embedding_runtime(monkeypatch)
    node_repo = MemoryNodeRepository(session)
    assertion_repo = MemoryAssertionRepository(session)

    content = await node_repo.upsert_node(
        draft=NodeDraft(
            node_kind="content",
            content_kind="fact",
            canonical_label="Backfill target",
            text="The content-node wording.",
            confidence=0.7,
        ),
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
    )
    cue = await node_repo.upsert_node(
        draft=NodeDraft(node_kind="cue", canonical_label="backfill", confidence=0.7),
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
    )
    second_content = await node_repo.upsert_node(
        draft=NodeDraft(
            node_kind="summary",
            content_kind="summary",
            canonical_label="Second backfill target",
            text="A second batch proves cursor-based resume.",
            confidence=0.6,
        ),
        org_id=_TEST_ORG_ID,
        user_id=_TEST_USER_ID,
        visibility="org",
    )
    await assertion_repo.create_assertion(
        draft=AssertionDraft(
            node_id=content.id,
            claim_text="A divergent assertion wording that also needs recall coverage.",
            confidence=0.7,
        )
    )

    first = await backfill_memory_node_embeddings(
        session,
        batch_size=1,
        limit=1,
        commit_batches=True,
    )
    resumed = await backfill_memory_node_embeddings(
        session,
        batch_size=1,
        after_id=first.last_node_id or 0,
        commit_batches=True,
    )
    repeated = await backfill_memory_node_embeddings(
        session,
        batch_size=1,
        commit_batches=False,
    )

    assert first.scanned == 1
    assert first.created == 2
    assert resumed.scanned == 1
    assert resumed.created == 1
    assert repeated.created == 0
    assert repeated.skipped == 3
    rows = list((await session.scalars(select(MemoryNodeEmbedding))).all())
    assert {(row.node_id, row.embedding_kind) for row in rows} == {
        (content.id, "content"),
        (content.id, "assertion"),
        (second_content.id, "content"),
    }
    assert all(row.node_id != cue.id for row in rows)


async def test_recall_observation_receives_agent_run_id(monkeypatch):
    from brain.app.mcp import server as mcp_server

    observe = AsyncMock(return_value={"retrieval_decision_id": None})
    monkeypatch.setattr(mcp_server, "observe_retrieval", observe)
    monkeypatch.setattr(mcp_server, "_async_log_retrieval", AsyncMock())

    class _AttentionController:
        def materialize_selection(self, memories, decision):
            del decision
            return SimpleNamespace(
                selected=list(memories),
                suppressed=[],
                lazy_load_eligible=[],
            )

    monkeypatch.setattr(mcp_server, "AttentionController", _AttentionController)
    result = await mcp_server._finalize_recall_response(
        query="Aritzia account",
        memories=[{"id": 1, "similarity": 0.83}],
        limit=3,
        user_id=_TEST_USER_ID,
        org_id=_TEST_ORG_ID,
        attention_debug=False,
        expand_lazy_load=False,
        run_id=72,
    )

    assert result["memories"] == [{"id": 1, "similarity": 0.83}]
    assert observe.await_args.kwargs["run_id"] == 72


async def test_retrieval_log_records_actual_match_score(monkeypatch):
    from brain.app.mcp import server as mcp_server
    from brain.systems.memory import retrieval_feedback

    recorder = AsyncMock(return_value={"retrieval_log_id": 11})
    monkeypatch.setattr(retrieval_feedback, "record_reconstruction_retrieval_feedback", recorder)

    class _UnitOfWork:
        async def __aenter__(self):
            self.session = object()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(mcp_server, "UnitOfWork", _UnitOfWork)
    await mcp_server._async_log_retrieval(
        "Aritzia account",
        [
            {"id": 1, "similarity": 0.83, "confidence": 0.83},
            {"id": 2, "similarity": 0.41, "confidence": 0.41},
        ],
        reconstruction_run_id=17,
        org_id=_TEST_ORG_ID,
    )

    assert recorder.await_args.kwargs["reconstruction_run_id"] == 17
    assert recorder.await_args.kwargs["evidence"][0]["confidence"] == 0.83
    assert recorder.await_args.kwargs["org_id"] == _TEST_ORG_ID


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
