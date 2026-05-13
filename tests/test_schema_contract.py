"""Integration checks for brain schema and recall fallback behavior."""

import os
import sys
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("CI_BRAIN_DB"),
        reason="Set CI_BRAIN_DB=1 to run DB-backed schema contract tests.",
    ),
]


async def test_memories_table_has_hierarchical_columns(rollback_db):
    """Critical recall columns must exist after migrations are applied."""
    cur = rollback_db
    await cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'memories'
    """)
    columns = {row["column_name"] for row in await cur.fetchall()}

    assert "memory_tier" in columns
    assert "consolidated" in columns
    assert "source_memory_ids" in columns


async def test_brain_recall_recovers_after_graph_sql_failure(db_session, unit_of_work_for_session):
    """Vector fallback must still work if graph recall poisons the transaction."""
    from brain.kernel.config import MEMORY_SEMANTIC_EMBEDDING_DIM
    import brain.app.mcp.server as mcp_server

    vector = np.zeros(MEMORY_SEMANTIC_EMBEDDING_DIM, dtype=np.float32)
    vector[0] = 1.0
    emb_str = "[" + ",".join(f"{x:.8f}" for x in vector) + "]"
    org_id = "11111111-1111-4111-8111-111111111111"
    user_id = "22222222-2222-4222-8222-222222222222"

    await db_session.execute(
        text("""
        INSERT INTO orgs (id, name, slug)
        VALUES (:org_id, :name, :slug)
        ON CONFLICT (id) DO NOTHING
        """),
        {"org_id": org_id, "name": "Schema Contract Org", "slug": "schema-contract-org"},
    )
    await db_session.execute(
        text("""
        INSERT INTO users (id, org_id, name, email, role, approved)
        VALUES (:user_id, :org_id, :name, :email, :role, true)
        ON CONFLICT (id) DO NOTHING
        """),
        {
            "user_id": user_id,
            "org_id": org_id,
            "name": "Schema Contract User",
            "email": "schema-contract@example.com",
            "role": "owner",
        },
    )

    await db_session.execute(text("""
        INSERT INTO memories (
            content, memory_type, semantic_embedding,
            salience, source, archived, user_id, org_id, visibility
        )
        VALUES (
            :content, :memory_type, CAST(:semantic_embedding AS vector),
            :salience, :source, false, :user_id, :org_id, 'private'
        )
    """), {
        "content": "Fallback recall should still find this lesson",
        "memory_type": "lesson",
        "semantic_embedding": emb_str,
        "salience": 8.0,
        "source": "test",
        "user_id": user_id,
        "org_id": org_id,
    })

    async def _poison_graph(self, *, query_embedding, limit=3, **kwargs):
        await self._session.execute(text("SELECT missing_memory_tier_column FROM memories LIMIT 1"))
        return []

    class _RollbackingSessionUnitOfWork(unit_of_work_for_session):
        async def __aenter__(self):
            await super().__aenter__()
            self._savepoint = db_session.begin_nested()
            await self._savepoint.__aenter__()
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                await db_session.flush()
            await self._savepoint.__aexit__(exc_type, exc_val, exc_tb)
            self._async_session = None
            self._clear_cached_repositories()
            return False

    with patch("brain.platform.db.repositories.memories.MemoryRepository.graph_augmented_recall", _poison_graph), \
         patch.object(mcp_server, "UnitOfWork", _RollbackingSessionUnitOfWork), \
         patch("brain.systems.memory.embeddings.embed_query", return_value=vector), \
         patch("brain.systems.memory.embeddings.vec_to_pg", return_value=emb_str), \
         patch("brain.app.mcp.server.observe_retrieval", new=AsyncMock(return_value={"retrieval_decision_id": 99, "stage": "brain_recall"})):
        result = await mcp_server.async_tool_brain_recall("fallback recall", user_id=user_id, org_id=org_id)

    assert result["count"] == 1
    assert result["memories"][0]["content"].startswith("Fallback recall should still find")
