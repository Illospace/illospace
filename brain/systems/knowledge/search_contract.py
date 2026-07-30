"""Canonical typed contract for knowledge-search requests and responses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

KNOWLEDGE_SEARCH_DEFAULT_RESULTS = 10
KNOWLEDGE_SEARCH_MAX_RESULTS = 50


def normalize_knowledge_search_limit(
    limit: int | None,
    *,
    default: int = KNOWLEDGE_SEARCH_DEFAULT_RESULTS,
) -> int:
    """Return the canonical bounded retrieval depth for knowledge search."""

    requested = int(limit or default)
    return max(1, min(requested, KNOWLEDGE_SEARCH_MAX_RESULTS))


class _StrictKnowledgeSearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class KnowledgeSearchChannelScore(_StrictKnowledgeSearchModel):
    """One channel's contribution to a fused knowledge result."""

    rank: int = Field(ge=1)
    score: float | None
    weight: float
    contribution: float


class KnowledgeSearchScoreChannels(_StrictKnowledgeSearchModel):
    """Per-channel score attribution for one fused result."""

    lexical: KnowledgeSearchChannelScore | None
    semantic: KnowledgeSearchChannelScore | None
    recency: KnowledgeSearchChannelScore | None


class KnowledgeSearchScores(_StrictKnowledgeSearchModel):
    """Canonical fused score and its channel attribution."""

    rrf: float
    channels: KnowledgeSearchScoreChannels


class KnowledgeSearchChannelWeights(_StrictKnowledgeSearchModel):
    """Weights applied to the knowledge-search ranking channels."""

    lexical: float
    semantic: float
    recency: float


class KnowledgeSearchHit(_StrictKnowledgeSearchModel):
    """One knowledge-search result with stable provenance."""

    id: int = Field(ge=1)
    source: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    title: str
    summary: str
    resolution: str | None
    entities: list[Any]
    extra: dict[str, Any]
    source_created_at: str | None
    source_updated_at: str | None
    scores: KnowledgeSearchScores


class KnowledgeSearchResponse(_StrictKnowledgeSearchModel):
    """Strict envelope shared by the search producer and its evaluators."""

    query: str = Field(min_length=1)
    org_id: str = Field(min_length=1)
    sources: list[str]
    kinds: list[str]
    semantic_available: bool
    semantic_degraded_reason: str | None
    weights: KnowledgeSearchChannelWeights
    requested_limit: int
    effective_limit: int = Field(ge=1, le=KNOWLEDGE_SEARCH_MAX_RESULTS)
    results: list[KnowledgeSearchHit]


__all__ = [
    "KNOWLEDGE_SEARCH_DEFAULT_RESULTS",
    "KNOWLEDGE_SEARCH_MAX_RESULTS",
    "KnowledgeSearchChannelScore",
    "KnowledgeSearchChannelWeights",
    "KnowledgeSearchHit",
    "KnowledgeSearchResponse",
    "KnowledgeSearchScoreChannels",
    "KnowledgeSearchScores",
    "normalize_knowledge_search_limit",
]
