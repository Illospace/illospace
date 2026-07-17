"""Embedding writes and backfill support for reconstructive memory nodes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.config import MEMORY_SEMANTIC_EMBEDDING_DIM
from brain.platform.db.models.reconstructive_memory import MemoryAssertionNode, MemoryNode
from brain.platform.db.repositories.reconstructive_memory import MemoryNodeEmbeddingRepository
from brain.systems.memory import embeddings as embedding_client
from brain.systems.runtime_settings import memory as runtime_settings
from brain.systems.runtime_settings.memory import EmbeddingRuntimeConfig

logger = logging.getLogger(__name__)

EMBEDDABLE_NODE_KINDS = ("content", "summary", "procedure", "policy")


@dataclass(frozen=True)
class EmbeddingWriteResult:
    created: int = 0
    skipped: int = 0
    failed: int = 0

    def __add__(self, other: EmbeddingWriteResult) -> EmbeddingWriteResult:
        return EmbeddingWriteResult(
            created=self.created + other.created,
            skipped=self.skipped + other.skipped,
            failed=self.failed + other.failed,
        )


@dataclass(frozen=True)
class EmbeddingBackfillResult:
    scanned: int
    created: int
    skipped: int
    failed: int
    last_node_id: int | None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "scanned": self.scanned,
            "created": self.created,
            "skipped": self.skipped,
            "failed": self.failed,
            "last_node_id": self.last_node_id,
        }


@dataclass(frozen=True)
class RecallQueryEmbedding:
    vector: np.ndarray
    model: str


def embedding_model_identity(runtime: EmbeddingRuntimeConfig) -> str:
    """Return stable model metadata shared by writers and retrieval."""

    backend = runtime.backend.strip().lower()
    if backend == "api":
        model = f"{backend}:{runtime.provider.strip().lower()}:{runtime.api_model}"
    elif backend == "cpu":
        model = f"{backend}:{runtime.cpu_model}"
    else:
        model = f"{backend}:default"
    return model[:120]


def node_embedding_text(node: MemoryNode) -> str:
    return (node.text or node.canonical_label or "").strip()


async def embed_recall_query(
    session: AsyncSession,
    query: str,
) -> RecallQueryEmbedding | None:
    """Embed a recall query, returning ``None`` for loud lexical degradation."""

    try:
        runtime = await runtime_settings.async_get_embedding_runtime_config(
            session,
            include_secret=True,
        )
        vector = np.asarray(
            embedding_client.embed_query(query, runtime_config=runtime),
            dtype=np.float32,
        ).reshape(-1)
        dimension = int(vector.shape[0])
        if dimension != int(runtime.dimensions) or dimension != MEMORY_SEMANTIC_EMBEDDING_DIM:
            raise ValueError(
                "query embedding dimension mismatch "
                f"(returned={dimension}, runtime={runtime.dimensions}, "
                f"database={MEMORY_SEMANTIC_EMBEDDING_DIM})"
            )
        return RecallQueryEmbedding(vector=vector, model=embedding_model_identity(runtime))
    except Exception as exc:
        logger.warning(
            "Semantic reconstructive recall unavailable; using lexical ranking for query %r: %s",
            query[:120],
            exc,
        )
        return None


async def embed_node_texts(
    session: AsyncSession,
    *,
    node: MemoryNode,
    assertion_texts: Iterable[str] = (),
    runtime_config: EmbeddingRuntimeConfig | None = None,
) -> EmbeddingWriteResult:
    """Write missing content/assertion embeddings without failing the caller.

    Assertions get their own embedding only when their claim differs from the
    node text. Each failed vector remains an explicit, backfillable gap.
    """

    try:
        runtime = runtime_config or await runtime_settings.async_get_embedding_runtime_config(
            session,
            include_secret=True,
        )
    except Exception as exc:
        logger.warning(
            "Could not load embedding configuration for memory node %s; leaving a backfillable gap: %s",
            node.id,
            exc,
        )
        return EmbeddingWriteResult(failed=1)

    content_text = node_embedding_text(node)
    payloads: list[tuple[str, str]] = []
    if content_text:
        payloads.append(("content", content_text))

    normalized_content = " ".join(content_text.split()).casefold()
    seen_assertions: set[str] = set()
    for assertion_text in assertion_texts:
        cleaned = " ".join((assertion_text or "").split()).strip()
        normalized = cleaned.casefold()
        if not cleaned or normalized == normalized_content or normalized in seen_assertions:
            continue
        seen_assertions.add(normalized)
        payloads.append(("assertion", cleaned))

    result = EmbeddingWriteResult()
    for embedding_kind, text_value in payloads:
        result += await _embed_one(
            session,
            node=node,
            embedding_kind=embedding_kind,
            text_value=text_value,
            runtime_config=runtime,
        )
    return result


async def _embed_one(
    session: AsyncSession,
    *,
    node: MemoryNode,
    embedding_kind: str,
    text_value: str,
    runtime_config: EmbeddingRuntimeConfig,
) -> EmbeddingWriteResult:
    repository = MemoryNodeEmbeddingRepository(session)
    model = embedding_model_identity(runtime_config)
    digest = repository.content_digest(text_value)
    try:
        async with session.begin_nested():
            exists = await repository.exists(
                node_id=node.id,
                embedding_kind=embedding_kind,
                model=model,
                content_digest=digest,
            )
        if exists:
            return EmbeddingWriteResult(skipped=1)
        vector = np.asarray(
            embedding_client.embed_document(text_value, runtime_config=runtime_config),
            dtype=np.float32,
        ).reshape(-1)
        dimension = int(vector.shape[0])
        if dimension != int(runtime_config.dimensions) or dimension != MEMORY_SEMANTIC_EMBEDDING_DIM:
            raise ValueError(
                "embedding dimension mismatch "
                f"(returned={dimension}, runtime={runtime_config.dimensions}, "
                f"database={MEMORY_SEMANTIC_EMBEDDING_DIM})"
            )
        async with session.begin_nested():
            await repository.create(
                node_id=node.id,
                embedding_kind=embedding_kind,
                model=model,
                dimension=dimension,
                embedding=vector.tolist(),
                content_digest=digest,
            )
    except Exception as exc:
        logger.warning(
            "Embedding write failed for memory node %s (%s); leaving a backfillable gap: %s",
            node.id,
            embedding_kind,
            exc,
        )
        return EmbeddingWriteResult(failed=1)
    return EmbeddingWriteResult(created=1)


async def backfill_memory_node_embeddings(
    session: AsyncSession,
    *,
    batch_size: int = 25,
    after_id: int = 0,
    limit: int | None = None,
    commit_batches: bool = True,
) -> EmbeddingBackfillResult:
    """Embed retrievable memory nodes in resumable, id-ordered batches.

    Cue and tag nodes are deliberately excluded: they are routing/index graph
    primitives, while recall ranks content-bearing nodes directly. Embedding
    them would increase cost and let terse workspace vocabulary crowd the
    evidence candidates without adding source-backed answer text.
    """

    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")

    runtime = await runtime_settings.async_get_embedding_runtime_config(session, include_secret=True)
    cursor = max(0, int(after_id))
    scanned = 0
    total = EmbeddingWriteResult()
    last_node_id: int | None = None

    while limit is None or scanned < limit:
        current_batch_size = batch_size if limit is None else min(batch_size, limit - scanned)
        stmt = (
            select(MemoryNode)
            .where(MemoryNode.node_kind.in_(EMBEDDABLE_NODE_KINDS))
            .where(MemoryNode.id > cursor)
            .order_by(MemoryNode.id.asc())
            .limit(current_batch_size)
        )
        nodes = list((await session.scalars(stmt)).all())
        if not nodes:
            break

        assertions = list(
            (
                await session.scalars(
                    select(MemoryAssertionNode).where(
                        MemoryAssertionNode.node_id.in_([node.id for node in nodes])
                    )
                )
            ).all()
        )
        assertions_by_node: dict[int, list[str]] = {}
        for assertion in assertions:
            assertions_by_node.setdefault(assertion.node_id, []).append(assertion.claim_text)

        for node in nodes:
            total += await embed_node_texts(
                session,
                node=node,
                assertion_texts=assertions_by_node.get(node.id, ()),
                runtime_config=runtime,
            )

        scanned += len(nodes)
        cursor = nodes[-1].id
        last_node_id = cursor
        if commit_batches:
            await session.commit()

    return EmbeddingBackfillResult(
        scanned=scanned,
        created=total.created,
        skipped=total.skipped,
        failed=total.failed,
        last_node_id=last_node_id,
    )
