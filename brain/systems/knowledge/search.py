"""LLM-free hybrid knowledge retrieval with reciprocal-rank fusion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.config import KNOWLEDGE_EMBEDDING_DIM
from brain.platform.db.models.knowledge import KnowledgeItem, KnowledgeItemEmbedding

logger = logging.getLogger(__name__)

RRF_K = 60
LEXICAL_WEIGHT = 1.0
SEMANTIC_WEIGHT = 1.0
RECENCY_WEIGHT = 0.5
KNOWLEDGE_SEARCH_DEFAULT_RESULTS = 10
KNOWLEDGE_SEARCH_MAX_RESULTS = 50

_CHANNEL_NAMES = ("lexical", "semantic", "recency")
_HIT_FIELDS = frozenset(
    {
        "id",
        "source",
        "kind",
        "source_ref",
        "title",
        "summary",
        "resolution",
        "entities",
        "extra",
        "source_created_at",
        "source_updated_at",
        "scores",
    }
)
_RESPONSE_FIELDS = frozenset(
    {
        "query",
        "org_id",
        "sources",
        "kinds",
        "semantic_available",
        "semantic_degraded_reason",
        "weights",
        "requested_limit",
        "effective_limit",
        "results",
    }
)


class KnowledgeSearchContractError(ValueError):
    """Raised when a knowledge-search payload violates its response contract."""


def normalize_knowledge_search_limit(
    limit: int | None,
    *,
    default: int = KNOWLEDGE_SEARCH_DEFAULT_RESULTS,
) -> int:
    """Return the canonical bounded retrieval depth for knowledge search."""

    requested = int(limit or default)
    return max(1, min(requested, KNOWLEDGE_SEARCH_MAX_RESULTS))


def _contract_mapping(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise KnowledgeSearchContractError(f"{path} must be an object")
    return value


def _contract_exact_fields(
    value: Mapping[str, Any],
    *,
    expected: frozenset[str],
    path: str,
) -> None:
    fields = frozenset(value)
    if fields == expected:
        return
    details: list[str] = []
    missing = sorted(expected - fields)
    unexpected = sorted(fields - expected)
    if missing:
        details.append(f"missing fields: {', '.join(missing)}")
    if unexpected:
        details.append(f"unexpected fields: {', '.join(unexpected)}")
    raise KnowledgeSearchContractError(
        f"{path} has invalid fields ({'; '.join(details)})"
    )


def _contract_text(
    value: Any,
    *,
    path: str,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        suffix = "a string" if allow_empty else "a non-empty string"
        raise KnowledgeSearchContractError(f"{path} must be {suffix}")
    return value


def _contract_optional_text(value: Any, *, path: str) -> str | None:
    if value is None:
        return None
    return _contract_text(value, path=path, allow_empty=True)


def _contract_int(value: Any, *, path: str, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KnowledgeSearchContractError(f"{path} must be an integer")
    if positive and value < 1:
        raise KnowledgeSearchContractError(f"{path} must be positive")
    return value


def _contract_float(value: Any, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KnowledgeSearchContractError(f"{path} must be numeric")
    return float(value)


@dataclass(frozen=True)
class KnowledgeSearchChannelScore:
    """One channel's contribution to a fused knowledge result."""

    rank: int
    score: float | None
    weight: float
    contribution: float

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        path: str,
    ) -> KnowledgeSearchChannelScore:
        value = _contract_mapping(payload, path=path)
        _contract_exact_fields(
            value,
            expected=frozenset({"rank", "score", "weight", "contribution"}),
            path=path,
        )
        raw_score = value["score"]
        return cls(
            rank=_contract_int(value["rank"], path=f"{path}.rank", positive=True),
            score=(
                None
                if raw_score is None
                else _contract_float(raw_score, path=f"{path}.score")
            ),
            weight=_contract_float(value["weight"], path=f"{path}.weight"),
            contribution=_contract_float(
                value["contribution"],
                path=f"{path}.contribution",
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "score": self.score,
            "weight": self.weight,
            "contribution": self.contribution,
        }


@dataclass(frozen=True)
class KnowledgeSearchScores:
    """Canonical fused score and per-channel attribution."""

    rrf: float
    lexical: KnowledgeSearchChannelScore | None
    semantic: KnowledgeSearchChannelScore | None
    recency: KnowledgeSearchChannelScore | None

    @classmethod
    def from_payload(cls, payload: Any, *, path: str) -> KnowledgeSearchScores:
        value = _contract_mapping(payload, path=path)
        _contract_exact_fields(
            value,
            expected=frozenset({"rrf", "channels"}),
            path=path,
        )
        raw_channels = _contract_mapping(
            value["channels"],
            path=f"{path}.channels",
        )
        _contract_exact_fields(
            raw_channels,
            expected=frozenset(_CHANNEL_NAMES),
            path=f"{path}.channels",
        )
        channels: dict[str, KnowledgeSearchChannelScore | None] = {}
        for channel_name in _CHANNEL_NAMES:
            raw_channel = raw_channels[channel_name]
            channels[channel_name] = (
                None
                if raw_channel is None
                else KnowledgeSearchChannelScore.from_payload(
                    raw_channel,
                    path=f"{path}.channels.{channel_name}",
                )
            )
        return cls(
            rrf=_contract_float(value["rrf"], path=f"{path}.rrf"),
            lexical=channels["lexical"],
            semantic=channels["semantic"],
            recency=channels["recency"],
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "rrf": self.rrf,
            "channels": {
                channel_name: (
                    channel.to_payload() if channel is not None else None
                )
                for channel_name, channel in (
                    ("lexical", self.lexical),
                    ("semantic", self.semantic),
                    ("recency", self.recency),
                )
            },
        }


@dataclass(frozen=True)
class KnowledgeSearchHit:
    """Validated knowledge result with stable provenance and score attribution."""

    id: int
    source: str
    kind: str
    source_ref: str
    title: str
    summary: str
    resolution: str | None
    entities: tuple[Any, ...]
    extra: dict[str, Any]
    source_created_at: str | None
    source_updated_at: str | None
    scores: KnowledgeSearchScores

    @classmethod
    def from_payload(cls, payload: Any, *, path: str) -> KnowledgeSearchHit:
        value = _contract_mapping(payload, path=path)
        _contract_exact_fields(value, expected=_HIT_FIELDS, path=path)
        raw_entities = value["entities"]
        if not isinstance(raw_entities, list):
            raise KnowledgeSearchContractError(f"{path}.entities must be a list")
        raw_extra = value["extra"]
        if not isinstance(raw_extra, Mapping):
            raise KnowledgeSearchContractError(f"{path}.extra must be an object")
        return cls(
            id=_contract_int(value["id"], path=f"{path}.id", positive=True),
            source=_contract_text(value["source"], path=f"{path}.source"),
            kind=_contract_text(value["kind"], path=f"{path}.kind"),
            source_ref=_contract_text(
                value["source_ref"],
                path=f"{path}.source_ref",
            ),
            title=_contract_text(
                value["title"],
                path=f"{path}.title",
                allow_empty=True,
            ),
            summary=_contract_text(
                value["summary"],
                path=f"{path}.summary",
                allow_empty=True,
            ),
            resolution=_contract_optional_text(
                value["resolution"],
                path=f"{path}.resolution",
            ),
            entities=tuple(raw_entities),
            extra=dict(raw_extra),
            source_created_at=_contract_optional_text(
                value["source_created_at"],
                path=f"{path}.source_created_at",
            ),
            source_updated_at=_contract_optional_text(
                value["source_updated_at"],
                path=f"{path}.source_updated_at",
            ),
            scores=KnowledgeSearchScores.from_payload(
                value["scores"],
                path=f"{path}.scores",
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "kind": self.kind,
            "source_ref": self.source_ref,
            "title": self.title,
            "summary": self.summary,
            "resolution": self.resolution,
            "entities": list(self.entities),
            "extra": dict(self.extra),
            "source_created_at": self.source_created_at,
            "source_updated_at": self.source_updated_at,
            "scores": self.scores.to_payload(),
        }


@dataclass(frozen=True)
class KnowledgeSearchResponse:
    """Validated envelope returned by the knowledge search producer."""

    query: str
    org_id: str
    sources: tuple[str, ...]
    kinds: tuple[str, ...]
    semantic_available: bool
    semantic_degraded_reason: str | None
    lexical_weight: float
    semantic_weight: float
    recency_weight: float
    requested_limit: int
    effective_limit: int
    results: tuple[KnowledgeSearchHit, ...]

    @classmethod
    def from_payload(cls, payload: Any) -> KnowledgeSearchResponse:
        value = _contract_mapping(payload, path="knowledge_search")
        _contract_exact_fields(
            value,
            expected=_RESPONSE_FIELDS,
            path="knowledge_search",
        )
        raw_sources = value["sources"]
        raw_kinds = value["kinds"]
        if not isinstance(raw_sources, list) or not all(
            isinstance(item, str) for item in raw_sources
        ):
            raise KnowledgeSearchContractError(
                "knowledge_search.sources must be a list of strings"
            )
        if not isinstance(raw_kinds, list) or not all(
            isinstance(item, str) for item in raw_kinds
        ):
            raise KnowledgeSearchContractError(
                "knowledge_search.kinds must be a list of strings"
            )
        semantic_available = value["semantic_available"]
        if not isinstance(semantic_available, bool):
            raise KnowledgeSearchContractError(
                "knowledge_search.semantic_available must be a boolean"
            )
        degraded_reason = _contract_optional_text(
            value["semantic_degraded_reason"],
            path="knowledge_search.semantic_degraded_reason",
        )
        if semantic_available and degraded_reason is not None:
            raise KnowledgeSearchContractError(
                "semantic_degraded_reason must be null when semantic search is available"
            )
        if not semantic_available and not degraded_reason:
            raise KnowledgeSearchContractError(
                "semantic_degraded_reason is required when semantic search is unavailable"
            )
        raw_weights = _contract_mapping(
            value["weights"],
            path="knowledge_search.weights",
        )
        _contract_exact_fields(
            raw_weights,
            expected=frozenset(_CHANNEL_NAMES),
            path="knowledge_search.weights",
        )
        raw_results = value["results"]
        if not isinstance(raw_results, list):
            raise KnowledgeSearchContractError(
                "knowledge_search.results must be a list"
            )
        requested_limit = _contract_int(
            value["requested_limit"],
            path="knowledge_search.requested_limit",
        )
        effective_limit = _contract_int(
            value["effective_limit"],
            path="knowledge_search.effective_limit",
            positive=True,
        )
        if effective_limit > KNOWLEDGE_SEARCH_MAX_RESULTS:
            raise KnowledgeSearchContractError(
                "knowledge_search.effective_limit cannot exceed "
                f"{KNOWLEDGE_SEARCH_MAX_RESULTS}"
            )
        return cls(
            query=_contract_text(value["query"], path="knowledge_search.query"),
            org_id=_contract_text(value["org_id"], path="knowledge_search.org_id"),
            sources=tuple(raw_sources),
            kinds=tuple(raw_kinds),
            semantic_available=semantic_available,
            semantic_degraded_reason=degraded_reason,
            lexical_weight=_contract_float(
                raw_weights["lexical"],
                path="knowledge_search.weights.lexical",
            ),
            semantic_weight=_contract_float(
                raw_weights["semantic"],
                path="knowledge_search.weights.semantic",
            ),
            recency_weight=_contract_float(
                raw_weights["recency"],
                path="knowledge_search.weights.recency",
            ),
            requested_limit=requested_limit,
            effective_limit=effective_limit,
            results=tuple(
                KnowledgeSearchHit.from_payload(
                    item,
                    path=f"knowledge_search.results[{index}]",
                )
                for index, item in enumerate(raw_results)
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "org_id": self.org_id,
            "sources": list(self.sources),
            "kinds": list(self.kinds),
            "semantic_available": self.semantic_available,
            "semantic_degraded_reason": self.semantic_degraded_reason,
            "weights": {
                "lexical": self.lexical_weight,
                "semantic": self.semantic_weight,
                "recency": self.recency_weight,
            },
            "requested_limit": self.requested_limit,
            "effective_limit": self.effective_limit,
            "results": [result.to_payload() for result in self.results],
        }


def parse_knowledge_search_response(payload: Any) -> KnowledgeSearchResponse:
    """Validate and type a serialized knowledge-search response."""

    return KnowledgeSearchResponse.from_payload(payload)


def serialize_knowledge_search_response(
    response: KnowledgeSearchResponse,
) -> dict[str, Any]:
    """Serialize the canonical typed knowledge-search response."""

    return KnowledgeSearchResponse.from_payload(response.to_payload()).to_payload()

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


def _item_filters(
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
        KnowledgeItem.extra["org_id"].as_string() == clean_org_id,
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
        degraded_reason = str(exc).strip() or type(exc).__name__
        logger.warning(
            "Semantic knowledge recall unavailable; using lexical ranking for query %r: %s",
            query[:120],
            degraded_reason,
        )
        return [], {}, degraded_reason

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


def _channel_score(
    debug: Mapping[str, Any],
    *,
    channel_name: str,
    raw_score: float | None,
) -> KnowledgeSearchChannelScore | None:
    channel_debug = debug["channels"]
    if channel_name not in channel_debug:
        return None
    channel = channel_debug[channel_name]
    return KnowledgeSearchChannelScore(
        rank=int(channel["rank"]),
        score=raw_score,
        weight=float(channel["weight"]),
        contribution=float(channel["contribution"]),
    )


def _result_contract(
    item: KnowledgeItem,
    *,
    debug: dict[str, Any],
    lexical_scores: Mapping[int, float],
    semantic_scores: Mapping[int, float],
) -> KnowledgeSearchHit:
    return KnowledgeSearchHit(
        id=item.id,
        source=item.source,
        kind=item.kind,
        source_ref=item.source_ref,
        title=item.title,
        summary=item.summary,
        resolution=item.resolution,
        entities=tuple(item.entities or []),
        extra=dict(item.extra or {}),
        source_created_at=_iso(item.source_created_at),
        source_updated_at=_iso(item.source_updated_at),
        scores=KnowledgeSearchScores(
            rrf=round(float(debug["rrf"]), 8),
            lexical=_channel_score(
                debug,
                channel_name="lexical",
                raw_score=lexical_scores.get(item.id),
            ),
            semantic=_channel_score(
                debug,
                channel_name="semantic",
                raw_score=semantic_scores.get(item.id),
            ),
            recency=_channel_score(
                debug,
                channel_name="recency",
                raw_score=None,
            ),
        ),
    )


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
    filters = _item_filters(
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
        _result_contract(
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
        sources=tuple(str(value) for value in sources or []),
        kinds=tuple(str(value) for value in kinds or []),
        semantic_available=semantic_error is None,
        semantic_degraded_reason=semantic_error,
        lexical_weight=LEXICAL_WEIGHT,
        semantic_weight=SEMANTIC_WEIGHT,
        recency_weight=RECENCY_WEIGHT,
        requested_limit=requested_limit,
        effective_limit=max_results,
        results=tuple(results),
    )
    return serialize_knowledge_search_response(response)


__all__ = [
    "KNOWLEDGE_SEARCH_DEFAULT_RESULTS",
    "KNOWLEDGE_SEARCH_MAX_RESULTS",
    "KnowledgeSearchChannelScore",
    "KnowledgeSearchContractError",
    "KnowledgeSearchHit",
    "KnowledgeSearchResponse",
    "KnowledgeSearchScores",
    "LEXICAL_WEIGHT",
    "RECENCY_WEIGHT",
    "RRF_K",
    "SEMANTIC_WEIGHT",
    "normalize_knowledge_search_limit",
    "parse_knowledge_search_response",
    "reciprocal_rank_fusion",
    "search_knowledge",
    "serialize_knowledge_search_response",
]
