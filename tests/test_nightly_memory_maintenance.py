"""Acceptance coverage for auditable nightly memory expiry maintenance."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

from brain.jobs.pipelines.nightly_memory_maintenance import (
    EXPIRY_POLICY_VERSION,
    EXPIRY_RULE,
    run_nightly_memory_maintenance,
)
from brain.platform.db.models.org import Org, User
from brain.platform.db.models.reconstructive_memory import (
    MemoryAssertionNode,
    MemoryEdgeNode,
    MemoryNode,
    MemorySource,
    MemorySpan,
    ReconstructionEvidence,
    ReconstructionFeedback,
    ReconstructionRun,
    ReconstructionStep,
)
from brain.platform.db.models.system import ConsolidationRun
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
from brain.systems.reconstructive_memory.curation import archive_memory_by_policy

SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "VARCHAR(36)"

pytestmark = pytest.mark.asyncio

ORG_ID = "00000000000000000000000000000417"
USER_ID = "00000000000000000000000000000418"
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


async def _session(async_sqlite_session_factory, sqlite_postgres_ddl_patch):
    del sqlite_postgres_ddl_patch
    session = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            MemorySource.__table__,
            MemorySpan.__table__,
            MemoryNode.__table__,
            MemoryAssertionNode.__table__,
            MemoryEdgeNode.__table__,
            ConsolidationRun.__table__,
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
        {"id": ORG_ID, "name": "Memory Maintenance Org", "slug": "memory-maintenance-org"},
    )
    await session.execute(
        text(
            "INSERT INTO users (id, org_id, name, email) "
            "VALUES (:id, :org_id, :name, :email)"
        ),
        {
            "id": USER_ID,
            "org_id": ORG_ID,
            "name": "Memory Maintainer",
            "email": "memory-maintainer@example.com",
        },
    )
    return session


async def _source_backed_content(
    session,
    *,
    label: str,
    content_kind: str,
    valid_until: datetime | None,
):
    source, spans = await MemorySourceRepository(session).create_with_spans(
        source_kind="test_fixture",
        source_ref=f"fixture:{label}",
        raw_content=label,
        spans=[SourceSpanDraft(text=label, locator={"kind": "fixture"})],
        org_id=ORG_ID,
        user_id=USER_ID,
        visibility="org",
        authority_principal=USER_ID,
    )
    node = await MemoryNodeRepository(session).upsert_node(
        draft=NodeDraft(
            node_kind="content",
            content_kind=content_kind,
            canonical_label=label,
            text=label,
            confidence=0.9,
            truth_status="active",
            freshness_status="fresh",
        ),
        org_id=ORG_ID,
        user_id=USER_ID,
        visibility="org",
    )
    node.valid_until = valid_until
    assertion = await MemoryAssertionRepository(session).create_assertion(
        draft=AssertionDraft(
            node_id=node.id,
            claim_text=label,
            confidence=0.9,
            truth_status="active",
            source_span_ids=(spans[0].id,),
        )
    )
    return source, spans[0], node, assertion


class _UnitOfWorkForSession:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            await self.session.flush()
        else:
            await self.session.rollback()
        return False


async def _recalled_ids(session, monkeypatch, query: str) -> set[int]:
    async def no_embedding(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "brain.systems.reconstructive_memory.controller.embed_recall_query",
        no_embedding,
    )
    pack = await reconstruct_memory(
        session,
        query=query,
        org_id=ORG_ID,
        user_id=USER_ID,
        limit=20,
    )
    return {item.node_id for item in pack.supporting_evidence}


async def test_expiry_maintenance_is_auditable_idempotent_and_recall_safe(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
    monkeypatch,
):
    session = await _session(async_sqlite_session_factory, sqlite_postgres_ddl_patch)
    cutoff = NOW - timedelta(days=7)
    eligible = await _source_backed_content(
        session,
        label="Eligible alpha transient fact",
        content_kind="fact",
        valid_until=cutoff,
    )
    policy = await _source_backed_content(
        session,
        label="Protected policy retention rule",
        content_kind="policy",
        valid_until=cutoff - timedelta(days=1),
    )
    procedure = await _source_backed_content(
        session,
        label="Protected procedure deployment steps",
        content_kind="procedure",
        valid_until=cutoff - timedelta(days=1),
    )
    lesson = await _source_backed_content(
        session,
        label="Protected lesson validation practice",
        content_kind="lesson",
        valid_until=cutoff - timedelta(days=1),
    )
    summary_support = await _source_backed_content(
        session,
        label="Supported beta durable evidence",
        content_kind="fact",
        valid_until=cutoff - timedelta(days=1),
    )
    recent = await _source_backed_content(
        session,
        label="Recent gamma transient fact",
        content_kind="fact",
        valid_until=NOW - timedelta(days=6),
    )

    summary = await MemoryNodeRepository(session).upsert_node(
        draft=NodeDraft(
            node_kind="summary",
            content_kind="summary",
            canonical_label="Current durable beta summary",
            text="Current durable beta summary",
            confidence=0.9,
            truth_status="active",
            freshness_status="fresh",
        ),
        org_id=ORG_ID,
        user_id=USER_ID,
        visibility="org",
    )
    await MemoryAssertionRepository(session).create_assertion(
        draft=AssertionDraft(
            node_id=summary.id,
            claim_text="Current durable beta summary",
            confidence=0.9,
            truth_status="active",
            review_status="reviewed",
            source_span_ids=(summary_support[1].id,),
        )
    )
    summary_edge = await MemoryEdgeRepository(session).upsert_edge(
        draft=EdgeDraft(
            source_node_id=summary.id,
            target_node_id=summary_support[2].id,
            edge_kind="derived_from",
            confidence=1.0,
            evidence_span_ids=(summary_support[1].id,),
            created_by="test_fixture",
        ),
        org_id=ORG_ID,
        visibility="org",
    )
    eligible_cue = await MemoryNodeRepository(session).upsert_node(
        draft=NodeDraft(node_kind="cue", canonical_label="eligible-alpha", confidence=0.8),
        org_id=ORG_ID,
        user_id=USER_ID,
        visibility="org",
    )
    eligible_edge = await MemoryEdgeRepository(session).upsert_edge(
        draft=EdgeDraft(
            source_node_id=eligible_cue.id,
            target_node_id=eligible[2].id,
            edge_kind="cue_to_content",
            confidence=0.8,
            evidence_span_ids=(eligible[1].id,),
            created_by="test_fixture",
        ),
        org_id=ORG_ID,
        visibility="org",
    )
    await session.flush()

    baseline_counts = {
        model: await session.scalar(select(func.count()).select_from(model))
        for model in (MemoryNode, MemoryAssertionNode, MemoryEdgeNode)
    }
    monkeypatch.setattr(
        "brain.jobs.pipelines.nightly_memory_maintenance.UnitOfWork",
        lambda: _UnitOfWorkForSession(session),
    )

    dry_run = await run_nightly_memory_maintenance(
        date(2026, 7, 22),
        org_id=ORG_ID,
        apply=False,
        now=NOW,
    )
    assert dry_run["mode"] == "dry_run"
    assert dry_run["candidates"] == 5
    assert dry_run["eligible_node_ids"] == [eligible[2].id]
    assert dry_run["archived"] == 0
    assert dry_run["excluded_by_rule"] == {
        "protected_kind": 3,
        "supports_active_current_summary": 1,
    }
    assert dry_run["errors"] == 0
    assert dry_run["error_details"] == []
    assert (await session.get(MemoryNode, eligible[2].id)).archived_at is None
    assert await session.scalar(
        select(func.count()).select_from(MemorySource).where(
            MemorySource.source_kind == "memory_curator"
        )
    ) == 0

    first_apply = await run_nightly_memory_maintenance(
        date(2026, 7, 22),
        org_id=ORG_ID,
        apply=True,
        now=NOW,
    )
    second_apply = await run_nightly_memory_maintenance(
        date(2026, 7, 22),
        org_id=ORG_ID,
        apply=True,
        now=NOW,
    )
    assert first_apply["archived"] == 1
    assert first_apply["archived_node_ids"] == [eligible[2].id]
    assert second_apply["archived"] == 0
    assert second_apply["eligible"] == 0
    assert (await session.get(MemoryNode, eligible[2].id)).archived_at is not None
    for fixture in (policy, procedure, lesson, summary_support, recent):
        assert (await session.get(MemoryNode, fixture[2].id)).archived_at is None

    runs = list(
        (
            await session.scalars(
                select(ConsolidationRun).order_by(ConsolidationRun.id)
            )
        ).all()
    )
    assert [run.phase for run in runs] == ["nightly_memory_maintenance"] * 3
    assert [run.status for run in runs] == ["completed"] * 3
    assert [run.memories_decayed for run in runs] == [0, 1, 0]
    summaries = [json.loads(run.summary) for run in runs]
    assert [summary_payload["archived"] for summary_payload in summaries] == [0, 1, 0]
    assert all(summary_payload["duration_ms"] >= 0 for summary_payload in summaries)
    assert all(summary_payload["errors"] == 0 for summary_payload in summaries)
    assert all(summary_payload["error_details"] == [] for summary_payload in summaries)

    curator_source = (
        await session.scalars(
            select(MemorySource).where(MemorySource.source_kind == "memory_curator")
        )
    ).one()
    curator_span = (
        await session.scalars(
            select(MemorySpan).where(MemorySpan.source_id == curator_source.id)
        )
    ).one()
    assert curator_source.structured_payload == {
        "action": "archive",
        "rule": EXPIRY_RULE,
        "policy_version": EXPIRY_POLICY_VERSION,
        "target_node": eligible[2].id,
        "run_id": str(first_apply["run_id"]),
        "reason": curator_source.structured_payload["reason"],
        "created_by": "memory_curator",
    }
    for expected in (
        EXPIRY_RULE,
        EXPIRY_POLICY_VERSION,
        str(eligible[2].id),
        str(first_apply["run_id"]),
        curator_source.structured_payload["reason"],
    ):
        assert expected in curator_span.text

    assert await session.get(MemorySource, eligible[0].id) is not None
    assert await session.get(MemorySpan, eligible[1].id) is not None
    assert await session.get(MemoryAssertionNode, eligible[3].id) is not None
    assert await session.get(MemoryEdgeNode, eligible_edge.id) is not None
    assert await session.get(MemoryEdgeNode, summary_edge.id) is not None
    assert {
        model: await session.scalar(select(func.count()).select_from(model))
        for model in (MemoryNode, MemoryAssertionNode, MemoryEdgeNode)
    } == baseline_counts

    assert eligible[2].id not in await _recalled_ids(
        session,
        monkeypatch,
        "Eligible alpha transient fact",
    )
    for fixture in (policy, procedure, lesson, summary_support):
        assert fixture[2].id in await _recalled_ids(
            session,
            monkeypatch,
            fixture[2].text,
        )

    await session.execute(
        update(MemoryNode)
        .where(MemoryNode.id == eligible[2].id)
        .values(archived_at=None)
    )
    assert eligible[2].id in await _recalled_ids(
        session,
        monkeypatch,
        "Eligible alpha transient fact",
    )


async def test_apply_error_rolls_back_all_archives_and_records_failure(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
    monkeypatch,
):
    session = await _session(async_sqlite_session_factory, sqlite_postgres_ddl_patch)
    first = await _source_backed_content(
        session,
        label="First rollback candidate",
        content_kind="fact",
        valid_until=NOW - timedelta(days=8),
    )
    second = await _source_backed_content(
        session,
        label="Second rollback candidate",
        content_kind="fact",
        valid_until=NOW - timedelta(days=8),
    )
    monkeypatch.setattr(
        "brain.jobs.pipelines.nightly_memory_maintenance.UnitOfWork",
        lambda: _UnitOfWorkForSession(session),
    )
    calls = 0

    async def fail_after_first_archive(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated curator failure")
        return await archive_memory_by_policy(*args, **kwargs)

    monkeypatch.setattr(
        "brain.jobs.pipelines.nightly_memory_maintenance.archive_memory_by_policy",
        fail_after_first_archive,
    )

    report = await run_nightly_memory_maintenance(
        date(2026, 7, 22),
        org_id=ORG_ID,
        apply=True,
        now=NOW,
    )
    await session.refresh(first[2])
    await session.refresh(second[2])

    assert report["candidates"] == 2
    assert report["eligible"] == 2
    assert report["archived"] == 0
    assert report["errors"] == 1
    assert report["error_details"] == [
        {"type": "RuntimeError", "message": "simulated curator failure"}
    ]
    assert first[2].archived_at is None
    assert second[2].archived_at is None
    assert await session.scalar(
        select(func.count()).select_from(MemorySource).where(
            MemorySource.source_kind == "memory_curator"
        )
    ) == 0
    run = await session.get(ConsolidationRun, report["run_id"])
    assert run.status == "failed"
    assert run.memories_decayed == 0
    assert json.loads(run.summary)["errors"] == 1
