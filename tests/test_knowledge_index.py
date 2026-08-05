"""Behavioral tests for the source-backed Illo Knowledge index."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.main import app
from brain.jobs.pipelines.knowledge_index_sync import CONNECTOR_FACTORIES
from brain.kernel.config import KNOWLEDGE_EMBEDDING_DIM
from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)
from brain.platform.db.models.domain import Domain, DomainObjectType, DomainRecord
from brain.platform.db.models.knowledge import (
    KnowledgeItem,
    KnowledgeItemEmbedding,
    KnowledgeSyncState,
)
from brain.platform.db.models.org import Org, User
from brain.platform.db.models.reconstructive_memory import (
    MemoryAssertionNode,
    MemoryEdgeNode,
    MemoryNode,
    MemorySource,
    MemorySpan,
)
from brain.systems.knowledge.connectors.base import (
    EnumerationFailure,
    EnumerationFailureKind,
    KNOWLEDGE_SCOPE_EXTRA_KEY,
    KnowledgeDraft,
    KnowledgeEnumeration,
    KnowledgeScope,
)
from brain.systems.knowledge.connectors.domain_records import DomainRecordsConnector
from brain.systems.knowledge.connectors.github import (
    GitHubConnector,
    _GitHubAuthority,
    _GitHubConfigurationError,
    _draft_for_issue,
    _github_authority,
)
from brain.systems.knowledge.connectors.memory import MemoryConnector
from brain.systems.knowledge.search import reciprocal_rank_fusion, search_knowledge
from brain.systems.knowledge.service import (
    RAW_TEXT_MAX_CHARS,
    index_memory_node,
    sync_connector,
)
from brain.systems.external_agents import service as external_agents
from brain.systems.runtime_settings.memory import EmbeddingRuntimeConfig
from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    GithubFixingPullRequest,
    GithubIssueClosure,
)
from brain.systems.github_read_failures import (
    GITHUB_READ_ACCESS_FORBIDDEN,
    GITHUB_READ_AUTHENTICATION_REQUIRED,
    GITHUB_READ_CONNECTOR_ERROR,
)
from brain.systems.vault.runtime_secrets import RuntimeSecretUnavailable


_ORG_ID = "11111111-1111-4111-8111-111111111111"


class _StubConnector:
    def __init__(
        self,
        *,
        source_key: str,
        drafts: list[KnowledgeDraft],
        new_cursor: dict | None = None,
        failures: tuple[EnumerationFailure, ...] = (),
    ):
        self.source_key = source_key
        self.drafts = drafts
        self.new_cursor = dict(new_cursor or {})
        self.failures = failures
        self.seen_cursors: list[dict] = []

    async def enumerate_changed(self, session, cursor):
        del session
        self.seen_cursors.append(dict(cursor))
        return KnowledgeEnumeration(
            drafts=list(self.drafts),
            cursor=dict(self.new_cursor),
            failures=self.failures,
        )


class _McpAsyncSession:
    def __init__(self):
        self.sync_session = MagicMock()

    async def run_sync(self, fn):
        return fn(self.sync_session)

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def close(self):
        return None


def _mcp_principal() -> external_agents.AgentBridgePrincipal:
    return external_agents.AgentBridgePrincipal(
        connection_id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        token_id="token-1",
        scopes=frozenset(external_agents.DEFAULT_BRIDGE_SCOPES),
        connection_display_name="Hermes",
        agent_kind="hermes",
    )


async def _mcp_request(*, session: _McpAsyncSession, request_id: int, arguments: dict):
    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "user-1",
        "org_id": "org-1",
        "role": "member",
    }
    app.dependency_overrides[rate_limit] = lambda: None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/mcp",
                headers={"Authorization": "Bearer bridge-token"},
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": "illo_read",
                        "arguments": arguments,
                    },
                },
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


def _runtime_config() -> EmbeddingRuntimeConfig:
    return EmbeddingRuntimeConfig(
        backend="api",
        provider="gemini",
        api_model="test-knowledge-embedding",
        cpu_model="unused",
        dimensions=KNOWLEDGE_EMBEDDING_DIM,
        api_key="test-key",
    )


def _unit_vector(axis: int = 0) -> np.ndarray:
    vector = np.zeros(KNOWLEDGE_EMBEDDING_DIM, dtype=np.float32)
    vector[axis] = 1.0
    return vector


def _memory_node(
    node_id: int,
    at: datetime,
    *,
    node_kind: str = "content",
    content_kind: str = "decision",
    title: str | None = None,
    text: str | None = None,
    scope: str = "engineering",
    org_id: str | None = _ORG_ID,
    user_id: str | None = None,
    visibility: str = "org",
    confidence: float = 0.8,
) -> MemoryNode:
    label = title or f"Memory {node_id}"
    return MemoryNode(
        id=node_id,
        node_kind=node_kind,
        content_kind=content_kind,
        canonical_label=label,
        text=text or f"Memory body {node_id}",
        normalized_key=label.casefold(),
        scope_key=scope,
        org_id=org_id,
        user_id=user_id,
        visibility=visibility,
        confidence=confidence,
        truth_status="active",
        freshness_status="fresh",
        created_at=at,
        updated_at=at,
    )


def test_default_knowledge_sync_includes_the_memory_mirror():
    assert [factory.source_key for factory in CONNECTOR_FACTORIES] == [
        "domain_records",
        "github",
        "slack",
        "memory",
        "skills",
    ]


def test_github_closed_issue_draft_carries_closure_facts_into_distillation():
    merged_at = datetime(2026, 7, 28, 20, 10, tzinfo=timezone.utc)
    draft = _draft_for_issue(
        "Illospace/illospace",
        {
            "id": 577,
            "number": 577,
            "title": "Knowledge slice 2",
            "state": "closed",
            "body": "Implement the conversational knowledge layer.",
            "labels": [],
            "user": {"login": "redawear"},
            "created_at": "2026-07-28T18:00:00Z",
            "updated_at": "2026-07-28T20:10:00Z",
            "closed_at": "2026-07-28T20:10:00Z",
        },
        closure=GithubIssueClosure(
            repo="Illospace/illospace",
            number=577,
            title="Knowledge slice 2",
            state="closed",
            closed_at=merged_at,
            closed_by="redawear",
            fixing_pull_requests=(
                GithubFixingPullRequest(
                    repo="Illospace/illospace",
                    number=583,
                    base_ref_name="main",
                    merge_commit_sha="a" * 40,
                    merged_at=merged_at,
                ),
            ),
        ),
        org_id=_ORG_ID,
    )

    assert draft.distill is True
    assert draft.resolution == (
        "Resolved by merged PR Illospace/illospace#583 "
        f"(commit {'a' * 40}) at {merged_at.isoformat()}"
    )
    assert "Illospace/illospace#583" in draft.entities
    assert draft.extra["closed_by"] == "redawear"
    assert draft.extra["fixing_pull_requests"] == [
        {
            "repo": "Illospace/illospace",
            "number": 583,
            "base_ref_name": "main",
            "merge_commit_sha": "a" * 40,
            "merged_at": merged_at.isoformat(),
        }
    ]
    assert draft.extra["org_id"] == _ORG_ID
    assert "actor_user_id" not in draft.extra


@pytest.fixture
def embedding_runtime(monkeypatch):
    from brain.systems.memory import embeddings as embedding_client
    from brain.systems.runtime_settings import memory as runtime_settings

    runtime = _runtime_config()

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
        lambda text, runtime_config=None: _unit_vector(),
    )
    monkeypatch.setattr(
        embedding_client,
        "embed_query",
        lambda text, runtime_config=None: _unit_vector(),
    )
    return runtime


@pytest.fixture
async def session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    del sqlite_postgres_ddl_patch
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"
    SQLiteTypeCompiler.visit_VECTOR = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_Vector = lambda self, type_, **kw: "TEXT"
    return await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
            Domain.__table__,
            DomainObjectType.__table__,
            DomainRecord.__table__,
            KnowledgeItem.__table__,
            KnowledgeItemEmbedding.__table__,
            KnowledgeSyncState.__table__,
            MemorySource.__table__,
            MemorySpan.__table__,
            MemoryNode.__table__,
            MemoryAssertionNode.__table__,
            MemoryEdgeNode.__table__,
        ]
    )


async def test_sync_connector_upsert_is_idempotent_by_digest(
    session,
    embedding_runtime,
):
    del embedding_runtime
    draft = KnowledgeDraft(
        source="stub",
        kind="record",
        source_ref="stub:1",
        scope=KnowledgeScope.ORGANIZATION,
        title="Stable knowledge",
        summary="The same source content should only be embedded once.",
        raw_text="stable source body",
    )
    connector = _StubConnector(
        source_key="stub",
        drafts=[draft],
        new_cursor={"position": 1},
    )

    first = await sync_connector(session, connector)
    second = await sync_connector(session, connector)

    assert first.stats == {
        "ingested": 1,
        "skipped": 0,
        "failed": 0,
        "truncated": 0,
    }
    assert second.stats == {
        "ingested": 0,
        "skipped": 1,
        "failed": 0,
        "truncated": 0,
    }
    assert connector.seen_cursors == [{}, {"position": 1}]
    assert await session.scalar(select(func.count()).select_from(KnowledgeItem)) == 1
    assert (
        await session.scalar(
            select(func.count()).select_from(KnowledgeItemEmbedding)
        )
        == 1
    )


async def test_domain_records_connector_advances_and_resumes_watermark_with_id_tiebreaker(
    session,
    embedding_runtime,
):
    del embedding_runtime
    updated_at = datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)
    created_at = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    session.add_all(
        [
            Domain(
                id=1,
                org_id=_ORG_ID,
                slug="knowledge-notes",
                name="Knowledge Notes",
                created_at=created_at,
                updated_at=updated_at,
            ),
            DomainObjectType(
                id=1,
                domain_id=1,
                key="note",
                name="Note",
                created_at=created_at,
                updated_at=updated_at,
            ),
            *[
                DomainRecord(
                    id=record_id,
                    org_id=_ORG_ID,
                    domain_id=1,
                    object_type_id=1,
                    title=f"Note {record_id}",
                    data={"body": f"Body {record_id}"},
                    search_text=f"note body {record_id}",
                    version=1,
                    created_at=created_at,
                    updated_at=updated_at,
                )
                for record_id in (1, 2, 3)
            ],
        ]
    )
    await session.flush()
    connector = DomainRecordsConnector(max_items=2)

    first = await sync_connector(session, connector)
    first_refs = list(
        (
            await session.scalars(
                select(KnowledgeItem.source_ref).order_by(KnowledgeItem.source_ref)
            )
        ).all()
    )
    second = await sync_connector(session, connector)

    assert first_refs == ["domain_record:1", "domain_record:2"]
    assert first.cursor == {"updated_at": updated_at.isoformat(), "id": 2}
    assert first.stats["ingested"] == 2
    assert second.cursor == {"updated_at": updated_at.isoformat(), "id": 3}
    assert second.stats["ingested"] == 1
    assert list(
        (
            await session.scalars(
                select(KnowledgeItem.source_ref).order_by(KnowledgeItem.source_ref)
            )
        ).all()
    ) == ["domain_record:1", "domain_record:2", "domain_record:3"]


async def test_memory_connector_mirrors_only_shared_source_backed_memories(
    session,
    embedding_runtime,
):
    del embedding_runtime
    updated_at = datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc)
    session.add_all(
        [
            _memory_node(
                11,
                updated_at,
                content_kind="decision",
                title="Queue ownership rule",
                text="An in-progress ticket belongs to the agent already working it.",
                visibility="org",
                confidence=0.92,
            ),
            _memory_node(
                12,
                updated_at,
                node_kind="summary",
                content_kind="decision",
                title="Unproduced derived summary",
                text="This hypothetical derived node must not define the mirror contract.",
                visibility="org",
                confidence=0.7,
            ),
            _memory_node(
                13,
                updated_at,
                content_kind="preference",
                title="Private preference",
                text="This private content must remain in memory only.",
                scope="personal",
                org_id=None,
                user_id="22222222-2222-4222-8222-222222222222",
                visibility="private",
                confidence=0.8,
            ),
        ]
    )
    await session.flush()

    result = await sync_connector(session, MemoryConnector(max_items=10))
    items = list((await session.scalars(select(KnowledgeItem))).all())

    assert result.status == "ok"
    assert result.stats == {
        "ingested": 1,
        "skipped": 0,
        "failed": 0,
        "truncated": 0,
    }
    assert result.to_dict()["corpus_empty"] is False
    assert result.cursor == {"updated_at": updated_at.isoformat(), "id": 13}
    assert [item.source_ref for item in items] == ["memory_node:11"]
    assert items[0].source == "memory"
    assert items[0].kind == "memory"
    assert items[0].title == "Queue ownership rule"
    assert (
        items[0].summary
        == "An in-progress ticket belongs to the agent already working it."
    )
    assert items[0].raw_text == items[0].summary
    assert items[0].entities == ["decision", "engineering"]
    assert items[0].extra == {
        "archived": False,
        "confidence": 0.92,
        "freshness_status": "fresh",
        KNOWLEDGE_SCOPE_EXTRA_KEY: KnowledgeScope.ORGANIZATION.value,
        "memory_type": "decision",
        "node_kind": "content",
        "org_id": _ORG_ID,
        "scope": "engineering",
        "sensitivity": "low",
        "source_backed": True,
        "source_type": "reconstructive_memory_node",
        "superseded": False,
        "superseded_by": None,
        "truth_status": "active",
        "visibility": "org",
    }


async def test_memory_connector_reports_an_empty_searchable_corpus(session):
    result = await sync_connector(session, MemoryConnector(max_items=10))
    state = await session.get(KnowledgeSyncState, "memory")

    assert result.status == "ok"
    assert result.stats == {
        "ingested": 0,
        "skipped": 0,
        "failed": 0,
        "truncated": 0,
    }
    assert result.corpus_empty is True
    assert result.to_dict()["corpus_empty"] is True
    assert state is not None
    assert state.last_stats == result.stats


async def test_memory_connector_skips_orgless_nodes_and_keeps_organization_scope(
    session,
    embedding_runtime,
    caplog,
):
    del embedding_runtime
    updated_at = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    session.add_all(
        [
            _memory_node(
                14,
                updated_at,
                title="Unreachable org-less memory",
                org_id=None,
                visibility="org",
            ),
            _memory_node(
                15,
                updated_at,
                title="Reachable organization memory",
                org_id=_ORG_ID,
                visibility="org",
            ),
        ]
    )
    await session.flush()

    with caplog.at_level("WARNING"):
        result = await sync_connector(session, MemoryConnector(max_items=10))

    items = list((await session.scalars(select(KnowledgeItem))).all())
    embeddings = list((await session.scalars(select(KnowledgeItemEmbedding))).all())

    assert result.stats["ingested"] == 1
    assert [item.source_ref for item in items] == ["memory_node:15"]
    assert [embedding.item_id for embedding in embeddings] == [items[0].id]
    assert items[0].extra[KNOWLEDGE_SCOPE_EXTRA_KEY] == (
        KnowledgeScope.ORGANIZATION.value
    )
    assert items[0].extra["org_id"] == _ORG_ID
    assert "skipped node 14: org_id is missing" in caplog.text


def _committing_unit_of_work(session):
    class _CommittingUnitOfWork:
        async def __aenter__(self):
            self.session = session
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            del exc, traceback
            if exc_type is None:
                await session.commit()
            else:
                await session.rollback()
            return False

    return _CommittingUnitOfWork


async def _ingest_shared_memory_through_mcp(
    session,
    monkeypatch,
    *,
    content: str,
    source_ref: str,
):
    from brain.app.mcp import server as mcp_server
    from brain.systems.reconstructive_memory import embeddings as memory_embeddings

    monkeypatch.setattr(
        mcp_server,
        "UnitOfWork",
        _committing_unit_of_work(session),
    )
    monkeypatch.setattr(memory_embeddings, "embed_node_texts", AsyncMock())
    return await mcp_server.async_tool_memory_ingest_source(
        content=content,
        content_kind="decision",
        source_kind="contract_test",
        source_ref=source_ref,
        org_id=_ORG_ID,
        user_id="22222222-2222-4222-8222-222222222222",
        visibility="team",
        confidence=0.9,
    )


async def test_memory_ingest_indexes_committed_node_for_immediate_search(
    session,
    embedding_runtime,
    monkeypatch,
):
    del embedding_runtime
    distinctive = "Zephyr quokka release ownership stays with the active worker."

    payload = await _ingest_shared_memory_through_mcp(
        session,
        monkeypatch,
        content=distinctive,
        source_ref="immediate-knowledge-search",
    )

    source_ref = f"memory_node:{payload['content_node_id']}"
    item = await session.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.source == "memory",
            KnowledgeItem.source_ref == source_ref,
        )
    )
    search = await search_knowledge(session, "zephyr quokka", org_id=_ORG_ID)

    assert item is not None
    assert item.summary == distinctive
    assert [result["source_ref"] for result in search["results"]] == [source_ref]


@pytest.mark.parametrize(
    ("node_id", "node_kind", "org_id", "visibility"),
    [
        (16, "content", None, "org"),
        (17, "content", _ORG_ID, "private"),
        (18, "summary", _ORG_ID, "org"),
    ],
)
async def test_immediate_memory_index_reuses_connector_eligibility(
    session,
    embedding_runtime,
    node_id,
    node_kind,
    org_id,
    visibility,
):
    del embedding_runtime
    session.add(
        _memory_node(
            node_id,
            datetime(2026, 8, 5, 14, node_id, tzinfo=timezone.utc),
            node_kind=node_kind,
            org_id=org_id,
            visibility=visibility,
        )
    )
    await session.commit()

    stats = await index_memory_node(session, node_id=node_id)
    await session.commit()

    item = await session.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.source == "memory",
            KnowledgeItem.source_ref == f"memory_node:{node_id}",
        )
    )
    assert stats.ingested == 0
    assert item is None


async def test_memory_sweep_and_cursor_rewalk_are_idempotent_after_immediate_ingest(
    session,
    embedding_runtime,
    monkeypatch,
):
    del embedding_runtime
    payload = await _ingest_shared_memory_through_mcp(
        session,
        monkeypatch,
        content="Cobalt narwhal decisions remain searchable after every reconciler pass.",
        source_ref="immediate-then-sweep",
    )
    source_ref = f"memory_node:{payload['content_node_id']}"
    before = await session.scalar(
        select(KnowledgeItem).where(KnowledgeItem.source_ref == source_ref)
    )
    assert before is not None
    item_id = before.id
    digest = before.content_digest

    first_sweep = await sync_connector(session, MemoryConnector(max_items=20))
    await session.execute(
        delete(KnowledgeSyncState).where(KnowledgeSyncState.source == "memory")
    )
    await session.flush()
    rewalk = await sync_connector(session, MemoryConnector(max_items=20))

    items = list(
        (
            await session.scalars(
                select(KnowledgeItem).where(KnowledgeItem.source_ref == source_ref)
            )
        ).all()
    )
    embeddings = list(
        (
            await session.scalars(
                select(KnowledgeItemEmbedding).where(
                    KnowledgeItemEmbedding.item_id == item_id
                )
            )
        ).all()
    )
    assert first_sweep.stats["ingested"] == 0
    assert first_sweep.stats["skipped"] == 1
    assert rewalk.stats["ingested"] == 0
    assert rewalk.stats["skipped"] == 1
    assert len(items) == 1
    assert items[0].id == item_id
    assert items[0].content_digest == digest
    assert len(embeddings) == 1


async def test_memory_index_failure_does_not_fail_committed_ingest(
    session,
    monkeypatch,
    caplog,
):
    from brain.app.mcp import server as mcp_server
    from brain.systems.knowledge import service as knowledge_service
    from brain.systems.reconstructive_memory import embeddings as memory_embeddings

    monkeypatch.setattr(
        mcp_server,
        "UnitOfWork",
        _committing_unit_of_work(session),
    )
    monkeypatch.setattr(memory_embeddings, "embed_node_texts", AsyncMock())
    monkeypatch.setattr(
        knowledge_service,
        "index_memory_node",
        AsyncMock(side_effect=RuntimeError("knowledge mirror unavailable")),
    )

    with caplog.at_level("ERROR", logger="mcp_brain"):
        payload = await mcp_server.async_tool_memory_ingest_source(
            content="Amber kestrel preservation remains durable during an index outage.",
            source_kind="contract_test",
            source_ref="failed-immediate-index",
            org_id=_ORG_ID,
            user_id="22222222-2222-4222-8222-222222222222",
            visibility="org",
        )

    node = await session.get(MemoryNode, payload["content_node_id"])
    assert node is not None
    assert node.text == "Amber kestrel preservation remains durable during an index outage."
    assert "Immediate knowledge indexing failed for committed memory node" in caplog.text


async def test_memory_connector_resumes_a_bounded_same_timestamp_backfill(
    session,
    embedding_runtime,
):
    del embedding_runtime
    updated_at = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)
    session.add_all(
        [
            _memory_node(
                node_id,
                updated_at,
                content_kind="lesson",
                title=f"Source-backed lesson {node_id}",
                text=f"Durable lesson body {node_id}",
                visibility="team",
            )
            for node_id in (21, 22, 23)
        ]
    )
    await session.flush()
    connector = MemoryConnector(max_items=2)

    first = await sync_connector(session, connector)
    second = await sync_connector(session, connector)
    exhausted = await sync_connector(session, connector)

    assert first.stats["ingested"] == 2
    assert first.cursor == {"updated_at": updated_at.isoformat(), "id": 22}
    assert second.stats["ingested"] == 1
    assert second.cursor == {"updated_at": updated_at.isoformat(), "id": 23}
    assert exhausted.stats == {
        "ingested": 0,
        "skipped": 0,
        "failed": 0,
        "truncated": 0,
    }
    assert exhausted.cursor == second.cursor
    assert list(
        (
            await session.scalars(
                select(KnowledgeItem.source_ref).order_by(KnowledgeItem.source_ref)
            )
        ).all()
    ) == ["memory_node:21", "memory_node:22", "memory_node:23"]


async def test_memory_connector_propagates_archived_and_superseded_state(
    session,
    embedding_runtime,
):
    del embedding_runtime
    created_at = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
    nodes = [
        _memory_node(
            node_id,
            created_at,
            content_kind="decision",
            title=f"Decision {node_id}",
            text=f"Decision body {node_id}",
        )
        for node_id in (31, 32)
    ]
    replacement = _memory_node(
        33,
        created_at,
        node_kind="content",
        title="Replacement decision",
    )
    session.add_all([*nodes, replacement])
    await session.flush()
    connector = MemoryConnector(max_items=10)
    await sync_connector(session, connector)

    retired_at = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
    nodes[0].archived_at = retired_at
    nodes[0].updated_at = retired_at
    nodes[1].truth_status = "superseded"
    nodes[1].freshness_status = "stale"
    nodes[1].updated_at = retired_at
    session.add(
        MemoryEdgeNode(
            source_node_id=32,
            target_node_id=33,
            edge_kind="superseded_by",
            org_id=_ORG_ID,
            visibility="org",
            created_by="test",
        )
    )
    await session.flush()

    result = await sync_connector(session, connector)
    mirrored = {
        item.source_ref: item
        for item in (await session.scalars(select(KnowledgeItem))).all()
    }

    assert result.stats == {
        "ingested": 2,
        "skipped": 0,
        "failed": 0,
        "truncated": 0,
    }
    assert (
        mirrored["memory_node:31"].archived_at.replace(tzinfo=timezone.utc)
        == retired_at
    )
    assert mirrored["memory_node:31"].extra["archived"] is True
    assert mirrored["memory_node:31"].extra["superseded"] is False
    assert (
        mirrored["memory_node:32"].archived_at.replace(tzinfo=timezone.utc)
        == retired_at
    )
    assert mirrored["memory_node:32"].extra["archived"] is False
    assert mirrored["memory_node:32"].extra["superseded"] is True
    assert mirrored["memory_node:32"].extra["superseded_by"] == 33
    assert mirrored["memory_node:32"].extra["truth_status"] == "superseded"


async def test_memory_connector_scrubs_a_mirror_when_visibility_becomes_private(
    session,
    embedding_runtime,
):
    del embedding_runtime
    created_at = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)
    node = _memory_node(
        41,
        created_at,
        content_kind="decision",
        title="Shared launch decision",
        text="The private launch detail must disappear if visibility changes.",
        scope="launch",
        visibility="team",
        confidence=0.9,
    )
    session.add(node)
    await session.flush()
    connector = MemoryConnector(max_items=10)
    await sync_connector(session, connector)

    withdrawn_at = datetime(2026, 7, 21, 11, 0, tzinfo=timezone.utc)
    node.visibility = "private"
    node.updated_at = withdrawn_at
    await session.flush()

    result = await sync_connector(session, connector)
    mirrored = await session.scalar(
        select(KnowledgeItem).where(KnowledgeItem.source_ref == "memory_node:41")
    )

    assert result.stats == {
        "ingested": 1,
        "skipped": 0,
        "failed": 0,
        "truncated": 0,
    }
    assert result.corpus_empty is True
    assert result.cursor == {"updated_at": withdrawn_at.isoformat(), "id": 41}
    assert mirrored is not None
    assert mirrored.archived_at.replace(tzinfo=timezone.utc) == withdrawn_at
    assert mirrored.title == "Memory no longer shared"
    assert (
        mirrored.summary
        == "This memory is no longer shared with the workspace."
    )
    assert mirrored.raw_text == ""
    assert "launch detail" not in mirrored.search_text
    assert mirrored.extra == {
        "archived": True,
        KNOWLEDGE_SCOPE_EXTRA_KEY: KnowledgeScope.ORGANIZATION.value,
        "mirror_status": "visibility_withdrawn",
        "node_kind": "content",
        "org_id": _ORG_ID,
        "truth_status": "active",
        "visibility": "private",
    }


async def test_memory_connector_scrubs_a_mirror_after_its_organization_is_removed(
    session,
    embedding_runtime,
):
    del embedding_runtime
    created_at = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    node = _memory_node(
        42,
        created_at,
        title="Shared organization decision",
        text="This organization detail must be scrubbed after withdrawal.",
        visibility="org",
    )
    session.add(node)
    await session.flush()
    connector = MemoryConnector(max_items=10)
    await sync_connector(session, connector)

    withdrawn_at = datetime(2026, 7, 21, 13, 0, tzinfo=timezone.utc)
    node.visibility = "private"
    node.org_id = None
    node.updated_at = withdrawn_at
    await session.flush()

    result = await sync_connector(session, connector)
    mirrored = await session.scalar(
        select(KnowledgeItem).where(KnowledgeItem.source_ref == "memory_node:42")
    )

    assert result.stats["ingested"] == 1
    assert result.corpus_empty is True
    assert mirrored is not None
    assert mirrored.archived_at.replace(tzinfo=timezone.utc) == withdrawn_at
    assert mirrored.raw_text == ""
    assert "organization detail" not in mirrored.search_text
    assert mirrored.extra["mirror_status"] == "visibility_withdrawn"
    assert mirrored.extra["org_id"] == _ORG_ID


def test_reciprocal_rank_fusion_uses_recency_as_weight_not_candidate_gate():
    lexical = [1, 2]
    semantic = [2, 3]
    candidate_union = set(lexical) | set(semantic)

    ordered, debug = reciprocal_rank_fusion(
        {
            "lexical": lexical,
            "semantic": semantic,
            "recency": [3],
        }
    )

    assert ordered[0] == 2
    assert set(ordered) == candidate_union
    assert 1 in ordered
    assert "recency" not in debug[1]["channels"]


async def test_embedding_failures_degrade_to_lexical_ingest_and_search(
    session,
    embedding_runtime,
    monkeypatch,
):
    del embedding_runtime
    from brain.systems.memory import embeddings as embedding_client

    def fail_document_embedding(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("document embedding offline")

    monkeypatch.setattr(
        embedding_client,
        "embed_document",
        fail_document_embedding,
    )
    connector = _StubConnector(
        source_key="degraded",
        drafts=[
            KnowledgeDraft(
                source="degraded",
                kind="record",
                source_ref="degraded:1",
                scope=KnowledgeScope.ORGANIZATION,
                title="Lexical lighthouse",
                summary="This row survives an embedding outage.",
                raw_text="lexical lighthouse fallback",
                extra={"org_id": "org-degraded"},
            )
        ],
    )

    sync_result = await sync_connector(session, connector)
    item = await session.scalar(
        select(KnowledgeItem).where(KnowledgeItem.source_ref == "degraded:1")
    )

    assert sync_result.status == "degraded"
    assert sync_result.stats == {
        "ingested": 1,
        "skipped": 0,
        "failed": 1,
        "truncated": 0,
    }
    assert item is not None
    assert "lexical lighthouse" in item.search_text.casefold()

    def fail_query_embedding(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("query embedding offline")

    monkeypatch.setattr(
        embedding_client,
        "embed_query",
        fail_query_embedding,
    )
    search_result = await search_knowledge(
        session,
        "lexical lighthouse",
        org_id="org-degraded",
    )

    assert search_result["semantic_available"] is False
    assert search_result["semantic_degraded_reason"] == "query embedding offline"
    assert [result["source_ref"] for result in search_result["results"]] == [
        "degraded:1"
    ]


async def test_agent_mcp_exposes_and_dispatches_knowledge_search():
    from brain.app.api.routers import agent_mcp

    assert "knowledge.search" in agent_mcp.READ_CAPABILITIES
    assert agent_mcp.READ_CAPABILITIES["knowledge.search"]["arguments"] == {
        "query": "string",
        "sources": "string[]",
        "kinds": "string[]",
        "limit": "integer",
    }

    expected = {
        "query": "roadmap",
        "semantic_available": True,
        "results": [{"source_ref": "github:illo/brain#42"}],
    }
    session = _McpAsyncSession()
    captured: dict[str, object] = {}

    async def fake_search_knowledge(db, query, *, org_id, sources, kinds, limit):
        captured.update(
            {
                "db": db,
                "query": query,
                "org_id": org_id,
                "sources": sources,
                "kinds": kinds,
                "limit": limit,
            }
        )
        return expected

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_mcp_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp.search_knowledge",
        new=AsyncMock(side_effect=fake_search_knowledge),
    ):
        catalog_response = await _mcp_request(
            session=session,
            request_id=13,
            arguments={"capability": "capabilities"},
        )
        search_response = await _mcp_request(
            session=session,
            request_id=14,
            arguments={
                "capability": "knowledge.search",
                "arguments": {
                    "query": "roadmap",
                    "sources": ["github", "domain_records"],
                    "kinds": ["issue"],
                    "limit": 7,
                },
            },
        )

    assert catalog_response.status_code == 200
    catalog = json.loads(catalog_response.json()["result"]["content"][0]["text"])
    assert "knowledge.search" in {
        capability["name"] for capability in catalog["capabilities"]
    }
    assert search_response.status_code == 200
    assert json.loads(search_response.json()["result"]["content"][0]["text"]) == expected
    assert captured == {
        "db": session,
        "query": "roadmap",
        "org_id": "org-1",
        "sources": ["github", "domain_records"],
        "kinds": ["issue"],
        "limit": 7,
    }


async def test_agent_tool_passes_raw_knowledge_search_limit_to_search_boundary():
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.knowledge import (
        _handle_search_knowledge,
    )

    class StubUnitOfWork:
        session = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            del exc_type, exc, traceback

    captured: dict[str, object] = {}

    async def fake_search(_session, query, *, org_id, sources, kinds, limit):
        captured.update(
            query=query,
            org_id=org_id,
            sources=sources,
            kinds=kinds,
            limit=limit,
        )
        return {
            "requested_limit": limit,
            "effective_limit": 50,
        }

    with (
        bind_agent_context({"org_id": _ORG_ID}),
        patch(
            "brain.systems.runs.tool_catalog.handlers.knowledge.UnitOfWork",
            return_value=StubUnitOfWork(),
        ),
        patch(
            "brain.systems.runs.tool_catalog.handlers.knowledge.search_knowledge",
            new=AsyncMock(side_effect=fake_search),
        ),
    ):
        payload = json.loads(
            await _handle_search_knowledge("roadmap", limit=75)
        )

    assert captured["limit"] == 75
    assert payload == {
        "requested_limit": 75,
        "effective_limit": 50,
    }


async def test_sync_connector_accounts_for_truncated_raw_text(
    session,
    embedding_runtime,
):
    del embedding_runtime
    raw_text = "x" * (RAW_TEXT_MAX_CHARS + 17)
    connector = _StubConnector(
        source_key="truncation",
        drafts=[
            KnowledgeDraft(
                source="truncation",
                kind="record",
                source_ref="truncation:1",
                scope=KnowledgeScope.ORGANIZATION,
                title="Oversized source",
                summary="A source body larger than the storage bound.",
                raw_text=raw_text,
                extra={"origin": "test"},
            )
        ],
    )

    result = await sync_connector(session, connector)
    item = await session.scalar(
        select(KnowledgeItem).where(KnowledgeItem.source_ref == "truncation:1")
    )
    state = await session.get(KnowledgeSyncState, "truncation")

    assert result.stats == {
        "ingested": 1,
        "skipped": 0,
        "failed": 0,
        "truncated": 1,
    }
    assert item is not None
    assert len(item.raw_text) == RAW_TEXT_MAX_CHARS
    assert item.extra == {
        KNOWLEDGE_SCOPE_EXTRA_KEY: KnowledgeScope.ORGANIZATION.value,
        "origin": "test",
        "raw_text_truncated": True,
        "raw_text_total_chars": len(raw_text),
    }
    assert state is not None
    assert state.last_stats == result.stats


async def test_distillation_admission_is_restart_safe_and_holds_cursor_until_harvest(
    session,
    embedding_runtime,
    monkeypatch,
):
    del embedding_runtime
    from brain.systems.memory import embeddings as embedding_client

    embedded_documents: list[str] = []

    def capture_embedding(text, runtime_config=None):
        del runtime_config
        embedded_documents.append(text)
        return _unit_vector()

    monkeypatch.setattr(embedding_client, "embed_document", capture_embedding)
    user_id = "22222222-2222-4222-8222-222222222222"
    session.add(Org(id=_ORG_ID, name="Knowledge Org", slug="knowledge-org"))
    session.add(
        User(
            id=user_id,
            org_id=_ORG_ID,
            name="Knowledge Worker",
            email="knowledge@example.com",
        )
    )
    await session.flush()
    connector = _StubConnector(
        source_key="slack",
        drafts=[
            KnowledgeDraft(
                source="slack",
                kind="slack_thread",
                source_ref="slack:T1:C1:1700000000.000001",
                scope=KnowledgeScope.ORGANIZATION,
                title="Release thread",
                summary="Structural fallback summary",
                raw_text="Why did release 42 fail? It was fixed in deploy.py.",
                extra={"org_id": _ORG_ID, "actor_user_id": user_id},
                distill=True,
            )
        ],
        new_cursor={"latest_reply": "1700000001.000001"},
    )

    dispatched = await sync_connector(session, connector)
    repeated = await sync_connector(session, connector)
    runs = list((await session.scalars(select(AgentRunRow))).all())
    state = await session.get(KnowledgeSyncState, "slack")

    assert dispatched.status == "pending"
    assert dispatched.cursor == {}
    assert dispatched.stats["pending"] == 1
    assert repeated.status == "pending"
    assert connector.seen_cursors == [{}]
    assert len(runs) == 1
    assert runs[0].model_policy == {
        "model": "openai/gpt-5.6-luna",
        "thinking": "xhigh",
    }
    assert runs[0].source_idempotency_scope == "knowledge"
    assert state is not None
    assert state.cursor["_distillation_pending"]["proposed_cursor"] == {
        "latest_reply": "1700000001.000001"
    }

    runs[0].status = "completed"
    session.add(
        AgentRunArtifactRow(
            run_id=runs[0].id,
            root_run_id=runs[0].root_run_id,
            artifact_type="final_answer",
            text=json.dumps(
                {
                    "question": "Why did release 42 fail?",
                    "summary": "Release 42 failed during deployment.",
                    "resolution": "The deployment path was fixed.",
                    "systems": ["deployment"],
                    "code_references": ["deploy.py"],
                }
            ),
        )
    )
    await session.flush()

    harvested = await sync_connector(session, connector)
    item = await session.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.source_ref == "slack:T1:C1:1700000000.000001"
        )
    )

    assert harvested.status == "ok"
    assert harvested.cursor == {"latest_reply": "1700000001.000001"}
    assert harvested.stats["distilled"] == 1
    assert connector.seen_cursors == [{}]
    assert item is not None
    assert item.title == "Release thread"
    assert item.summary == "Release 42 failed during deployment."
    assert item.resolution == "The deployment path was fixed."
    assert item.entities == ["deployment", "deploy.py"]
    assert "Why did release 42 fail?" in item.search_text
    assert embedded_documents == [
        "Why did release 42 fail?\n"
        "Release 42 failed during deployment.\n"
        "The deployment path was fixed.\n"
        "deployment deploy.py"
    ]
    assert "Release thread" not in embedded_documents[0]
    assert (
        await session.scalar(
            select(func.count()).select_from(KnowledgeItemEmbedding)
        )
        == 1
    )

    unchanged = await sync_connector(session, connector)
    assert unchanged.status == "ok"
    assert unchanged.stats == {
        "ingested": 0,
        "skipped": 1,
        "failed": 0,
        "truncated": 0,
    }
    assert len(list((await session.scalars(select(AgentRunRow))).all())) == 1


async def test_distillation_exhaustion_lands_lexical_fallback_without_embedding(
    session,
    embedding_runtime,
):
    del embedding_runtime
    user_id = "33333333-3333-4333-8333-333333333333"
    session.add(Org(id=_ORG_ID, name="Fallback Org", slug="fallback-org"))
    session.add(
        User(
            id=user_id,
            org_id=_ORG_ID,
            name="Fallback Worker",
            email="fallback@example.com",
        )
    )
    await session.flush()
    connector = _StubConnector(
        source_key="github",
        drafts=[
            KnowledgeDraft(
                source="github",
                kind="issue",
                source_ref="github:Illospace/illospace#577",
                scope=KnowledgeScope.ORGANIZATION,
                title="Distill this issue",
                summary="Structural fallback survives poison output.",
                raw_text="Original issue body remains lexically searchable.",
                extra={"org_id": _ORG_ID, "actor_user_id": user_id},
                distill=True,
            )
        ],
        new_cursor={"id": 577},
    )

    await sync_connector(session, connector)
    first = (await session.scalars(select(AgentRunRow).order_by(AgentRunRow.id))).one()
    first.status = "completed"
    session.add(
        AgentRunArtifactRow(
            run_id=first.id,
            root_run_id=first.root_run_id,
            artifact_type="final_answer",
            text="This is not JSON.",
        )
    )
    await session.flush()

    retry = await sync_connector(session, connector)
    runs = list((await session.scalars(select(AgentRunRow).order_by(AgentRunRow.id))).all())
    assert retry.status == "pending"
    assert len(runs) == 2
    runs[-1].status = "failed"
    await session.flush()

    exhausted = await sync_connector(session, connector)
    item = await session.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.source_ref == "github:Illospace/illospace#577"
        )
    )

    assert exhausted.status == "degraded"
    assert exhausted.cursor == {"id": 577}
    assert exhausted.stats["failed"] == 1
    assert item is not None
    assert item.summary == "Structural fallback survives poison output."
    assert "Original issue body" in item.search_text
    assert item.extra["distillation"]["status"] == "failed"
    assert (
        await session.scalar(
            select(func.count()).select_from(KnowledgeItemEmbedding)
        )
        == 0
    )


async def test_mixed_distillation_batch_persists_completed_rows_while_others_wait(
    session,
    embedding_runtime,
):
    del embedding_runtime
    user_id = "44444444-4444-4444-8444-444444444444"
    session.add(Org(id=_ORG_ID, name="Mixed Org", slug="mixed-org"))
    session.add(
        User(
            id=user_id,
            org_id=_ORG_ID,
            name="Mixed Worker",
            email="mixed@example.com",
        )
    )
    await session.flush()
    connector = _StubConnector(
        source_key="slack",
        drafts=[
            KnowledgeDraft(
                source="slack",
                kind="slack_thread",
                source_ref=f"slack:T1:C1:{index}",
                scope=KnowledgeScope.ORGANIZATION,
                title=f"Thread {index}",
                summary=f"Fallback {index}",
                raw_text=f"Raw thread {index}",
                extra={"org_id": _ORG_ID, "actor_user_id": user_id},
                distill=True,
            )
            for index in (1, 2)
        ],
        new_cursor={"position": 2},
    )

    dispatched = await sync_connector(session, connector)
    runs = list((await session.scalars(select(AgentRunRow).order_by(AgentRunRow.id))).all())
    assert dispatched.stats["pending"] == 2
    runs[0].status = "completed"
    session.add(
        AgentRunArtifactRow(
            run_id=runs[0].id,
            root_run_id=runs[0].root_run_id,
            artifact_type="final_answer",
            text=json.dumps(
                {
                    "question": "Question one?",
                    "summary": "Distilled one.",
                    "resolution": None,
                    "systems": [],
                    "code_references": [],
                }
            ),
        )
    )
    await session.flush()

    partial = await sync_connector(session, connector)
    assert partial.status == "pending"
    assert partial.cursor == {}
    assert partial.stats["pending"] == 1
    assert partial.stats["distilled"] == 1
    assert list((await session.scalars(select(KnowledgeItem.source_ref))).all()) == [
        "slack:T1:C1:1"
    ]

    runs[1].status = "completed"
    session.add(
        AgentRunArtifactRow(
            run_id=runs[1].id,
            root_run_id=runs[1].root_run_id,
            artifact_type="final_answer",
            text=json.dumps(
                {
                    "question": "Question two?",
                    "summary": "Distilled two.",
                    "resolution": None,
                    "systems": [],
                    "code_references": [],
                }
            ),
        )
    )
    await session.flush()

    finished = await sync_connector(session, connector)
    assert finished.status == "ok"
    assert finished.cursor == {"position": 2}
    assert set((await session.scalars(select(KnowledgeItem.source_ref))).all()) == {
        "slack:T1:C1:1",
        "slack:T1:C1:2",
    }


async def test_knowledge_search_scopes_lexical_and_semantic_candidates_to_org(
    session,
    embedding_runtime,
):
    del embedding_runtime
    connector = _StubConnector(
        source_key="scoped",
        drafts=[
            KnowledgeDraft(
                source="scoped",
                kind="record",
                source_ref=f"scoped:{suffix}",
                scope=KnowledgeScope.ORGANIZATION,
                title="Shared deployment keyword",
                summary=f"Secret for org {suffix}",
                raw_text="shared deployment keyword",
                extra={"org_id": org_id},
            )
            for suffix, org_id in (("a", "org-a"), ("b", "org-b"))
        ],
    )
    await sync_connector(session, connector)

    result = await search_knowledge(
        session,
        "shared deployment keyword",
        org_id="org-a",
    )

    assert result["org_id"] == "org-a"
    assert result["semantic_available"] is True
    assert [item["source_ref"] for item in result["results"]] == ["scoped:a"]


async def test_knowledge_search_treats_missing_scope_as_organization_scoped(
    session,
    embedding_runtime,
):
    del embedding_runtime
    connector = _StubConnector(
        source_key="legacy-scoped",
        drafts=[
            KnowledgeDraft(
                source="legacy-scoped",
                kind="record",
                source_ref="legacy-scoped:a",
                scope=KnowledgeScope.ORGANIZATION,
                title="Legacy narwhal deployment",
                summary="Legacy organization-only deployment detail.",
                raw_text="legacy narwhal deployment",
                extra={"org_id": "org-a"},
            )
        ],
    )
    await sync_connector(session, connector)
    item = await session.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.source_ref == "legacy-scoped:a"
        )
    )
    assert item is not None
    legacy_extra = dict(item.extra)
    legacy_extra.pop(KNOWLEDGE_SCOPE_EXTRA_KEY)
    item.extra = legacy_extra
    await session.flush()

    own_org = await search_knowledge(
        session,
        "legacy narwhal deployment",
        org_id="org-a",
    )
    other_org = await search_knowledge(
        session,
        "legacy narwhal deployment",
        org_id="org-b",
    )

    assert [row["source_ref"] for row in own_org["results"]] == [
        "legacy-scoped:a"
    ]
    assert other_org["results"] == []


async def test_distillation_preserves_verified_github_resolution_when_model_omits_it(
    session,
    embedding_runtime,
):
    del embedding_runtime
    user_id = "55555555-5555-4555-8555-555555555555"
    structural_resolution = "Resolved by merged PR Illospace/illospace#583"
    session.add(Org(id=_ORG_ID, name="Closure Org", slug="closure-org"))
    session.add(
        User(
            id=user_id,
            org_id=_ORG_ID,
            name="Closure Worker",
            email="closure@example.com",
        )
    )
    await session.flush()
    connector = _StubConnector(
        source_key="github",
        drafts=[
            KnowledgeDraft(
                source="github",
                kind="issue",
                source_ref="github:Illospace/illospace#577",
                scope=KnowledgeScope.ORGANIZATION,
                title="Quasarlexeme closure title",
                summary="Closed issue awaiting distillation.",
                resolution=structural_resolution,
                raw_text="Implement the conversational knowledge layer.",
                extra={"org_id": _ORG_ID, "actor_user_id": user_id},
                distill=True,
            )
        ],
        new_cursor={"version": 2},
    )

    await sync_connector(session, connector)
    run = (await session.scalars(select(AgentRunRow))).one()
    run.status = "completed"
    session.add(
        AgentRunArtifactRow(
            run_id=run.id,
            root_run_id=run.root_run_id,
            artifact_type="final_answer",
            text=json.dumps(
                {
                    "question": "How was knowledge slice 2 completed?",
                    "summary": "The conversational knowledge layer shipped.",
                    "resolution": None,
                    "systems": ["knowledge"],
                    "code_references": [],
                }
            ),
        )
    )
    await session.flush()

    result = await sync_connector(session, connector)
    item = await session.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.source_ref == "github:Illospace/illospace#577"
        )
    )

    assert result.status == "ok"
    assert item is not None
    assert item.title == "Quasarlexeme closure title"
    assert item.resolution == structural_resolution
    search_result = await search_knowledge(
        session,
        "quasarlexeme",
        org_id=_ORG_ID,
    )
    assert [row["source_ref"] for row in search_result["results"]] == [
        "github:Illospace/illospace#577"
    ]


async def test_github_closure_enrichment_failure_retries_without_advancing_cursor(
    session,
    embedding_runtime,
):
    del embedding_runtime
    user_id = "66666666-6666-4666-8666-666666666666"
    repo = "Illospace/illospace"
    session.add(Org(id=_ORG_ID, name="Retry Org", slug="retry-org"))
    session.add(
        User(
            id=user_id,
            org_id=_ORG_ID,
            name="Retry Worker",
            email="retry@example.com",
        )
    )
    await session.flush()
    issue = {
        "id": 577,
        "number": 577,
        "title": "Knowledge slice 2",
        "state": "closed",
        "body": "Implement the conversational knowledge layer.",
        "labels": [],
        "user": {"login": "redawear"},
        "created_at": "2026-07-28T18:00:00Z",
        "updated_at": "2026-07-28T20:10:00Z",
        "closed_at": "2026-07-28T20:10:00Z",
    }
    closure = GithubIssueClosure(
        repo=repo,
        number=577,
        title="Knowledge slice 2",
        state="closed",
        closed_at=datetime(2026, 7, 28, 20, 10, tzinfo=timezone.utc),
        closed_by="redawear",
        fixing_pull_requests=(),
    )
    list_issues = AsyncMock(return_value={"issues": [issue], "next_page": None})
    get_closure = AsyncMock(side_effect=[RuntimeError("temporary"), closure])
    authority = _GitHubAuthority(token="token", org_id=_ORG_ID)
    connector = GitHubConnector(repositories=[repo])

    with patch(
        "brain.systems.knowledge.connectors.github._github_authority",
        new=AsyncMock(return_value=authority),
    ), patch(
        "brain.systems.knowledge.connectors.github.async_list_repo_issues",
        new=list_issues,
    ), patch(
        "brain.systems.knowledge.connectors.github.async_get_issue_closure_info",
        new=get_closure,
    ):
        failed = await sync_connector(session, connector)
        retried = await sync_connector(session, connector)

    assert failed.status == "degraded"
    assert failed.cursor == {
        "version": 2,
        "active_repository": 0,
        "repositories": {},
    }
    assert failed.stats["failed"] == 1
    assert failed.error == "Illospace/illospace: temporary"
    assert retried.status == "pending"
    assert retried.cursor == failed.cursor
    assert get_closure.await_count == 2
    state = await session.get(KnowledgeSyncState, "github")
    assert state is not None
    assert state.cursor["_distillation_pending"]["proposed_cursor"]["version"] == 2


async def test_github_legacy_cursor_is_reset_for_org_scope_backfill(session):
    repo = "Illospace/illospace"
    list_issues = AsyncMock(return_value={"issues": [], "next_page": None})
    authority = _GitHubAuthority(token="token", org_id=_ORG_ID)
    connector = GitHubConnector(repositories=[repo])
    legacy_cursor = {
        "active_repository": 0,
        "repositories": {
            repo: {
                "watermark": "2026-07-28T20:10:00+00:00",
                "watermark_id": 577,
            }
        },
    }

    with patch(
        "brain.systems.knowledge.connectors.github._github_authority",
        new=AsyncMock(return_value=authority),
    ), patch(
        "brain.systems.knowledge.connectors.github.async_list_repo_issues",
        new=list_issues,
    ):
        enumeration = await connector.enumerate_changed(session, legacy_cursor)

    assert enumeration.drafts == []
    assert list_issues.await_args.kwargs["since"] is None
    assert enumeration.cursor["version"] == 2
    assert enumeration.failures == ()


async def test_github_enumeration_reserves_capacity_for_stale_lower_index_repo(
    session,
):
    repositories = ["acme/stale", "acme/backfill"]
    initial_backfill_page = (
        "eyJraW5kIjoiZ2l0aHViX2lzc3VlczphY21lL2JhY2tmaWxsIiwiaW5kZXgiOjkwfQ=="
    )
    next_backfill_page = (
        "eyJraW5kIjoiZ2l0aHViX2lzc3VlczphY21lL2JhY2tmaWxsIiwiaW5kZXgiOjB9"
    )
    initial_cursor = {
        "version": 2,
        "active_repository": 1,
        "repositories": {
            "acme/stale": {
                "watermark": "2026-07-29T12:00:00+00:00",
                "watermark_id": 100,
            },
            "acme/backfill": {
                "next_page": initial_backfill_page,
                "high_watermark": "2026-07-30T12:00:00+00:00",
                "high_watermark_id": 200,
            },
        },
    }
    issues = {
        "acme/backfill": [
            {
                "id": issue_id,
                "number": issue_id,
                "title": f"Backfill issue {issue_id}",
                "state": "open",
                "body": "Historical backfill item.",
                "labels": [],
                "user": {"login": "octocat"},
                "created_at": "2026-07-30T12:00:00Z",
                "updated_at": f"2026-07-30T12:0{issue_id - 200}:00Z",
            }
            for issue_id in (201, 202, 203, 204, 205)
        ],
        "acme/stale": [
            {
                "id": 100,
                "number": 100,
                "title": "Already settled",
                "state": "open",
                "body": "This item is already behind the watermark.",
                "labels": [],
                "user": {"login": "octocat"},
                "created_at": "2026-07-29T12:00:00Z",
                "updated_at": "2026-07-29T12:00:00Z",
            },
            {
                "id": 101,
                "number": 101,
                "title": "Fresh lower-index issue",
                "state": "open",
                "body": "The stale repository gets a turn.",
                "labels": [],
                "user": {"login": "octocat"},
                "created_at": "2026-07-31T09:00:00Z",
                "updated_at": "2026-07-31T10:00:00Z",
            },
        ],
    }
    calls: dict[str, dict] = {}

    async def list_issues(repo, **kwargs):
        calls[repo] = kwargs
        return {
            "issues": issues[repo][: kwargs["limit"]],
            "next_page": next_backfill_page if repo == "acme/backfill" else None,
        }

    authority = _GitHubAuthority(token="token", org_id=_ORG_ID)
    connector = GitHubConnector(repositories=repositories, max_items=4)
    with patch(
        "brain.systems.knowledge.connectors.github._github_authority",
        new=AsyncMock(return_value=authority),
    ), patch(
        "brain.systems.knowledge.connectors.github.async_list_repo_issues",
        new=AsyncMock(side_effect=list_issues),
    ):
        enumeration = await connector.enumerate_changed(session, initial_cursor)

    assert [draft.source_ref for draft in enumeration.drafts] == [
        "github:acme/backfill#201",
        "github:acme/backfill#202",
        "github:acme/stale#101",
    ]
    assert calls["acme/backfill"]["cursor"] == initial_backfill_page
    assert calls["acme/backfill"]["since"] is None
    assert calls["acme/stale"]["cursor"] is None
    assert calls["acme/stale"]["since"] == "2026-07-29T12:00:00+00:00"
    assert enumeration.cursor == {
        "version": 2,
        "active_repository": 0,
        "repositories": {
            "acme/stale": {
                "watermark": "2026-07-31T10:00:00+00:00",
                "watermark_id": 101,
            },
            "acme/backfill": {
                "next_page": next_backfill_page,
                "high_watermark": "2026-07-30T12:02:00+00:00",
                "high_watermark_id": 202,
            },
        },
    }


async def test_github_enumeration_advances_every_repo_during_long_backfill(session):
    repositories = ["acme/steady-one", "acme/backfill", "acme/steady-two"]
    backfill_page_tokens = (
        "eyJraW5kIjoiZ2l0aHViX2lzc3VlczphY21lL2JhY2tmaWxsIiwiaW5kZXgiOjF9",
        "eyJraW5kIjoiZ2l0aHViX2lzc3VlczphY21lL2JhY2tmaWxsIiwiaW5kZXgiOjJ9",
        "eyJraW5kIjoiZ2l0aHViX2lzc3VlczphY21lL2JhY2tmaWxsIiwiaW5kZXgiOjN9",
    )
    initial_watermarks = {
        "acme/steady-one": ("2026-07-27T08:00:00+00:00", 10),
        "acme/steady-two": ("2026-07-27T08:00:00+00:00", 20),
    }
    cursor = {
        "version": 2,
        "active_repository": 1,
        "repositories": {
            repo: {"watermark": watermark, "watermark_id": row_id}
            for repo, (watermark, row_id) in initial_watermarks.items()
        }
        | {
            "acme/backfill": {
                "next_page": backfill_page_tokens[0],
                "high_watermark": "2026-07-28T08:00:00+00:00",
                "high_watermark_id": 30,
            }
        },
    }
    backfill_pages = {
        backfill_page_tokens[0]: (31, backfill_page_tokens[1]),
        backfill_page_tokens[1]: (32, backfill_page_tokens[2]),
        backfill_page_tokens[2]: (33, None),
    }
    steady_calls = {"acme/steady-one": 0, "acme/steady-two": 0}
    seen_backfill_pages: list[str] = []

    def issue(repo: str, issue_id: int, day: int) -> dict:
        return {
            "id": issue_id,
            "number": issue_id,
            "title": f"{repo} issue {issue_id}",
            "state": "open",
            "body": "Repository scheduling test item.",
            "labels": [],
            "user": {"login": "octocat"},
            "created_at": f"2026-07-{day:02d}T08:00:00Z",
            "updated_at": f"2026-07-{day:02d}T08:00:00Z",
        }

    async def list_issues(repo, **kwargs):
        assert kwargs["limit"] == 1
        if repo == "acme/backfill":
            page = kwargs["cursor"]
            seen_backfill_pages.append(page)
            issue_id, next_page = backfill_pages[page]
            return {
                "issues": [issue(repo, issue_id, issue_id - 2)],
                "next_page": next_page,
            }

        steady_calls[repo] += 1
        call = steady_calls[repo]
        issue_id = initial_watermarks[repo][1] + call
        return {
            "issues": [issue(repo, issue_id, 27 + call)],
            "next_page": None,
        }

    authority = _GitHubAuthority(token="token", org_id=_ORG_ID)
    connector = GitHubConnector(repositories=repositories, max_items=3)
    enumerations = []
    with patch(
        "brain.systems.knowledge.connectors.github._github_authority",
        new=AsyncMock(return_value=authority),
    ), patch(
        "brain.systems.knowledge.connectors.github.async_list_repo_issues",
        new=AsyncMock(side_effect=list_issues),
    ):
        for _ in range(3):
            enumeration = await connector.enumerate_changed(session, cursor)
            enumerations.append(enumeration)
            cursor = enumeration.cursor

    assert [len(enumeration.drafts) for enumeration in enumerations] == [3, 3, 3]
    assert seen_backfill_pages == list(backfill_page_tokens)
    assert steady_calls == {"acme/steady-one": 3, "acme/steady-two": 3}
    for repo, (initial_watermark, _) in initial_watermarks.items():
        assert cursor["repositories"][repo]["watermark"] > initial_watermark
    assert cursor["repositories"]["acme/backfill"] == {
        "watermark": "2026-07-31T08:00:00+00:00",
        "watermark_id": 33,
    }


async def test_github_enumeration_isolates_repo_failures_and_wraps_cursor(
    session,
    caplog,
):
    repositories = ["acme/healthy-one", "acme/missing", "acme/healthy-two"]
    initial_cursor = {
        "version": 2,
        "active_repository": 0,
        "repositories": {
            repo: {
                "watermark": "2026-07-29T12:00:00+00:00",
                "watermark_id": index,
            }
            for index, repo in enumerate(repositories, start=1)
        },
    }
    issues = {
        "acme/healthy-one": {
            "id": 101,
            "number": 11,
            "title": "First healthy issue",
            "state": "open",
            "body": "Healthy repository one advanced.",
            "labels": [],
            "user": {"login": "octocat"},
            "created_at": "2026-07-30T09:00:00Z",
            "updated_at": "2026-07-30T10:00:00Z",
        },
        "acme/healthy-two": {
            "id": 303,
            "number": 33,
            "title": "Second healthy issue",
            "state": "open",
            "body": "Healthy repository two advanced.",
            "labels": [],
            "user": {"login": "octocat"},
            "created_at": "2026-07-30T11:00:00Z",
            "updated_at": "2026-07-30T12:00:00Z",
        },
    }
    emit_issues = True

    async def list_issues(repo, **kwargs):
        del kwargs
        if repo == "acme/missing":
            raise GitHubConnectorError(
                status_code=404,
                message="Repository not found or not visible to this token.",
            )
        return {
            "issues": [issues[repo]] if emit_issues else [],
            "next_page": None,
        }

    authority = _GitHubAuthority(token="token", org_id=_ORG_ID)
    connector = GitHubConnector(repositories=repositories, max_items=10)
    with patch(
        "brain.systems.knowledge.connectors.github._github_authority",
        new=AsyncMock(return_value=authority),
    ), patch(
        "brain.systems.knowledge.connectors.github.async_list_repo_issues",
        new=AsyncMock(side_effect=list_issues),
    ):
        enumeration = await connector.enumerate_changed(session, initial_cursor)

        assert [draft.source_ref for draft in enumeration.drafts] == [
            "github:acme/healthy-one#11",
            "github:acme/healthy-two#33",
        ]
        cursor = enumeration.cursor
        assert cursor["repositories"]["acme/healthy-one"] == {
            "watermark": "2026-07-30T10:00:00+00:00",
            "watermark_id": 101,
        }
        assert cursor["repositories"]["acme/healthy-two"] == {
            "watermark": "2026-07-30T12:00:00+00:00",
            "watermark_id": 303,
        }
        assert (
            cursor["repositories"]["acme/missing"]
            == initial_cursor["repositories"]["acme/missing"]
        )
        assert cursor["active_repository"] == 0
        assert enumeration.failures == (
            EnumerationFailure(
                scope="acme/missing",
                message="Repository not found or not visible to this token.",
                reason_code=GITHUB_READ_CONNECTOR_ERROR,
            ),
        )

        emit_issues = False
        result = await sync_connector(
            session,
            GitHubConnector(repositories=repositories, max_items=10),
        )

    payload = result.to_dict()
    assert result.status == "degraded"
    assert result.stats["failed"] == 1
    assert "config_faults" not in result.stats
    assert payload["error"] == (
        "acme/missing: [github_connector_error] Repository not found or not "
        "visible to this token."
    )
    assert "config_faults" not in payload
    assert any(
        "acme/missing" in record.message
        and "Repository not found or not visible to this token." in record.message
        for record in caplog.records
    )


async def test_github_repository_access_denial_is_a_configuration_failure(session):
    repo = "acme/restricted"
    authority = _GitHubAuthority(token="token", org_id=_ORG_ID)
    list_issues = AsyncMock(
        side_effect=GitHubConnectorError(
            status_code=403,
            message="Resource not accessible by integration.",
        )
    )

    with patch(
        "brain.systems.knowledge.connectors.github._github_authority",
        new=AsyncMock(return_value=authority),
    ), patch(
        "brain.systems.knowledge.connectors.github.async_list_repo_issues",
        new=list_issues,
    ):
        enumeration = await GitHubConnector(
            repositories=[repo]
        ).enumerate_changed(session, {})

    assert enumeration.failures == (
        EnumerationFailure(
            scope=repo,
            message="Resource not accessible by integration.",
            kind=EnumerationFailureKind.CONFIGURATION,
            reason_code=GITHUB_READ_ACCESS_FORBIDDEN,
            remediation=(
                "Grant the installed GitHub App access to acme/restricted, then "
                "reconnect or update its active Vault project binding for this "
                "exact repository slug."
            ),
        ),
    )


async def test_github_connector_error_message_surfaces_in_failed_sync_payload(session):
    message = "Repository not found or not visible to this token."
    error = GitHubConnectorError(status_code=404, message=message)

    assert str(error) == message
    assert error.args == (message,)
    assert error.status_code == 404
    assert error.message == message
    assert error == GitHubConnectorError(status_code=404, message=message)
    assert repr(error) == (
        "GitHubConnectorError(status_code=404, "
        "message='Repository not found or not visible to this token.')"
    )

    connector = SimpleNamespace(
        source_key="github",
        enumerate_changed=AsyncMock(side_effect=error),
    )
    result = await sync_connector(session, connector)

    assert result.status == "failed"
    assert result.stats["failed"] == 1
    assert result.to_dict()["error"] == message


async def test_enumeration_error_survives_pending_distillation_as_degraded(
    session,
    embedding_runtime,
):
    del embedding_runtime
    user_id = "88888888-8888-4888-8888-888888888888"
    session.add(Org(id=_ORG_ID, name="Degraded Org", slug="degraded-org"))
    session.add(
        User(
            id=user_id,
            org_id=_ORG_ID,
            name="Degraded Worker",
            email="degraded@example.com",
        )
    )
    await session.flush()
    proposed_cursor = {
        "version": 2,
        "active_repository": 0,
        "repositories": {
            "acme/healthy": {
                "watermark": "2026-07-30T12:00:00+00:00",
                "watermark_id": 303,
            }
        },
    }
    connector = _StubConnector(
        source_key="github",
        drafts=[
            KnowledgeDraft(
                source="github",
                kind="issue",
                source_ref="github:acme/healthy#33",
                scope=KnowledgeScope.ORGANIZATION,
                title="Healthy issue",
                summary="A healthy repository continued indexing.",
                raw_text="Healthy repository body.",
                extra={"org_id": _ORG_ID, "actor_user_id": user_id},
                distill=True,
            )
        ],
        new_cursor=proposed_cursor,
        failures=(
            EnumerationFailure(
                scope="acme/missing",
                message="Repository not found or not visible to this token.",
            ),
        ),
    )
    expected_error = (
        "acme/missing: Repository not found or not visible to this token."
    )

    pending = await sync_connector(session, connector)
    state = await session.get(KnowledgeSyncState, "github")
    assert state is not None
    assert state.cursor["_distillation_pending"]["enumeration_errors"] == [
        {
            "scope": "acme/missing",
            "message": "Repository not found or not visible to this token.",
            "kind": "transient",
        }
    ]
    run = (await session.scalars(select(AgentRunRow))).one()
    run.status = "completed"
    session.add(
        AgentRunArtifactRow(
            run_id=run.id,
            root_run_id=run.root_run_id,
            artifact_type="final_answer",
            text=json.dumps(
                {
                    "question": "What changed in the healthy repository?",
                    "summary": "The healthy repository continued indexing.",
                    "resolution": None,
                    "systems": ["knowledge"],
                    "code_references": [],
                }
            ),
        )
    )
    await session.flush()

    degraded = await sync_connector(session, connector)

    assert pending.status == "pending"
    assert pending.stats["failed"] == 1
    assert pending.to_dict()["error"] == expected_error
    assert degraded.status == "degraded"
    assert degraded.stats["failed"] == 1
    assert degraded.cursor == proposed_cursor
    assert degraded.to_dict()["error"] == expected_error


async def test_github_knowledge_sweep_reports_missing_binding_without_read(
    session,
):
    repo = "Illospace/illospace"
    list_issues = AsyncMock()
    get_closure = AsyncMock()
    authority_session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    connector = GitHubConnector(repositories=[repo])

    async def missing_binding(_session, repository):
        return await _github_authority(authority_session, repository)

    with patch(
        "brain.systems.knowledge.connectors.github._github_authority",
        new=missing_binding,
    ), patch(
        "brain.systems.knowledge.connectors.github.async_list_repo_issues",
        new=list_issues,
    ), patch(
        "brain.systems.knowledge.connectors.github.async_get_issue_closure_info",
        new=get_closure,
    ):
        result = await sync_connector(session, connector)

    item = await session.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.source_ref == "github:Illospace/illospace#577"
        )
    )
    assert result.status == "degraded"
    assert result.cursor["version"] == 2
    assert result.stats["failed"] == 0
    assert result.stats["config_faults"] == 1
    assert result.to_dict()["config_faults"] == [
        {
            "scope": repo,
            "message": (
                "No active GitHub App Vault project binding exists for the "
                "knowledge sync."
            ),
            "kind": "configuration",
            "reason_code": GITHUB_READ_AUTHENTICATION_REQUIRED,
            "remediation": (
                "Install or reconnect the GitHub App for Illospace/illospace, then "
                "create an active VaultProjectBinding to a github_app secret for "
                "this exact repository slug."
            ),
        }
    ]
    list_issues.assert_not_awaited()
    get_closure.assert_not_awaited()
    assert item is None


async def test_github_knowledge_authority_reports_missing_binding():
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))

    with pytest.raises(
        _GitHubConfigurationError,
        match="No active GitHub App Vault project binding exists",
    ) as exc_info:
        await _github_authority(session, "Illospace/illospace")

    assert exc_info.value.reason_code == GITHUB_READ_AUTHENTICATION_REQUIRED
    assert not hasattr(exc_info.value, "status_code")


async def test_github_knowledge_authority_reports_unmintable_credential():
    repo = "uwear-ai/uwear-backend"
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(org_id=_ORG_ID))
    )
    resolve = AsyncMock(
        side_effect=RuntimeSecretUnavailable("GitHub App private key is invalid")
    )

    with patch(
        "brain.systems.knowledge.connectors.github."
        "async_resolve_org_project_bound_env_tokens",
        new=resolve,
    ), pytest.raises(
        _GitHubConfigurationError,
        match="credential could not mint a token",
    ) as exc_info:
        await _github_authority(session, repo)

    assert exc_info.value.reason_code == GITHUB_READ_AUTHENTICATION_REQUIRED
    assert exc_info.value.message.endswith("GitHub App private key is invalid")
    assert not hasattr(exc_info.value, "status_code")


async def test_github_knowledge_authority_reports_empty_token_resolution():
    repo = "uwear-ai/uwear-backend"
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(org_id=_ORG_ID))
    )

    with patch(
        "brain.systems.knowledge.connectors.github."
        "async_resolve_org_project_bound_env_tokens",
        new=AsyncMock(return_value={}),
    ), pytest.raises(
        _GitHubConfigurationError,
        match="did not resolve a token",
    ) as exc_info:
        await _github_authority(session, repo)

    assert exc_info.value.reason_code == GITHUB_READ_AUTHENTICATION_REQUIRED
    assert not hasattr(exc_info.value, "status_code")


async def test_github_knowledge_authority_mints_without_user_actor():
    repo = "uwear-ai/uwear-backend"
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(org_id=_ORG_ID))
    )
    resolve = AsyncMock(return_value={"GITHUB_TOKEN": "installation-token"})

    with patch(
        "brain.systems.knowledge.connectors.github."
        "async_resolve_org_project_bound_env_tokens",
        new=resolve,
    ):
        authority = await _github_authority(session, repo)

    assert authority.token == "installation-token"
    assert authority.org_id == _ORG_ID
    assert not hasattr(authority, "actor_user_id")
    resolve.assert_awaited_once_with(
        org_id=_ORG_ID,
        accessed_by="knowledge_index_sync",
        project_slug=repo,
        github_app_only=True,
        github_app_permissions={
            "contents": "read",
            "issues": "read",
            "pull_requests": "read",
        },
    )
