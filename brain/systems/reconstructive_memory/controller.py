"""Deterministic reconstructive-memory controller.

This is the first working replacement primitive for legacy top-k memory recall.
It records a reconstruction run, searches source-backed content nodes, attaches
assertions/source spans as evidence, and returns an evidence pack.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.reconstructive_memory import MemorySpan
from brain.platform.db.repositories.reconstructive_memory import (
    MemoryAssertionRepository,
    MemoryNodeRepository,
    ReconstructionRepository,
)
from brain.systems.reconstructive_memory.contracts import EvidenceItem, EvidencePack, ReconstructionTraceStep


async def reconstruct_memory(
    session: AsyncSession,
    *,
    query: str,
    org_id: str | None = None,
    user_id: str | None = None,
    limit: int = 5,
    run_id: int | None = None,
    thread_id: str | None = None,
) -> EvidencePack:
    """Build a source-backed evidence pack from the new memory graph."""

    reconstruction_repo = ReconstructionRepository(session)
    node_repo = MemoryNodeRepository(session)
    assertion_repo = MemoryAssertionRepository(session)

    run = await reconstruction_repo.start_run(
        query_text=query,
        query_kind="fact_lookup",
        org_id=org_id,
        user_id=user_id,
        run_id=run_id,
        thread_id=thread_id,
        budget_steps=3,
        policy_version="deterministic-v1",
    )

    candidates = await node_repo.search_content_nodes(
        query=query,
        org_id=org_id,
        user_id=user_id,
        limit=limit,
    )
    await reconstruction_repo.add_step(
        reconstruction_run_id=run.id,
        step_index=0,
        action_kind="seed_cues",
        action_input={"query": query},
        action_output={"candidate_count": len(candidates)},
        selected_node_ids=[node.id for node in candidates],
        reason="lexical cue seed over source-backed content nodes",
    )

    assertions = await assertion_repo.list_for_nodes([node.id for node in candidates])
    assertion_by_node = {assertion.node_id: assertion for assertion in assertions}
    span_ids = {
        span_id
        for assertion in assertions
        for span_id in (assertion.source_span_ids or [])
        if span_id is not None
    }
    spans = await _load_spans(session, span_ids)

    supporting: list[EvidenceItem] = []
    for rank, node in enumerate(candidates):
        assertion = assertion_by_node.get(node.id)
        source_span_id = None
        source_text = None
        if assertion and assertion.source_span_ids:
            source_span_id = assertion.source_span_ids[0]
            span = spans.get(source_span_id)
            source_text = span.text if span else None
        item = EvidenceItem(
            node_id=node.id,
            assertion_id=assertion.id if assertion else None,
            source_span_id=source_span_id,
            role="supports_answer",
            text=(assertion.claim_text if assertion else node.text or node.canonical_label),
            source_text=source_text,
            confidence=float(assertion.confidence if assertion else node.confidence),
        )
        supporting.append(item)
        await reconstruction_repo.add_evidence(
            reconstruction_run_id=run.id,
            node_id=item.node_id,
            assertion_id=item.assertion_id,
            source_span_id=item.source_span_id,
            role=item.role,
            confidence=item.confidence,
            rank=rank,
        )

    await reconstruction_repo.add_step(
        reconstruction_run_id=run.id,
        step_index=1,
        action_kind="summarize_evidence",
        action_input={"node_ids": [node.id for node in candidates]},
        action_output={"supporting_evidence_count": len(supporting)},
        selected_node_ids=[item.node_id for item in supporting],
        reason="materialized source spans and assertions into an evidence pack",
    )
    confidence = _pack_confidence(supporting)
    await reconstruction_repo.complete_run(run.id, confidence=confidence)

    trajectory = (
        ReconstructionTraceStep(
            action_kind="seed_cues",
            reason="lexical cue seed over source-backed content nodes",
            selected_node_ids=tuple(node.id for node in candidates),
            output={"candidate_count": len(candidates)},
        ),
        ReconstructionTraceStep(
            action_kind="summarize_evidence",
            reason="materialized source spans and assertions into an evidence pack",
            selected_node_ids=tuple(item.node_id for item in supporting),
            output={"supporting_evidence_count": len(supporting)},
        ),
    )
    unresolved = () if supporting else (f"No source-backed evidence found for: {query}",)
    return EvidencePack(
        reconstruction_run_id=run.id,
        query=query,
        confidence=confidence,
        supporting_evidence=tuple(supporting),
        trajectory=trajectory,
        unresolved_questions=unresolved,
    )


async def _load_spans(session: AsyncSession, span_ids: set[int]) -> dict[int, MemorySpan]:
    if not span_ids:
        return {}
    rows = (await session.scalars(select(MemorySpan).where(MemorySpan.id.in_(sorted(span_ids))))).all()
    return {row.id: row for row in rows}


def _pack_confidence(items: list[EvidenceItem]) -> float:
    if not items:
        return 0.0
    return round(sum(item.confidence for item in items) / len(items), 4)
