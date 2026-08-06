"""Source-backed ingestion for reconstructive memory.

This replaces legacy "write a memory blob" behavior with a small deterministic
extraction pass that creates source spans, content nodes, assertions, cue/tag
nodes, and graph edges in one transaction.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.repositories.unit_of_work import UnitOfWork
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

logger = logging.getLogger(__name__)

_INFO_QUEUE_KEY = "illo_memory_knowledge_index_queue"
_INFO_ARMED_KEY = "illo_memory_knowledge_index_listeners_armed"
_POST_COMMIT_TASKS: set[asyncio.Task] = set()

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_STOP_WORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "because",
    "been",
    "before",
    "being",
    "but",
    "can",
    "could",
    "did",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "into",
    "its",
    "not",
    "our",
    "out",
    "should",
    "that",
    "the",
    "their",
    "then",
    "there",
    "this",
    "was",
    "were",
    "when",
    "with",
    "would",
}


@dataclass(frozen=True)
class IngestedMemorySource:
    source_id: int
    span_ids: tuple[int, ...]
    content_node_id: int
    assertion_id: int
    cue_node_ids: tuple[int, ...]
    tag_node_ids: tuple[int, ...]
    edge_ids: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_system": "reconstructive",
            "source_id": self.source_id,
            "span_ids": list(self.span_ids),
            "content_node_id": self.content_node_id,
            "assertion_id": self.assertion_id,
            "cue_node_ids": list(self.cue_node_ids),
            "tag_node_ids": list(self.tag_node_ids),
            "edge_ids": list(self.edge_ids),
        }


async def _index_committed_memory_node(node_id: int) -> None:
    """Best-effort mirror write after the memory transaction is durable."""

    from brain.systems.knowledge.service import index_memory_node

    try:
        async with UnitOfWork() as uow:
            stats = await index_memory_node(uow.session, node_id=node_id)
            if stats.failed:
                logger.warning(
                    "Immediate knowledge indexing degraded for committed memory node %s",
                    node_id,
                )
    except Exception:
        # Knowledge is a disposable mirror. A failed index write must never
        # change the successful outcome of the committed memory ingest.
        logger.exception(
            "Immediate knowledge indexing failed for committed memory node %s",
            node_id,
        )


async def _index_committed_memory_nodes(node_ids: list[int]) -> None:
    for node_id in node_ids:
        await _index_committed_memory_node(node_id)


def _reap_knowledge_index_task(task: asyncio.Task) -> None:
    _POST_COMMIT_TASKS.discard(task)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.exception(
            "Immediate knowledge indexing failed for committed memory node",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def _spawn_knowledge_index_task(node_ids: list[int]) -> None:
    try:
        task = asyncio.ensure_future(_index_committed_memory_nodes(node_ids))
        _POST_COMMIT_TASKS.add(task)
        task.add_done_callback(_reap_knowledge_index_task)
    except Exception:
        logger.exception(
            "Immediate knowledge indexing failed for committed memory node %s",
            node_ids,
        )


def _schedule_post_commit_knowledge_index(
    session: AsyncSession,
    *,
    node_id: int,
) -> bool:
    """Queue immediate indexing for the session's next outer commit."""

    try:
        sync_session = getattr(session, "sync_session", None)
        if sync_session is None:
            return False
        loop = asyncio.get_running_loop()
        queue: list[int] = sync_session.info.setdefault(_INFO_QUEUE_KEY, [])
        queue.append(int(node_id))
        if sync_session.info.get(_INFO_ARMED_KEY):
            return True

        def _drain_into_task(target_session: Any) -> None:
            # SQLAlchemy fires after_commit for a savepoint release too. The
            # nested transaction is still available during that callback, so
            # keep the queue until the outer transaction commits.
            previous = target_session.get_nested_transaction()
            if getattr(previous, "nested", False):
                return
            drained = list(dict.fromkeys(target_session.info.get(_INFO_QUEUE_KEY) or []))
            target_session.info[_INFO_QUEUE_KEY] = []
            if not drained:
                return
            try:
                loop.call_soon_threadsafe(_spawn_knowledge_index_task, drained)
            except Exception:
                logger.exception(
                    "Immediate knowledge indexing failed for committed memory node %s",
                    drained,
                )

        def _on_rollback(target_session: Any, previous: Any) -> None:
            if getattr(previous, "nested", False):
                return
            target_session.info[_INFO_QUEUE_KEY] = []

        from sqlalchemy import event

        event.listen(sync_session, "after_commit", _drain_into_task)
        event.listen(sync_session, "after_soft_rollback", _on_rollback)
        sync_session.info[_INFO_ARMED_KEY] = True
        return True
    except Exception as exc:
        logger.warning(
            "Immediate knowledge indexing not armed for memory node %s (%s); "
            "the periodic sweep will deliver it",
            node_id,
            exc,
        )
        return False


async def ingest_memory_source(
    session: AsyncSession,
    *,
    content: str,
    content_kind: str = "episode",
    source_kind: str = "agent_run",
    source_ref: str | None = None,
    source_url: str | None = None,
    org_id: str | None = None,
    user_id: str | None = None,
    visibility: str = "private",
    scope_key: str = "default",
    confidence: float = 0.5,
    evidence: dict[str, Any] | None = None,
    authority_principal: str | None = None,
) -> IngestedMemorySource:
    """Ingest one source-backed reconstructive memory item."""

    cleaned = _clean_content(content)
    source_repo = MemorySourceRepository(session)
    node_repo = MemoryNodeRepository(session)
    edge_repo = MemoryEdgeRepository(session)
    assertion_repo = MemoryAssertionRepository(session)

    source, spans = await source_repo.create_with_spans(
        source_kind=source_kind,
        source_ref=source_ref,
        source_url=source_url,
        raw_content=cleaned,
        spans=[SourceSpanDraft(text=cleaned, locator={"kind": "full_content"})],
        org_id=org_id,
        user_id=user_id,
        visibility=visibility,
        structured_payload={"content_kind": content_kind, "evidence": dict(evidence or {})},
        authority_principal=authority_principal or user_id,
    )
    span_ids = tuple(span.id for span in spans)

    content_node = await node_repo.upsert_node(
        draft=NodeDraft(
            node_kind="content",
            content_kind=_normalize_content_kind(content_kind),
            canonical_label=_canonical_label(cleaned),
            text=cleaned,
            scope_key=scope_key,
            confidence=confidence,
            truth_status="active",
            freshness_status="fresh",
        ),
        org_id=org_id,
        user_id=user_id,
        visibility=visibility,
    )
    assertion = await assertion_repo.create_assertion(
        draft=AssertionDraft(
            node_id=content_node.id,
            claim_text=cleaned,
            confidence=confidence,
            truth_status="active",
            source_span_ids=span_ids,
        )
    )

    tag_nodes = []
    for label in _tag_labels(content_kind):
        tag_nodes.append(
            await node_repo.upsert_node(
                draft=NodeDraft(
                    node_kind="tag",
                    canonical_label=label,
                    confidence=confidence,
                    truth_status="active",
                    freshness_status="fresh",
                ),
                org_id=org_id,
                user_id=user_id,
                visibility=visibility,
            )
        )

    cue_nodes = []
    for cue in _extract_cues(cleaned):
        cue_nodes.append(
            await node_repo.upsert_node(
                draft=NodeDraft(
                    node_kind="cue",
                    canonical_label=cue,
                    confidence=max(0.4, confidence - 0.1),
                    truth_status="active",
                    freshness_status="fresh",
                ),
                org_id=org_id,
                user_id=user_id,
                visibility=visibility,
            )
        )

    edges = []
    for tag in tag_nodes:
        edges.append(
            await edge_repo.upsert_edge(
                draft=EdgeDraft(
                    source_node_id=tag.id,
                    target_node_id=content_node.id,
                    edge_kind="tag_to_content",
                    confidence=confidence,
                    evidence_span_ids=span_ids,
                    created_by="deterministic_ingestion",
                ),
                org_id=org_id,
                visibility=visibility,
            )
        )
    for cue in cue_nodes:
        for tag in tag_nodes:
            edges.append(
                await edge_repo.upsert_edge(
                    draft=EdgeDraft(
                        source_node_id=cue.id,
                        target_node_id=tag.id,
                        edge_kind="cue_to_tag",
                        confidence=max(0.4, confidence - 0.1),
                        evidence_span_ids=span_ids,
                        created_by="deterministic_ingestion",
                    ),
                    org_id=org_id,
                    visibility=visibility,
                )
            )
        edges.append(
            await edge_repo.upsert_edge(
                draft=EdgeDraft(
                    source_node_id=content_node.id,
                    target_node_id=cue.id,
                    edge_kind="content_to_cue",
                    confidence=max(0.4, confidence - 0.1),
                    evidence_span_ids=span_ids,
                    created_by="deterministic_ingestion",
                ),
                org_id=org_id,
                visibility=visibility,
            )
        )

    try:
        from brain.systems.reconstructive_memory.embeddings import embed_node_texts

        await embed_node_texts(
            session,
            node=content_node,
            assertion_texts=(assertion.claim_text,),
        )
    except Exception as exc:
        # Embeddings are a recoverable derivative. The complete source-backed
        # graph remains valid and the backfill CLI can fill this explicit gap.
        logger.warning(
            "Memory graph ingest completed but embedding failed for node %s; "
            "leaving a backfillable gap: %s",
            content_node.id,
            exc,
        )

    result = IngestedMemorySource(
        source_id=source.id,
        span_ids=span_ids,
        content_node_id=content_node.id,
        assertion_id=assertion.id,
        cue_node_ids=tuple(node.id for node in cue_nodes),
        tag_node_ids=tuple(node.id for node in tag_nodes),
        edge_ids=tuple(edge.id for edge in edges),
    )
    _schedule_post_commit_knowledge_index(
        session,
        node_id=result.content_node_id,
    )
    return result


def _clean_content(content: str) -> str:
    return re.sub(r"\s+", " ", content).strip()


def _canonical_label(content: str) -> str:
    first_sentence = re.split(r"(?<=[.!?])\s+", content, maxsplit=1)[0]
    return first_sentence[:140].strip() or content[:140]


def _normalize_content_kind(value: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", (value or "episode").strip().lower()).strip("_")
    return normalized or "episode"


def _tag_labels(content_kind: str | None) -> tuple[str, ...]:
    kind = _normalize_content_kind(content_kind)
    return ("memory", kind) if kind != "memory" else ("memory",)


def _extract_cues(content: str, *, limit: int = 8) -> tuple[str, ...]:
    seen: set[str] = set()
    cues: list[str] = []
    for match in _WORD_RE.finditer(content):
        word = match.group(0).lower()
        if word in _STOP_WORDS or word in seen:
            continue
        seen.add(word)
        cues.append(word)
        if len(cues) >= limit:
            break
    return tuple(cues)
