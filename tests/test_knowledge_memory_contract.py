"""Writer-to-reader contract tests for the reconstructive-memory mirror."""

from __future__ import annotations

from unittest.mock import AsyncMock

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

from brain.kernel.config import KNOWLEDGE_EMBEDDING_DIM
from brain.platform.db.models.knowledge import (
    KnowledgeItem,
    KnowledgeItemEmbedding,
    KnowledgeSyncState,
)
from brain.platform.db.models.reconstructive_memory import (
    MemoryAssertionNode,
    MemoryEdgeNode,
    MemoryNode,
    MemorySource,
    MemorySpan,
)
from brain.systems.knowledge.connectors.memory import MemoryConnector
from brain.systems.knowledge.service import sync_connector
from brain.systems.reconstructive_memory.ingestion import ingest_memory_source
from brain.systems.runtime_settings.memory import EmbeddingRuntimeConfig


_ORG_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
async def session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    del sqlite_postgres_ddl_patch
    SQLiteTypeCompiler.visit_VECTOR = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_Vector = lambda self, type_, **kw: "TEXT"
    return await async_sqlite_session_factory(
        [
            MemorySource.__table__,
            MemorySpan.__table__,
            MemoryNode.__table__,
            MemoryAssertionNode.__table__,
            MemoryEdgeNode.__table__,
            KnowledgeItem.__table__,
            KnowledgeItemEmbedding.__table__,
            KnowledgeSyncState.__table__,
        ]
    )


@pytest.fixture
def embedding_runtime(monkeypatch):
    from brain.systems.memory import embeddings as embedding_client
    from brain.systems.runtime_settings import memory as runtime_settings

    runtime = EmbeddingRuntimeConfig(
        backend="api",
        provider="gemini",
        api_model="test-knowledge-embedding",
        cpu_model="unused",
        dimensions=KNOWLEDGE_EMBEDDING_DIM,
        api_key="test-key",
    )
    vector = np.zeros(KNOWLEDGE_EMBEDDING_DIM, dtype=np.float32)
    vector[0] = 1.0

    async def fake_runtime_config(session, *, include_secret=True):
        del session, include_secret
        return runtime

    monkeypatch.setattr(
        runtime_settings,
        "async_get_embedding_runtime_config",
        fake_runtime_config,
    )
    monkeypatch.setattr(
        embedding_client,
        "embed_document",
        lambda text, runtime_config=None: vector,
    )
    return runtime


async def test_memory_connector_indexes_the_content_kind_produced_by_ingestion(
    session,
    embedding_runtime,
    monkeypatch,
):
    del embedding_runtime
    from brain.systems.reconstructive_memory import embeddings as memory_embeddings

    monkeypatch.setattr(memory_embeddings, "embed_node_texts", AsyncMock())
    ingested = await ingest_memory_source(
        session,
        content=(
            "Release ownership stays with the agent already working the ticket. "
            "New workers must inspect active assignments before claiming it."
        ),
        content_kind="decision",
        source_kind="contract_test",
        source_ref="knowledge-memory-contract",
        org_id=_ORG_ID,
        visibility="team",
        scope_key="engineering",
        confidence=0.9,
    )

    first = await sync_connector(session, MemoryConnector(max_items=20))
    mirrored_refs = list(
        (
            await session.scalars(
                select(KnowledgeItem.source_ref).where(
                    KnowledgeItem.source == "memory"
                )
            )
        ).all()
    )
    produced_kinds = set(
        (
            await session.scalars(
                select(MemoryNode.node_kind).where(
                    MemoryNode.id.in_(
                        [
                            ingested.content_node_id,
                            *ingested.tag_node_ids,
                            *ingested.cue_node_ids,
                        ]
                    )
                )
            )
        ).all()
    )

    assert produced_kinds == {"content", "tag", "cue"}
    assert mirrored_refs == [f"memory_node:{ingested.content_node_id}"]
    assert first.stats == {
        "ingested": 1,
        "skipped": 0,
        "failed": 0,
        "truncated": 0,
    }

    unchanged = await sync_connector(session, MemoryConnector(max_items=20))
    assert unchanged.stats == {
        "ingested": 0,
        "skipped": 0,
        "failed": 0,
        "truncated": 0,
    }
