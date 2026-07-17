"""Repositories for reconstructive memory graph persistence."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import numpy as np
from sqlalchemy import and_, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.reconstructive_memory import (
    MemoryAssertionNode,
    MemoryEdgeNode,
    MemoryNode,
    MemoryNodeEmbedding,
    MemorySource,
    MemorySpan,
    ReconstructionEvidence,
    ReconstructionRun,
    ReconstructionStep,
)
from brain.platform.db.repositories.base import BaseRepository

_CONTENT_NODE_KINDS = ("content", "summary", "procedure", "policy")
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
_MIN_SEMANTIC_CANDIDATE_SCORE = 0.35


def stable_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def estimate_tokens(value: str) -> int:
    return max(1, len(value) // 4) if value else 0


@dataclass(frozen=True)
class SourceSpanDraft:
    text: str
    span_kind: str = "text"
    locator: dict[str, Any] | None = None


@dataclass(frozen=True)
class NodeDraft:
    node_kind: str
    canonical_label: str
    text: str | None = None
    content_kind: str | None = None
    normalized_key: str | None = None
    scope_key: str = "default"
    confidence: float = 0.5
    truth_status: str = "unknown"
    freshness_status: str = "unknown"
    sensitivity: str = "low"
    source_span_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class EdgeDraft:
    source_node_id: int
    target_node_id: int
    edge_kind: str
    weight: float = 1.0
    confidence: float = 0.5
    evidence_span_ids: tuple[int, ...] = ()
    created_by: str = "extractor"


@dataclass(frozen=True)
class AssertionDraft:
    node_id: int
    claim_text: str
    subject_node_id: int | None = None
    predicate: str | None = None
    object_node_id: int | None = None
    object_text: str | None = None
    confidence: float = 0.5
    truth_status: str = "unknown"
    review_status: str = "unreviewed"
    source_span_ids: tuple[int, ...] = ()


class MemorySourceRepository(BaseRepository[MemorySource]):
    model = MemorySource

    async def create_with_spans(
        self,
        *,
        source_kind: str,
        raw_content: str,
        spans: Sequence[SourceSpanDraft] | None = None,
        org_id: str | None = None,
        user_id: str | None = None,
        visibility: str = "private",
        source_ref: str | None = None,
        source_url: str | None = None,
        structured_payload: dict[str, Any] | None = None,
        authority_principal: str | None = None,
        sensitivity: str = "low",
    ) -> tuple[MemorySource, list[MemorySpan]]:
        source = MemorySource(
            org_id=org_id,
            user_id=user_id,
            visibility=visibility,
            source_kind=source_kind,
            source_ref=source_ref,
            source_url=source_url,
            content_digest=stable_digest(raw_content),
            raw_content=raw_content,
            structured_payload=dict(structured_payload or {}),
            authority_principal=authority_principal,
            sensitivity=sensitivity,
        )
        self._session.add(source)
        await self._session.flush()

        span_drafts = list(spans or [SourceSpanDraft(text=raw_content)])
        rows: list[MemorySpan] = []
        for index, draft in enumerate(span_drafts):
            locator = dict(draft.locator or {"index": index})
            row = MemorySpan(
                source_id=source.id,
                span_kind=draft.span_kind,
                locator=locator,
                text=draft.text,
                token_count=estimate_tokens(draft.text),
                content_digest=stable_digest(draft.text),
            )
            self._session.add(row)
            rows.append(row)
        await self._session.flush()
        return source, rows


class MemoryNodeEmbeddingRepository(BaseRepository[MemoryNodeEmbedding]):
    model = MemoryNodeEmbedding
    content_digest = staticmethod(stable_digest)

    async def exists(
        self,
        *,
        node_id: int,
        embedding_kind: str,
        model: str,
        content_digest: str,
    ) -> bool:
        stmt = select(MemoryNodeEmbedding.id).where(
            MemoryNodeEmbedding.node_id == node_id,
            MemoryNodeEmbedding.embedding_kind == embedding_kind,
            MemoryNodeEmbedding.model == model,
            MemoryNodeEmbedding.content_digest == content_digest,
        )
        return (await self._session.scalars(stmt)).first() is not None

    async def create(
        self,
        *,
        node_id: int,
        embedding_kind: str,
        model: str,
        dimension: int,
        embedding: list[float],
        content_digest: str,
    ) -> MemoryNodeEmbedding:
        row = MemoryNodeEmbedding(
            node_id=node_id,
            embedding_kind=embedding_kind,
            model=model,
            dimension=dimension,
            embedding=embedding,
            content_digest=content_digest,
        )
        self._session.add(row)
        await self._session.flush()
        return row


class MemoryNodeRepository(BaseRepository[MemoryNode]):
    model = MemoryNode

    async def upsert_node(
        self,
        *,
        draft: NodeDraft,
        org_id: str | None = None,
        user_id: str | None = None,
        visibility: str = "private",
    ) -> MemoryNode:
        normalized = draft.normalized_key or normalize_key(draft.canonical_label)
        stmt = select(MemoryNode).where(
            MemoryNode.org_id.is_(None) if org_id is None else MemoryNode.org_id == org_id,
            MemoryNode.node_kind == draft.node_kind,
            MemoryNode.scope_key == draft.scope_key,
            MemoryNode.normalized_key == normalized,
        )
        existing = (await self._session.scalars(stmt)).first()
        if existing is not None:
            if draft.text and not existing.text:
                existing.text = draft.text
            existing.confidence = max(float(existing.confidence or 0), draft.confidence)
            return existing

        node = MemoryNode(
            node_kind=draft.node_kind,
            content_kind=draft.content_kind,
            canonical_label=draft.canonical_label,
            text=draft.text,
            normalized_key=normalized,
            scope_key=draft.scope_key,
            org_id=org_id,
            user_id=user_id,
            visibility=visibility,
            sensitivity=draft.sensitivity,
            confidence=draft.confidence,
            truth_status=draft.truth_status,
            freshness_status=draft.freshness_status,
        )
        self._session.add(node)
        await self._session.flush()
        return node

    async def search_content_nodes(
        self,
        *,
        query: str,
        org_id: str | None = None,
        user_id: str | None = None,
        limit: int = 5,
        allow_global: bool = False,
        query_embedding: Sequence[float] | np.ndarray | None = None,
        embedding_model: str | None = None,
    ) -> list[MemoryNode]:
        query = query.strip()
        if not query:
            return []
        if allow_global:
            visibility_predicate = true()
        elif not user_id and not org_id:
            return []
        else:
            visibility_predicate = or_(
                and_(MemoryNode.visibility == "org", MemoryNode.org_id == org_id),
                and_(MemoryNode.visibility == "team", MemoryNode.org_id == org_id),
                and_(MemoryNode.visibility == "private", MemoryNode.user_id == user_id),
            )

        base_stmt = (
            select(MemoryNode)
            .where(MemoryNode.archived_at.is_(None))
            .where(MemoryNode.node_kind.in_(_CONTENT_NODE_KINDS))
            .where(visibility_predicate)
        )
        if org_id is not None:
            base_stmt = base_stmt.where(or_(MemoryNode.org_id == org_id, MemoryNode.org_id.is_(None)))

        query_vector = (
            np.asarray(query_embedding, dtype=np.float32).reshape(-1)
            if query_embedding is not None
            else None
        )

        rows: list[tuple[MemoryNode, float | None]]
        if query_vector is None or not embedding_model:
            rows = [(node, None) for node in (await self._session.scalars(base_stmt)).all()]
        elif self._session.get_bind().dialect.name == "postgresql":
            similarity = 1.0 - MemoryNodeEmbedding.embedding.cosine_distance(query_vector.tolist())
            semantic_scores = (
                select(
                    MemoryNodeEmbedding.node_id.label("node_id"),
                    func.max(similarity).label("semantic_score"),
                )
                .where(MemoryNodeEmbedding.embedding.isnot(None))
                .where(MemoryNodeEmbedding.model == embedding_model)
                .where(MemoryNodeEmbedding.dimension == int(query_vector.shape[0]))
                .group_by(MemoryNodeEmbedding.node_id)
                .subquery()
            )
            ranked_stmt = (
                base_stmt.add_columns(semantic_scores.c.semantic_score)
                .outerjoin(semantic_scores, semantic_scores.c.node_id == MemoryNode.id)
            )
            rows = [
                (node, float(semantic_score) if semantic_score is not None else None)
                for node, semantic_score in (await self._session.execute(ranked_stmt)).all()
            ]
        else:
            nodes = list((await self._session.scalars(base_stmt)).all())
            semantic_by_node = await self._python_semantic_scores(
                nodes=nodes,
                query_vector=query_vector,
                model=embedding_model,
            )
            rows = [(node, semantic_by_node.get(node.id)) for node in nodes]

        return _rank_memory_nodes(query=query, rows=rows, limit=limit)

    async def _python_semantic_scores(
        self,
        *,
        nodes: Sequence[MemoryNode],
        query_vector: np.ndarray,
        model: str,
    ) -> dict[int, float]:
        """Portable cosine fallback used by SQLite unit tests."""

        if not nodes:
            return {}
        stmt = select(MemoryNodeEmbedding).where(
            MemoryNodeEmbedding.node_id.in_([node.id for node in nodes]),
            MemoryNodeEmbedding.embedding.isnot(None),
            MemoryNodeEmbedding.model == model,
            MemoryNodeEmbedding.dimension == int(query_vector.shape[0]),
        )
        scores: dict[int, float] = {}
        for row in (await self._session.scalars(stmt)).all():
            vector = _coerce_embedding_vector(row.embedding)
            similarity = _cosine_similarity(query_vector, vector)
            if similarity is not None:
                scores[row.node_id] = max(scores.get(row.node_id, -1.0), similarity)
        return scores


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


def _lexical_relevance(node: MemoryNode, *, query: str, terms: Sequence[str]) -> float:
    fields = (
        (node.canonical_label or "").casefold(),
        (node.text or "").casefold(),
        (node.normalized_key or "").casefold(),
    )
    normalized_query = " ".join(query.casefold().split())
    exact = any(field == normalized_query for field in fields if field)
    if not terms:
        return 1.0 if exact else 0.0
    hits = sum(1 for term in terms if any(term in field for field in fields))
    return max(1.0 if exact else 0.0, hits / len(terms))


def _blended_relevance_score(
    *,
    semantic_score: float | None,
    lexical_score: float,
    storage_confidence: float,
) -> float:
    storage_confidence = min(1.0, max(0.0, storage_confidence))
    lexical_score = min(1.0, max(0.0, lexical_score))
    if semantic_score is None:
        # Missing vectors remain fully retrievable by text while backfill runs.
        return 0.95 * lexical_score + 0.05 * storage_confidence
    semantic_score = min(1.0, max(0.0, semantic_score))
    # Similarity and query-term coverage own 97% of ranking. Storage confidence
    # is only a stable tie-break signal and cannot swamp relevance.
    return 0.72 * semantic_score + 0.25 * lexical_score + 0.03 * storage_confidence


def _rank_memory_nodes(
    *,
    query: str,
    rows: Sequence[tuple[MemoryNode, float | None]],
    limit: int,
) -> list[MemoryNode]:
    terms = _query_terms(query)
    ranked: list[MemoryNode] = []
    for node, raw_semantic_score in rows:
        semantic_score = (
            min(1.0, max(0.0, float(raw_semantic_score)))
            if raw_semantic_score is not None
            else None
        )
        lexical_score = _lexical_relevance(node, query=query, terms=terms)
        if lexical_score <= 0.0 and (
            semantic_score is None or semantic_score < _MIN_SEMANTIC_CANDIDATE_SCORE
        ):
            continue
        match_score = _blended_relevance_score(
            semantic_score=semantic_score,
            lexical_score=lexical_score,
            storage_confidence=float(node.confidence or 0.0),
        )
        # These are query-local, non-persisted annotations consumed by the
        # reconstruction controller and compatibility adapters.
        node.retrieval_score = round(match_score, 4)
        node.semantic_score = round(semantic_score, 4) if semantic_score is not None else None
        node.lexical_score = round(lexical_score, 4)
        ranked.append(node)

    ranked.sort(
        key=lambda node: (
            -float(node.retrieval_score),
            -float(node.semantic_score if node.semantic_score is not None else -1.0),
            -float(node.lexical_score),
            -(node.updated_at.timestamp() if node.updated_at is not None else 0.0),
            node.id,
        )
    )
    return ranked[: max(0, limit)]


def _coerce_embedding_vector(value: Any) -> np.ndarray:
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


class MemoryEdgeRepository(BaseRepository[MemoryEdgeNode]):
    model = MemoryEdgeNode

    async def upsert_edge(
        self,
        *,
        draft: EdgeDraft,
        org_id: str | None = None,
        visibility: str = "private",
    ) -> MemoryEdgeNode:
        stmt = select(MemoryEdgeNode).where(
            MemoryEdgeNode.source_node_id == draft.source_node_id,
            MemoryEdgeNode.target_node_id == draft.target_node_id,
            MemoryEdgeNode.edge_kind == draft.edge_kind,
        )
        existing = (await self._session.scalars(stmt)).first()
        if existing is not None:
            existing.weight = max(float(existing.weight or 0), draft.weight)
            existing.confidence = max(float(existing.confidence or 0), draft.confidence)
            existing.evidence_span_ids = sorted(set(existing.evidence_span_ids or []) | set(draft.evidence_span_ids))
            return existing

        edge = MemoryEdgeNode(
            source_node_id=draft.source_node_id,
            target_node_id=draft.target_node_id,
            edge_kind=draft.edge_kind,
            weight=draft.weight,
            confidence=draft.confidence,
            org_id=org_id,
            visibility=visibility,
            evidence_span_ids=list(draft.evidence_span_ids),
            created_by=draft.created_by,
        )
        self._session.add(edge)
        await self._session.flush()
        return edge


class MemoryAssertionRepository(BaseRepository[MemoryAssertionNode]):
    model = MemoryAssertionNode

    async def create_assertion(self, *, draft: AssertionDraft) -> MemoryAssertionNode:
        row = MemoryAssertionNode(
            node_id=draft.node_id,
            claim_text=draft.claim_text,
            subject_node_id=draft.subject_node_id,
            predicate=draft.predicate,
            object_node_id=draft.object_node_id,
            object_text=draft.object_text,
            confidence=draft.confidence,
            truth_status=draft.truth_status,
            review_status=draft.review_status,
            source_span_ids=list(draft.source_span_ids),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_for_nodes(self, node_ids: Sequence[int]) -> list[MemoryAssertionNode]:
        if not node_ids:
            return []
        stmt = select(MemoryAssertionNode).where(MemoryAssertionNode.node_id.in_(list(node_ids)))
        return list((await self._session.scalars(stmt)).all())


class ReconstructionRepository(BaseRepository[ReconstructionRun]):
    model = ReconstructionRun

    async def start_run(
        self,
        *,
        query_text: str,
        query_kind: str = "fact_lookup",
        org_id: str | None = None,
        user_id: str | None = None,
        run_id: int | None = None,
        thread_id: str | None = None,
        budget_tokens: int = 0,
        budget_steps: int = 0,
        policy_version: str = "deterministic-v1",
    ) -> ReconstructionRun:
        row = ReconstructionRun(
            run_id=run_id,
            thread_id=thread_id,
            query_text=query_text,
            query_kind=query_kind,
            org_id=org_id,
            user_id=user_id,
            visibility_context={"org_id": org_id, "user_id": user_id},
            budget_tokens=budget_tokens,
            budget_steps=budget_steps,
            policy_version=policy_version,
            status="running",
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def add_step(
        self,
        *,
        reconstruction_run_id: int,
        step_index: int,
        action_kind: str,
        action_input: dict[str, Any] | None = None,
        action_output: dict[str, Any] | None = None,
        selected_node_ids: Sequence[int] = (),
        rejected_node_ids: Sequence[int] = (),
        reason: str | None = None,
        state_summary: str | None = None,
    ) -> ReconstructionStep:
        row = ReconstructionStep(
            reconstruction_run_id=reconstruction_run_id,
            step_index=step_index,
            action_kind=action_kind,
            action_input=dict(action_input or {}),
            action_output=dict(action_output or {}),
            selected_node_ids=list(selected_node_ids),
            rejected_node_ids=list(rejected_node_ids),
            reason=reason,
            state_summary=state_summary,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def add_evidence(
        self,
        *,
        reconstruction_run_id: int,
        node_id: int | None,
        assertion_id: int | None,
        source_span_id: int | None,
        role: str = "supports_answer",
        confidence: float = 0.5,
        rank: int = 0,
    ) -> ReconstructionEvidence:
        row = ReconstructionEvidence(
            reconstruction_run_id=reconstruction_run_id,
            node_id=node_id,
            assertion_id=assertion_id,
            source_span_id=source_span_id,
            role=role,
            confidence=confidence,
            rank=rank,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def complete_run(self, reconstruction_run_id: int, *, confidence: float) -> None:
        row = await self._session.get(ReconstructionRun, reconstruction_run_id)
        if row is None:
            raise LookupError(f"ReconstructionRun {reconstruction_run_id} not found")
        row.status = "completed"
        row.final_confidence = confidence
        row.completed_at = datetime.now(timezone.utc)
        await self._session.flush()


def _context_user_id(context: Any) -> str | None:
    return str(getattr(context, "user_id", "") or "") or None


def _context_org_id(context: Any) -> str | None:
    return str(getattr(context, "org_id", "") or "") or None


def _context_allow_global(context: Any) -> bool:
    return bool(getattr(context, "allow_global", False))


def _context_visibility(context: Any, default: str = "private") -> str:
    return str(getattr(context, "visibility", None) or default)


def _source_ref(context: Any) -> str | None:
    value = getattr(context, "source_ref", None)
    if callable(value):
        return value()
    return value


def _source_session(context: Any) -> str | None:
    value = getattr(context, "source_session", None)
    if callable(value):
        return value()
    return value


def _visible_node_predicate(context: Any):
    if context is None or _context_allow_global(context):
        return true()
    user_id = _context_user_id(context)
    org_id = _context_org_id(context)
    if not user_id and not org_id:
        return false_predicate()
    return or_(
        and_(MemoryNode.visibility == "org", MemoryNode.org_id == org_id),
        and_(MemoryNode.visibility == "team", MemoryNode.org_id == org_id),
        and_(MemoryNode.visibility == "private", MemoryNode.user_id == user_id),
    )


def false_predicate():
    return MemoryNode.id < 0


def _node_content(node: MemoryNode) -> str:
    return node.text or node.canonical_label


def _node_type(node: MemoryNode) -> str:
    return node.content_kind or node.node_kind


def _node_memory_payload(node: MemoryNode) -> dict[str, Any]:
    confidence = float(node.confidence or 0.0)
    now = datetime.now(timezone.utc)
    archived = node.archived_at is not None
    return {
        "id": node.id,
        "content": _node_content(node),
        "memory_type": _node_type(node),
        "memory_tier": _node_type(node),
        "consolidated": node.node_kind == "summary",
        "archived": archived,
        "superseded_by": None,
        "salience": round(confidence * 10, 2),
        "source": "reconstructive_memory",
        "source_type": "reconstructive_memory_node",
        "source_ref": f"memory_node:{node.id}",
        "truth_status": node.truth_status,
        "review_status": "unreviewed",
        "confidence": confidence,
        "freshness_score": 1.0 if node.freshness_status == "fresh" else 0.5,
        "valid_from": node.valid_from,
        "valid_until": node.valid_until,
        "policy_kind": node.content_kind if node.content_kind == "policy" else None,
        "policy_scope": node.scope_key,
        "reviewed_at": None,
        "reviewed_by": None,
        "demoted_at": node.archived_at if archived else None,
        "demotion_reason": "archived" if archived else None,
        "open_contradiction_count": 0,
        "resolved_contradiction_count": 0,
        "contradiction_status": "none",
        "has_open_contradiction": False,
        "is_reviewed_active": node.truth_status == "active",
        "is_policy_effective": node.content_kind == "policy" and node.truth_status in {"active", "reviewed"},
        "tags": [node.node_kind, *([node.content_kind] if node.content_kind else [])],
        "access_count": 0,
        "last_accessed": node.updated_at,
        "created_at": node.created_at or now,
        "scope": node.scope_key,
        "visibility": node.visibility,
        "user_id": node.user_id,
        "org_id": node.org_id,
    }


def _node_memory_object(node: MemoryNode) -> SimpleNamespace:
    return SimpleNamespace(**_node_memory_payload(node))


def _edge_payload(edge: MemoryEdgeNode) -> dict[str, Any]:
    return {
        "id": edge.id,
        "source_id": edge.source_node_id,
        "target_id": edge.target_node_id,
        "relationship": edge.edge_kind,
        "weight": float(edge.weight or 0.0),
    }


class ReconstructiveMemoryCompatibilityRepository(BaseRepository[MemoryNode]):
    """Backward-compatible memory repository facade backed by reconstructive nodes."""

    model = MemoryNode

    async def insert_memory(
        self,
        *,
        content: str,
        memory_type: str,
        context: Any,
        semantic_embedding: Any | None = None,
        salience: float = 5.0,
        emotion_label: str | None = None,
        emotion_valence: float | None = None,
        emotion_arousal: float | None = None,
        tags: list[str] | None = None,
        related_ids: list[int] | None = None,
        rel_type: str = "related_to",
        decay_eligible: bool = True,
        scope: str = "personal",
        memory_tier: str = "episodic",
        source_memory_ids: list[int] | None = None,
        harvest_type: str | None = None,
        harvest_confidence: float | None = None,
        topic_tags: list[str] | None = None,
        auto_edge: bool = False,
        auto_edge_k: int = 0,
        auto_edge_threshold: float = 0.5,
    ) -> dict:
        del (
            semantic_embedding,
            emotion_label,
            emotion_valence,
            emotion_arousal,
            related_ids,
            rel_type,
            decay_eligible,
            scope,
            memory_tier,
            source_memory_ids,
            harvest_type,
            harvest_confidence,
            topic_tags,
            auto_edge,
            auto_edge_k,
            auto_edge_threshold,
        )
        from brain.systems.reconstructive_memory.ingestion import ingest_memory_source

        confidence = getattr(context, "confidence", None)
        confidence = float(confidence if confidence is not None else max(0.0, min(1.0, salience / 10.0)))
        source = getattr(context, "source", None) or "compatibility_memory_write"
        ingested = await ingest_memory_source(
            self._session,
            content=content,
            content_kind=memory_type,
            source_kind=source,
            source_ref=_source_ref(context) or _source_session(context),
            org_id=_context_org_id(context),
            user_id=_context_user_id(context),
            visibility=_context_visibility(context),
            confidence=confidence,
            evidence={"legacy_tags": list(tags or []), "compatibility_facade": True},
            authority_principal=_context_user_id(context),
        )
        return {
            "id": ingested.content_node_id,
            "type": memory_type,
            "salience": salience,
            "visibility": _context_visibility(context),
            "user_id": _context_user_id(context),
            "org_id": _context_org_id(context),
            "source": source,
            "source_ref": _source_ref(context),
            "auto_edges": 0,
            "manual_edges": 0,
            "neighbors": [],
            "truth_maintenance": {"memory_system": "reconstructive"},
        }

    async def get(self, memory_id: int) -> dict[str, Any] | None:
        node = await self._session.get(MemoryNode, memory_id)
        if node is None or node.archived_at is not None:
            return None
        return _node_memory_payload(node)

    async def get_or_raise_visible(self, memory_id: int, context: Any) -> SimpleNamespace:
        node = await self._get_visible_node(memory_id, context)
        if node is None:
            raise LookupError(f"Memory {memory_id} not found")
        return _node_memory_object(node)

    async def search_visible(self, query: str, context: Any) -> list[dict[str, Any]]:
        nodes = await MemoryNodeRepository(self._session).search_content_nodes(
            query=query,
            org_id=_context_org_id(context),
            user_id=_context_user_id(context),
            limit=50,
            allow_global=_context_allow_global(context),
        )
        return [_node_memory_payload(node) for node in nodes]

    async def list_stale_visible(self, context: Any) -> list[dict[str, Any]]:
        stmt = (
            select(MemoryNode)
            .where(MemoryNode.archived_at.is_(None))
            .where(MemoryNode.node_kind.in_(["content", "summary", "procedure", "policy"]))
            .where(_visible_node_predicate(context))
            .where(MemoryNode.freshness_status.in_(["stale", "expired", "unknown"]))
            .order_by(MemoryNode.updated_at.asc())
            .limit(100)
        )
        return [_node_memory_payload(node) for node in (await self._session.scalars(stmt)).all()]

    async def list_org_memories(self, context: Any, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        org_id = _context_org_id(context)
        if not org_id:
            return []
        stmt = (
            select(MemoryNode)
            .where(MemoryNode.archived_at.is_(None))
            .where(MemoryNode.org_id == org_id)
            .where(MemoryNode.visibility.in_(["org", "team"]))
            .where(MemoryNode.node_kind.in_(["content", "summary", "procedure", "policy"]))
            .order_by(MemoryNode.confidence.desc(), MemoryNode.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return [_node_memory_payload(node) for node in (await self._session.scalars(stmt)).all()]

    async def get_graph_data(self, limit: int = 200, context: Any | None = None) -> dict[str, Any]:
        stmt = (
            select(MemoryNode)
            .where(MemoryNode.archived_at.is_(None))
            .where(MemoryNode.node_kind.in_(["content", "summary", "procedure", "policy", "tag", "cue"]))
            .where(_visible_node_predicate(context))
            .order_by(MemoryNode.confidence.desc(), MemoryNode.updated_at.desc())
            .limit(limit)
        )
        nodes = list((await self._session.scalars(stmt)).all())
        node_ids = [node.id for node in nodes]
        edges: list[MemoryEdgeNode] = []
        if node_ids:
            edge_stmt = (
                select(MemoryEdgeNode)
                .where(MemoryEdgeNode.source_node_id.in_(node_ids))
                .where(MemoryEdgeNode.target_node_id.in_(node_ids))
                .order_by(MemoryEdgeNode.weight.desc())
            )
            edges = list((await self._session.scalars(edge_stmt)).all())
        return {
            "nodes": [_node_memory_payload(node) for node in nodes],
            "edges": [_edge_payload(edge) for edge in edges],
        }

    async def get_similarity_edges(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        del args, kwargs
        return []

    async def get_truth_snapshot(
        self,
        memory_id: int,
        *,
        include_records: bool = False,
        context: Any | None = None,
    ) -> dict[str, Any]:
        del include_records
        node = await self._get_visible_node(memory_id, context)
        if node is None:
            raise LookupError(f"Memory {memory_id} not found")
        memory = _node_memory_payload(node)
        return {
            "memory": memory,
            "state": memory,
            "contradictions": [],
            "reviews": [],
            "conservative_filter_enabled": True,
        }

    async def query_ranked(self, *, query: str | None = None, limit: int = 5, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if not query:
            return {"results": [], "spread_activation": []}
        nodes = await MemoryNodeRepository(self._session).search_content_nodes(query=query, limit=limit)
        results = [
            {
                **_node_memory_payload(node),
                "memory_type": _node_type(node),
                "combined_score": float(getattr(node, "retrieval_score", 0.0)),
                "semantic_score": getattr(node, "semantic_score", None),
                "lexical_score": float(getattr(node, "lexical_score", 0.0)),
                "recency_score": 0.5,
            }
            for node in nodes
        ]
        return {"results": results, "spread_activation": []}

    async def retrieve_with_pools(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        del args, kwargs
        return []

    async def get_detail(self, memory_id: int) -> dict[str, Any] | None:
        node = await self._session.get(MemoryNode, memory_id)
        if node is None or node.archived_at is not None:
            return None
        return {"memory": _node_memory_object(node), "edges": await self.get_neighborhood(memory_id)}

    async def get_graph_context(self, memory_id: int, depth: int = 2) -> dict[str, Any]:
        del depth
        node = await self._session.get(MemoryNode, memory_id)
        if node is None or node.archived_at is not None:
            return {"center": None, "nodes": [], "edges": []}
        edges = await self.get_neighborhood(memory_id)
        node_ids = {memory_id}
        for edge in edges:
            node_ids.add(edge["source_id"])
            node_ids.add(edge["target_id"])
        rows = list((await self._session.scalars(select(MemoryNode).where(MemoryNode.id.in_(node_ids)))).all())
        return {
            "center": _node_memory_payload(node),
            "nodes": [_node_memory_payload(row) for row in rows],
            "edges": edges,
        }

    async def get_memory_neighborhood(self, memory_id: int, hops: int = 1, context: Any | None = None) -> dict[str, Any]:
        del hops, context
        return await self.get_graph_context(memory_id)

    async def get_neighborhood(self, memory_id: int) -> list[dict[str, Any]]:
        stmt = select(MemoryEdgeNode).where(
            or_(MemoryEdgeNode.source_node_id == memory_id, MemoryEdgeNode.target_node_id == memory_id)
        )
        return [_edge_payload(edge) for edge in (await self._session.scalars(stmt)).all()]

    async def list_filtered(
        self,
        *,
        memory_type: str | None = None,
        limit: int = 20,
        min_salience: float | None = None,
        tags: list[str] | None = None,
    ) -> list[SimpleNamespace]:
        stmt = (
            select(MemoryNode)
            .where(MemoryNode.archived_at.is_(None))
            .where(MemoryNode.node_kind.in_(["content", "summary", "procedure", "policy"]))
        )
        if memory_type:
            stmt = stmt.where(MemoryNode.content_kind == memory_type)
        if min_salience is not None:
            stmt = stmt.where(MemoryNode.confidence >= float(min_salience) / 10.0)
        if tags:
            tag_predicates = [func.lower(MemoryNode.text).contains(tag.lower()) for tag in tags]
            stmt = stmt.where(or_(*tag_predicates))
        stmt = stmt.order_by(MemoryNode.confidence.desc(), MemoryNode.updated_at.desc()).limit(limit)
        return [_node_memory_object(node) for node in (await self._session.scalars(stmt)).all()]

    async def stats(self) -> dict[str, Any]:
        total = await self._session.scalar(select(func.count(MemoryNode.id))) or 0
        archived = await self._session.scalar(select(func.count(MemoryNode.id)).where(MemoryNode.archived_at.isnot(None))) or 0
        edge_total = await self._session.scalar(select(func.count(MemoryEdgeNode.id))) or 0
        by_kind_rows = (
            await self._session.execute(
                select(MemoryNode.content_kind, func.count(MemoryNode.id), func.avg(MemoryNode.confidence))
                .where(MemoryNode.archived_at.is_(None))
                .group_by(MemoryNode.content_kind)
            )
        ).all()
        return {
            "total_memories": total,
            "active_memories": max(0, total - archived),
            "archived_memories": archived,
            "edges": edge_total,
            "by_type": [
                {"memory_type": row[0] or "unknown", "count": row[1], "avg_salience": round(float(row[2] or 0.0) * 10, 2)}
                for row in by_kind_rows
            ],
            "memory_system": "reconstructive",
        }

    async def count_active(self) -> int:
        return int(
            await self._session.scalar(
                select(func.count(MemoryNode.id)).where(MemoryNode.archived_at.is_(None))
            )
            or 0
        )

    async def a_count_active(self) -> int:
        return await self.count_active()

    async def count_archived(self) -> int:
        return int(
            await self._session.scalar(
                select(func.count(MemoryNode.id)).where(MemoryNode.archived_at.isnot(None))
            )
            or 0
        )

    async def a_count_archived(self) -> int:
        return await self.count_archived()

    async def count_by_type(self) -> list[dict[str, Any]]:
        rows = (
            await self._session.execute(
                select(MemoryNode.content_kind, func.count(MemoryNode.id))
                .where(MemoryNode.archived_at.is_(None))
                .group_by(MemoryNode.content_kind)
                .order_by(func.count(MemoryNode.id).desc())
            )
        ).all()
        return [{"memory_type": row[0] or "unknown", "count": row[1]} for row in rows]

    async def a_count_by_type(self) -> list[dict[str, Any]]:
        return await self.count_by_type()

    async def recent_activity(self, *, limit: int = 10) -> list[dict[str, Any]]:
        stmt = (
            select(MemoryNode)
            .where(MemoryNode.archived_at.is_(None))
            .where(MemoryNode.node_kind.in_(["content", "summary", "procedure", "policy"]))
            .order_by(MemoryNode.updated_at.desc())
            .limit(limit)
        )
        return [
            {
                "id": node.id,
                "content": _node_content(node),
                "memory_type": _node_type(node),
                "created_at": node.created_at,
                "updated_at": node.updated_at,
            }
            for node in (await self._session.scalars(stmt)).all()
        ]

    async def a_recent_activity(self, *, limit: int = 10) -> list[dict[str, Any]]:
        return await self.recent_activity(limit=limit)

    async def retrieval_accuracy(self) -> dict[str, Any]:
        completed = await self._session.scalar(
            select(func.count(ReconstructionRun.id)).where(ReconstructionRun.status == "completed")
        ) or 0
        avg_confidence = await self._session.scalar(
            select(func.avg(ReconstructionRun.final_confidence)).where(ReconstructionRun.status == "completed")
        )
        return {
            "completed_reconstructions": int(completed),
            "average_confidence": round(float(avg_confidence or 0.0), 4),
            "memory_system": "reconstructive",
        }

    async def a_retrieval_accuracy(self) -> dict[str, Any]:
        return await self.retrieval_accuracy()

    async def list_decay_candidates(self, *, days: int = 30, threshold: float = 2.0) -> list[SimpleNamespace]:
        del days, threshold
        return []

    async def archive_many(self, memory_ids: Sequence[int]) -> None:
        if not memory_ids:
            return
        await self._session.execute(
            update(MemoryNode)
            .where(MemoryNode.id.in_(list(memory_ids)))
            .values(archived_at=datetime.now(timezone.utc))
        )

    async def list_index_memories(self, *, limit: int = 50) -> list[SimpleNamespace]:
        stmt = (
            select(MemoryNode)
            .where(MemoryNode.archived_at.is_(None))
            .where(MemoryNode.node_kind.in_(["content", "summary", "procedure", "policy"]))
            .order_by(MemoryNode.confidence.desc(), MemoryNode.updated_at.desc())
            .limit(limit)
        )
        return [_node_memory_object(node) for node in (await self._session.scalars(stmt)).all()]

    async def high_salience_warnings_for_skill(self, *, skill_embedding: Any | None = None, limit: int = 5) -> list[dict[str, Any]]:
        del skill_embedding
        stmt = (
            select(MemoryNode)
            .where(MemoryNode.archived_at.is_(None))
            .where(MemoryNode.node_kind.in_(["content", "procedure", "policy"]))
            .where(MemoryNode.confidence >= 0.8)
            .order_by(MemoryNode.confidence.desc(), MemoryNode.updated_at.desc())
            .limit(limit)
        )
        return [
            {"id": node.id, "content": _node_content(node), "type": _node_type(node), "salience": round(float(node.confidence or 0.0) * 10, 2)}
            for node in (await self._session.scalars(stmt)).all()
        ]

    async def guardrail_memories_for_task(self, *, task_embedding: Any | None = None, limit: int = 5) -> list[dict[str, Any]]:
        del task_embedding
        return await self.high_salience_warnings_for_skill(limit=limit)

    async def graph_augmented_recall(self, *, query: str | None = None, limit: int = 5, context: Any | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        del kwargs
        if not query:
            return []
        return [
            _node_memory_payload(node)
            for node in await MemoryNodeRepository(self._session).search_content_nodes(
                query=query,
                org_id=_context_org_id(context),
                user_id=_context_user_id(context),
                limit=limit,
                allow_global=_context_allow_global(context),
            )
        ]

    async def _get_visible_node(self, memory_id: int, context: Any | None) -> MemoryNode | None:
        stmt = (
            select(MemoryNode)
            .where(MemoryNode.id == memory_id)
            .where(MemoryNode.archived_at.is_(None))
            .where(_visible_node_predicate(context))
        )
        return (await self._session.scalars(stmt)).first()


class ReconstructiveEdgeCompatibilityRepository(BaseRepository[MemoryEdgeNode]):
    """Backward-compatible edge repository facade backed by reconstructive edges."""

    model = MemoryEdgeNode

    async def upsert_edge(
        self,
        source_id: int,
        target_id: int,
        relationship: str,
        *,
        weight: float = 1.0,
        auto_generated: bool = False,
    ) -> int:
        del auto_generated
        edge = await MemoryEdgeRepository(self._session).upsert_edge(
            draft=EdgeDraft(
                source_node_id=source_id,
                target_node_id=target_id,
                edge_kind=relationship,
                weight=weight,
                confidence=min(1.0, max(0.0, weight)),
                created_by="compatibility_facade",
            )
        )
        return edge.id

    async def neighborhood(self, memory_id: int, context: Any | None = None) -> list[dict[str, Any]]:
        del context
        stmt = select(MemoryEdgeNode).where(
            or_(MemoryEdgeNode.source_node_id == memory_id, MemoryEdgeNode.target_node_id == memory_id)
        )
        return [_edge_payload(edge) for edge in (await self._session.scalars(stmt)).all()]

    async def count_all(self) -> int:
        return int(await self._session.scalar(select(func.count(MemoryEdgeNode.id))) or 0)

    async def a_count_all(self) -> int:
        return await self.count_all()
