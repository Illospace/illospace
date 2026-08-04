"""LLM-free hybrid knowledge retrieval with reciprocal-rank fusion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import logging
import re
from typing import Any

import numpy as np
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.config import KNOWLEDGE_EMBEDDING_DIM
from brain.platform.db.models.knowledge import KnowledgeItem, KnowledgeItemEmbedding
from brain.systems.knowledge.scope import (
    KNOWLEDGE_SCOPE_EXTRA_KEY,
    KnowledgeScope,
)
from brain.systems.knowledge.search_contract import (
    KNOWLEDGE_SEARCH_DEFAULT_RESULTS,
    KnowledgeSearchResponse,
    normalize_knowledge_search_limit,
)

logger = logging.getLogger(__name__)

RRF_K = 60
LEXICAL_WEIGHT = 1.0
SEMANTIC_WEIGHT = 1.0
RECENCY_WEIGHT = 0.5

_QUERY_TERM_RE = re.compile(r"[a-zA-Z0-9_/-]{3,}")
_QUERY_STOP_WORDS = {
    "about",
    "and",
    "are",
    "did",
    "does",
    "for",
    "from",
    "has",
    "have",
    "how",
    "into",
    "that",
    "the",
    "this",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


def _query_terms(query: str) -> tuple[str, ...]:
    terms: list[str] = []
    seen: set[str] = set()
    for match in _QUERY_TERM_RE.finditer(query.casefold()):
        term = match.group(0)
        if term in _QUERY_STOP_WORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= 8:
            break
    return tuple(terms)


def _lexical_relevance(item: KnowledgeItem, *, query: str, terms: Sequence[str]) -> float:
    fields = (
        (item.title or "").casefold(),
        (item.summary or "").casefold(),
        (item.search_text or "").casefold(),
    )
    normalized_query = " ".join(query.casefold().split())
    exact = any(field == normalized_query for field in fields if field)
    if not terms:
        return 1.0 if exact else 0.0
    hits = sum(1 for term in terms if any(term in field for field in fields))
    return max(1.0 if exact else 0.0, hits / len(terms))


def _coerce_vector(value: Any) -> np.ndarray:
    if isinstance(value, str):
        cleaned = value.strip().removeprefix("[").removesuffix("]")
        return np.fromstring(cleaned, sep=",", dtype=np.float32)
    return np.asarray(value, dtype=np.float32).reshape(-1)


def _cosine_similarity(left: np.ndarray, right: np.ndarray) -> float | None:
    if left.shape != right.shape or not left.size:
        return None
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator <= 0.0:
        return None
    return float(np.dot(left, right) / denominator)


def reciprocal_rank_fusion(
    ranked_lists: Mapping[str, Sequence[int]],
    *,
    weights: Mapping[str, float] | None = None,
) -> tuple[list[int], dict[int, dict[str, Any]]]:
    """Fuse deduplicated ranked lists and retain per-channel debug scores."""

    channel_weights = {
        "lexical": LEXICAL_WEIGHT,
        "semantic": SEMANTIC_WEIGHT,
        "recency": RECENCY_WEIGHT,
        **dict(weights or {}),
    }
    debug: dict[int, dict[str, Any]] = {}
    for channel, ranked_ids in ranked_lists.items():
        seen: set[int] = set()
        weight = float(channel_weights.get(channel, 1.0))
        for rank, item_id in enumerate(ranked_ids, start=1):
            if item_id in seen:
                continue
            seen.add(item_id)
            contribution = weight / (RRF_K + rank)
            item_debug = debug.setdefault(item_id, {"rrf": 0.0, "channels": {}})
            item_debug["rrf"] += contribution
            item_debug["channels"][channel] = {
                "rank": rank,
                "weight": weight,
                "contribution": contribution,
            }
    ordered = sorted(
        debug,
        key=lambda item_id: (
            -float(debug[item_id]["rrf"]),
            item_id,
        ),
    )
    return ordered, debug


def knowledge_item_filters(
    *,
    org_id: str,
    sources: Sequence[str] | None,
    kinds: Sequence[str] | None,
) -> list[Any]:
    clean_org_id = str(org_id or "").strip()
    if not clean_org_id:
        raise ValueError("org_id is required for knowledge search")
    filters: list[Any] = [
        KnowledgeItem.archived_at.is_(None),
        or_(
            KnowledgeItem.extra["org_id"].as_string() == clean_org_id,
            KnowledgeItem.extra[KNOWLEDGE_SCOPE_EXTRA_KEY].as_string()
            == KnowledgeScope.GLOBAL.value,
        ),
    ]
    clean_sources = [str(value).strip() for value in sources or [] if str(value).strip()]
    clean_kinds = [str(value).strip() for value in kinds or [] if str(value).strip()]
    if clean_sources:
        filters.append(KnowledgeItem.source.in_(clean_sources))
    if clean_kinds:
        filters.append(KnowledgeItem.kind.in_(clean_kinds))
    return filters


async def _lexical_channel(
    session: AsyncSession,
    *,
    query: str,
    filters: Sequence[Any],
    channel_limit: int,
) -> tuple[list[KnowledgeItem], dict[int, float]]:
    if session.get_bind().dialect.name == "postgresql":
        # word_similarity scores the query against the best-matching substring,
        # so short queries stay findable inside long search_text blobs (plain
        # similarity() is symmetric and starves exact-token matches in long docs).
        relevance = func.word_similarity(query, KnowledgeItem.search_text)
        stmt = (
            select(KnowledgeItem, relevance.label("lexical_score"))
            .where(*filters)
            .where(KnowledgeItem.search_text.op("%>")(query))
            .order_by(relevance.desc(), KnowledgeItem.id.asc())
            .limit(channel_limit)
        )
        rows = list((await session.execute(stmt)).all())
        return (
            [item for item, _score in rows],
            {item.id: float(score) for item, score in rows},
        )

    items = list((await session.scalars(select(KnowledgeItem).where(*filters))).all())
    terms = _query_terms(query)
    scored = [
        (item, _lexical_relevance(item, query=query, terms=terms))
        for item in items
    ]
    scored = [(item, score) for item, score in scored if score > 0.0]
    scored.sort(key=lambda pair: (-pair[1], pair[0].id))
    selected = scored[:channel_limit]
    return (
        [item for item, _score in selected],
        {item.id: score for item, score in selected},
    )


async def _semantic_channel(
    session: AsyncSession,
    *,
    query: str,
    filters: Sequence[Any],
    channel_limit: int,
) -> tuple[list[KnowledgeItem], dict[int, float], str | None]:
    try:
        from brain.systems.memory import embeddings as embedding_client
        from brain.systems.reconstructive_memory.embeddings import embedding_model_identity
        from brain.systems.runtime_settings import memory as runtime_settings

        runtime = await runtime_settings.async_get_embedding_runtime_config(
            session,
            include_secret=True,
        )
        query_vector = np.asarray(
            embedding_client.embed_query(query, runtime_config=runtime),
            dtype=np.float32,
        ).reshape(-1)
        dimension = int(query_vector.shape[0])
        if dimension != int(runtime.dimensions) or dimension != KNOWLEDGE_EMBEDDING_DIM:
            raise ValueError(
                "knowledge query embedding dimension mismatch "
                f"(returned={dimension}, runtime={runtime.dimensions}, "
                f"database={KNOWLEDGE_EMBEDDING_DIM})"
            )
        model = embedding_model_identity(runtime)
    except Exception as exc:
        logger.warning(
            "Semantic knowledge recall unavailable; using lexical ranking for query %r: %s",
            query[:120],
            exc,
        )
        return [], {}, str(exc)

    embedding_filters = [
        KnowledgeItemEmbedding.embedding_kind == "summary",
        KnowledgeItemEmbedding.embedding.isnot(None),
        KnowledgeItemEmbedding.model == model,
        KnowledgeItemEmbedding.dimension == dimension,
        KnowledgeItemEmbedding.content_digest == KnowledgeItem.content_digest,
    ]
    if session.get_bind().dialect.name == "postgresql":
        similarity = 1.0 - KnowledgeItemEmbedding.embedding.cosine_distance(
            query_vector.tolist()
        )
        stmt = (
            select(KnowledgeItem, similarity.label("semantic_score"))
            .join(
                KnowledgeItemEmbedding,
                KnowledgeItemEmbedding.item_id == KnowledgeItem.id,
            )
            .where(*filters, *embedding_filters)
            .order_by(similarity.desc(), KnowledgeItem.id.asc())
            .limit(channel_limit)
        )
        rows = list((await session.execute(stmt)).all())
        return (
            [item for item, _score in rows],
            {item.id: float(score) for item, score in rows},
            None,
        )

    stmt = (
        select(KnowledgeItem, KnowledgeItemEmbedding)
        .join(
            KnowledgeItemEmbedding,
            KnowledgeItemEmbedding.item_id == KnowledgeItem.id,
        )
        .where(*filters, *embedding_filters)
    )
    scores: dict[int, tuple[KnowledgeItem, float]] = {}
    for item, embedding in (await session.execute(stmt)).all():
        similarity = _cosine_similarity(
            query_vector,
            _coerce_vector(embedding.embedding),
        )
        if similarity is None:
            continue
        previous = scores.get(item.id)
        if previous is None or similarity > previous[1]:
            scores[item.id] = (item, similarity)
    ranked = sorted(scores.values(), key=lambda pair: (-pair[1], pair[0].id))
    selected = ranked[:channel_limit]
    return (
        [item for item, _score in selected],
        {item.id: score for item, score in selected},
        None,
    )


def _recency_value(item: KnowledgeItem) -> tuple[datetime, int]:
    value = item.source_updated_at or item.source_created_at
    if value is None:
        value = datetime.min.replace(tzinfo=timezone.utc)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value, item.id


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _serialize_result(
    item: KnowledgeItem,
    *,
    debug: dict[str, Any],
    lexical_scores: Mapping[int, float],
    semantic_scores: Mapping[int, float],
) -> dict[str, Any]:
    channel_debug = debug["channels"]
    channel_scores = {
        channel_name: (
            {
                **channel_debug[channel_name],
                "score": raw_score,
            }
            if channel_name in channel_debug
            else None
        )
        for channel_name, raw_score in (
            ("lexical", lexical_scores.get(item.id)),
            ("semantic", semantic_scores.get(item.id)),
            ("recency", None),
        )
    }
    return {
        "id": item.id,
        "source": item.source,
        "kind": item.kind,
        "source_ref": item.source_ref,
        "title": item.title,
        "summary": item.summary,
        "resolution": item.resolution,
        "entities": list(item.entities or []),
        "extra": dict(item.extra or {}),
        "source_created_at": _iso(item.source_created_at),
        "source_updated_at": _iso(item.source_updated_at),
        "scores": {
            "rrf": round(float(debug["rrf"]), 8),
            "channels": channel_scores,
        },
    }


async def search_knowledge(
    session: AsyncSession,
    query: str,
    *,
    org_id: str,
    sources: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search non-archived index rows across lexical and semantic channels."""

    clean_query = str(query or "").strip()
    if not clean_query:
        raise ValueError("query is required")
    requested_limit = int(limit or KNOWLEDGE_SEARCH_DEFAULT_RESULTS)
    max_results = normalize_knowledge_search_limit(limit)
    channel_limit = min(200, max(20, max_results * 4))
    clean_org_id = str(org_id or "").strip()
    filters = knowledge_item_filters(
        org_id=clean_org_id,
        sources=sources,
        kinds=kinds,
    )

    lexical_items, lexical_scores = await _lexical_channel(
        session,
        query=clean_query,
        filters=filters,
        channel_limit=channel_limit,
    )
    semantic_items, semantic_scores, semantic_error = await _semantic_channel(
        session,
        query=clean_query,
        filters=filters,
        channel_limit=channel_limit,
    )
    candidates = {
        item.id: item
        for item in [*lexical_items, *semantic_items]
    }
    recency_items = sorted(candidates.values(), key=_recency_value, reverse=True)
    ordered_ids, debug = reciprocal_rank_fusion(
        {
            "lexical": [item.id for item in lexical_items],
            "semantic": [item.id for item in semantic_items],
            # Recency is deliberately restricted to the candidate union.
            "recency": [item.id for item in recency_items],
        }
    )
    results = [
        _serialize_result(
            candidates[item_id],
            debug=debug[item_id],
            lexical_scores=lexical_scores,
            semantic_scores=semantic_scores,
        )
        for item_id in ordered_ids[:max_results]
    ]
    response = KnowledgeSearchResponse(
        query=clean_query,
        org_id=clean_org_id,
        sources=[str(value) for value in sources or []],
        kinds=[str(value) for value in kinds or []],
        semantic_available=semantic_error is None,
        semantic_degraded_reason=semantic_error,
        weights={
            "lexical": LEXICAL_WEIGHT,
            "semantic": SEMANTIC_WEIGHT,
            "recency": RECENCY_WEIGHT,
        },
        requested_limit=requested_limit,
        effective_limit=max_results,
        results=results,
    )
    return response.model_dump(mode="json")


__all__ = [
    "LEXICAL_WEIGHT",
    "RECENCY_WEIGHT",
    "RRF_K",
    "SEMANTIC_WEIGHT",
    "knowledge_item_filters",
    "reciprocal_rank_fusion",
    "search_knowledge",
]
