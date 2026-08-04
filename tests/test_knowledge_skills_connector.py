"""Contract tests for the skills knowledge connector."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

from brain.kernel.config import KNOWLEDGE_EMBEDDING_DIM
from brain.platform.db.models.knowledge import (
    KnowledgeItem,
    KnowledgeItemEmbedding,
    KnowledgeSyncState,
)
from brain.platform.db.models.skill import Skill
from brain.systems.knowledge.connectors.skills import (
    SkillsConnector,
    _draft_for_skill,
)
from brain.systems.knowledge.search import search_knowledge
from brain.systems.knowledge.service import sync_connector
from brain.systems.runtime_settings.memory import EmbeddingRuntimeConfig


_ORG_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
async def session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    del sqlite_postgres_ddl_patch
    SQLiteTypeCompiler.visit_VECTOR = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_Vector = lambda self, type_, **kw: "TEXT"
    return await async_sqlite_session_factory(
        [
            Skill.__table__,
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
    monkeypatch.setattr(
        embedding_client,
        "embed_query",
        lambda text, runtime_config=None: vector,
    )
    return runtime


def _skill(
    skill_id: int,
    updated_at: datetime,
    *,
    name: str | None = None,
    archived: bool = False,
) -> Skill:
    return Skill(
        id=skill_id,
        name=name or f"skill-{skill_id}",
        description="Triage customer generation reports using source evidence.",
        procedure=(
            "Inspect the generation report, collect policy evidence, and route "
            "the model before changing content policy."
        ),
        version=3,
        level="cognitive",
        skill_type="procedure",
        maturity="proficient",
        confidence=0.9,
        use_count=12,
        success_count=10,
        failure_count=1,
        partial_count=1,
        pitfalls=[{"text": "Do not infer a policy failure without evidence."}],
        triggers=[{"direction": "for", "pattern": "generation report triage"}],
        guardrails=[{"text": "Preserve the original report before rerouting."}],
        archived=archived,
        created_at=updated_at,
        updated_at=updated_at,
    )


def test_skill_draft_contains_subject_matter_and_skill_view_provenance():
    updated_at = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    draft = _draft_for_skill(
        _skill(
            54,
            updated_at,
            name="uwear-customer-generation-report-triage",
        )
    )

    assert draft.source == "skills"
    assert draft.kind == "skill"
    assert draft.source_ref == "skill:54"
    assert draft.title == "uwear-customer-generation-report-triage"
    assert "Inspect the generation report" in draft.raw_text
    assert "generation report triage" in draft.raw_text
    assert "Preserve the original report" in draft.raw_text
    assert "Do not infer a policy failure" in draft.raw_text
    assert draft.summary == draft.raw_text
    assert draft.extra == {
        "archived": False,
        "maturity": "proficient",
        "skill_type": "procedure",
        "skill_view": {
            "tool": "skill_view",
            "arguments": {"name": "uwear-customer-generation-report-triage"},
        },
        "success_count": 10,
        "use_count": 12,
        "version": 3,
    }


async def test_skill_connector_cursor_is_incremental_and_upsert_is_idempotent(
    session,
    embedding_runtime,
):
    del embedding_runtime
    first_at = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    session.add_all([_skill(54, first_at), _skill(55, first_at)])
    await session.flush()
    connector = SkillsConnector(max_items=1)

    first = await sync_connector(session, connector)
    second = await sync_connector(session, connector)
    exhausted = await sync_connector(session, connector)

    assert first.cursor == {"updated_at": first_at.isoformat(), "id": 54}
    assert first.stats["ingested"] == 1
    assert second.cursor == {"updated_at": first_at.isoformat(), "id": 55}
    assert second.stats["ingested"] == 1
    assert exhausted.cursor == second.cursor
    assert exhausted.stats["ingested"] == 0
    assert await session.scalar(select(func.count(KnowledgeItem.id))) == 2

    skill = await session.get(Skill, 54)
    assert skill is not None
    changed_at = datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc)
    skill.procedure = "Updated evidence-first procedure."
    skill.updated_at = changed_at
    await session.flush()

    updated = await sync_connector(session, connector)
    item = await session.scalar(
        select(KnowledgeItem).where(KnowledgeItem.source_ref == "skill:54")
    )

    assert updated.cursor == {"updated_at": changed_at.isoformat(), "id": 54}
    assert updated.stats["ingested"] == 1
    assert item is not None
    assert item.raw_text.find("Updated evidence-first procedure") >= 0
    assert await session.scalar(select(func.count(KnowledgeItem.id))) == 2


async def test_archived_skill_updates_the_existing_item_as_archived(
    session,
    embedding_runtime,
):
    del embedding_runtime
    created_at = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    skill = _skill(54, created_at)
    session.add(skill)
    await session.flush()
    connector = SkillsConnector(max_items=10)
    await sync_connector(session, connector)

    archived_at = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
    skill.archived = True
    skill.updated_at = archived_at
    await session.flush()

    archived = await sync_connector(session, connector)
    item = await session.scalar(
        select(KnowledgeItem).where(KnowledgeItem.source_ref == "skill:54")
    )

    assert archived.stats["ingested"] == 1
    assert item is not None
    assert item.archived_at.replace(tzinfo=timezone.utc) == archived_at
    assert item.extra["archived"] is True


async def test_customer_generation_report_triage_skill_is_searchable(
    session,
    embedding_runtime,
):
    del embedding_runtime
    updated_at = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    session.add(
        _skill(
            54,
            updated_at,
            name="uwear-customer-generation-report-triage",
        )
    )
    await session.flush()
    await sync_connector(session, SkillsConnector(max_items=10))

    result = await search_knowledge(
        session,
        "customer generation report triage",
        org_id=_ORG_ID,
    )

    assert [hit["source_ref"] for hit in result["results"]] == ["skill:54"]
    assert result["results"][0]["extra"]["skill_view"] == {
        "tool": "skill_view",
        "arguments": {"name": "uwear-customer-generation-report-triage"},
    }
