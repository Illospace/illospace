"""Ranked-retrieval evaluation for the Illo Knowledge search surface."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.knowledge import KnowledgeItem
from brain.platform.db.models.reconstructive_memory import MemoryNode
from brain.platform.db.repositories.reconstructive_memory import (
    MemoryNodeRepository,
    memory_content_node_filters,
)
from brain.systems.knowledge.recall_eval_contract import (
    KNOWLEDGE_RECALL_ARTIFACT_SCHEMA_VERSION,
    KnowledgeRecallArtifactCaseError,
    KnowledgeRecallArtifactCaseResult,
    KnowledgeRecallArtifactConfiguration,
    KnowledgeRecallArtifactEvidence,
    KnowledgeRecallArtifactQuestionSet,
    KnowledgeRecallArtifactRankedResult,
    KnowledgeRecallArtifactSummary,
    KnowledgeRecallCorpusFingerprint,
    KnowledgeRecallInvalidArtifact,
    KnowledgeRecallInvalidArtifactConfiguration,
    KnowledgeRecallValidArtifact,
)
from brain.systems.knowledge.search import (
    knowledge_item_filters,
    search_knowledge,
)
from brain.systems.knowledge.search_contract import (
    KNOWLEDGE_SEARCH_MAX_RESULTS,
    KnowledgeSearchChannelScore,
    KnowledgeSearchChannelWeights,
    KnowledgeSearchHit,
    KnowledgeSearchResponse,
    KnowledgeSearchScoreChannels,
    KnowledgeSearchScores,
    normalize_knowledge_search_limit,
)

DEFAULT_K_VALUES = (3, 10)
DEFAULT_QUESTION_SET_PATH = (
    Path(__file__).parent / "knowledge_recall_seed_v2.json"
)

KnowledgeSearch = Callable[..., Awaitable[Any]]
KnowledgeRecallEngine = Literal["knowledge", "memory"]


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


async def build_knowledge_recall_corpus_fingerprint(
    session: AsyncSession,
    *,
    org_id: str,
) -> KnowledgeRecallCorpusFingerprint:
    """Summarize the same organization-visible rows used by knowledge search."""

    rows = (
        await session.execute(
            select(
                KnowledgeItem.id,
                KnowledgeItem.source,
                KnowledgeItem.source_ref,
                KnowledgeItem.content_digest,
                KnowledgeItem.search_text,
                KnowledgeItem.source_created_at,
                KnowledgeItem.source_updated_at,
                KnowledgeItem.ingested_at,
            )
            .where(
                *knowledge_item_filters(
                    org_id=org_id,
                    sources=None,
                    kinds=None,
                )
            )
            .order_by(KnowledgeItem.source.asc(), KnowledgeItem.source_ref.asc())
        )
    ).all()
    canonical_rows = [
        {
            "id": int(row.id),
            "source": str(row.source),
            "source_ref": str(row.source_ref),
            "content_digest": str(row.content_digest),
            "search_text": str(row.search_text),
            "source_created_at": _iso_utc(row.source_created_at),
            "source_updated_at": _iso_utc(row.source_updated_at),
        }
        for row in rows
    ]
    encoded_rows = json.dumps(
        canonical_rows,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    source_counts = Counter(str(row.source) for row in rows)
    source_updated_values = [
        row.source_updated_at for row in rows if row.source_updated_at is not None
    ]
    ingested_values = [row.ingested_at for row in rows if row.ingested_at is not None]
    return KnowledgeRecallCorpusFingerprint(
        total_item_count=len(rows),
        source_counts=dict(sorted(source_counts.items())),
        newest_source_updated_at=_iso_utc(
            max(source_updated_values) if source_updated_values else None
        ),
        newest_ingested_at=_iso_utc(
            max(ingested_values) if ingested_values else None
        ),
        fingerprint=hashlib.sha256(encoded_rows).hexdigest(),
    )


async def _visible_memory_content_nodes(
    session: AsyncSession,
    *,
    org_id: str,
) -> list[MemoryNode]:
    return list(
        (
            await session.scalars(
                select(MemoryNode)
                .where(*memory_content_node_filters(org_id=org_id))
                .order_by(MemoryNode.id.asc())
            )
        ).all()
    )


async def build_memory_recall_corpus_fingerprint(
    session: AsyncSession,
    *,
    org_id: str,
) -> KnowledgeRecallCorpusFingerprint:
    """Summarize the visible content nodes used by reconstructive memory."""

    rows = await _visible_memory_content_nodes(session, org_id=org_id)
    canonical_rows = [
        {
            "id": int(row.id),
            "node_kind": str(row.node_kind),
            "content_kind": str(row.content_kind) if row.content_kind else None,
            "canonical_label": str(row.canonical_label),
            "text": str(row.text) if row.text is not None else None,
            "normalized_key": str(row.normalized_key),
            "scope_key": str(row.scope_key),
            "confidence": float(row.confidence),
            "truth_status": str(row.truth_status),
            "freshness_status": str(row.freshness_status),
            "created_at": _iso_utc(row.created_at),
            "updated_at": _iso_utc(row.updated_at),
        }
        for row in rows
    ]
    encoded_rows = json.dumps(
        canonical_rows,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    updated_values = [row.updated_at for row in rows if row.updated_at is not None]
    created_values = [row.created_at for row in rows if row.created_at is not None]
    return KnowledgeRecallCorpusFingerprint(
        total_item_count=len(rows),
        source_counts={"memory": len(rows)} if rows else {},
        newest_source_updated_at=_iso_utc(
            max(updated_values) if updated_values else None
        ),
        newest_ingested_at=_iso_utc(
            max(created_values) if created_values else None
        ),
        fingerprint=hashlib.sha256(encoded_rows).hexdigest(),
    )


async def _recall_corpus(
    session: AsyncSession,
    *,
    org_id: str,
    engine: KnowledgeRecallEngine,
) -> tuple[KnowledgeRecallCorpusFingerprint, set[tuple[str, str]]]:
    if engine == "knowledge":
        fingerprint = await build_knowledge_recall_corpus_fingerprint(
            session,
            org_id=org_id,
        )
        pointers = set(
            (
                await session.execute(
                    select(KnowledgeItem.source, KnowledgeItem.source_ref).where(
                        *knowledge_item_filters(
                            org_id=org_id,
                            sources=None,
                            kinds=None,
                        )
                    )
                )
            ).all()
        )
        return fingerprint, {
            (str(source), str(source_ref)) for source, source_ref in pointers
        }

    rows = await _visible_memory_content_nodes(session, org_id=org_id)
    fingerprint = await build_memory_recall_corpus_fingerprint(
        session,
        org_id=org_id,
    )
    return fingerprint, {("memory", f"memory_node:{row.id}") for row in rows}


@dataclass(frozen=True)
class EvidencePointer:
    """Stable provenance identity for expected or observed knowledge evidence."""

    source: str
    source_ref: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "source_ref": self.source_ref,
        }

    def to_artifact(self) -> KnowledgeRecallArtifactEvidence:
        return KnowledgeRecallArtifactEvidence(
            source=self.source,
            source_ref=self.source_ref,
        )


@dataclass(frozen=True)
class KnowledgeRecallQuestion:
    case_id: str
    question: str
    acceptable_evidence: tuple[EvidencePointer, ...]
    origin: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "question": self.question,
            "acceptable_evidence": [
                pointer.to_dict() for pointer in self.acceptable_evidence
            ],
            "origin": dict(self.origin),
        }


@dataclass(frozen=True)
class KnowledgeRecallQuestionSet:
    question_set_id: str
    version: str
    description: str
    cases: tuple[KnowledgeRecallQuestion, ...]

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_set_id": self.question_set_id,
            "version": self.version,
            "description": self.description,
            "cases": [case.to_dict() for case in self.cases],
        }

    def to_artifact(self) -> KnowledgeRecallArtifactQuestionSet:
        return KnowledgeRecallArtifactQuestionSet(
            id=self.question_set_id,
            version=self.version,
            digest=self.digest,
            case_count=len(self.cases),
        )


@dataclass(frozen=True)
class RankedKnowledgeResult:
    rank: int
    evidence: EvidencePointer
    kind: str
    title: str
    matched: bool
    scores: KnowledgeSearchScores

    def to_artifact(self) -> KnowledgeRecallArtifactRankedResult:
        return KnowledgeRecallArtifactRankedResult(
            rank=self.rank,
            evidence=self.evidence.to_artifact(),
            kind=self.kind,
            title=self.title,
            matched=self.matched,
            scores=self.scores,
        )


@dataclass(frozen=True)
class KnowledgeRecallCaseResult:
    case_id: str
    question: str
    acceptable_evidence: tuple[EvidencePointer, ...]
    origin: dict[str, Any]
    best_evidence_rank: int | None
    reciprocal_rank: float
    hits_at_k: dict[int, bool]
    semantic_available: bool
    semantic_degraded_reason: str | None
    ranked_results: tuple[RankedKnowledgeResult, ...]

    @property
    def missed(self) -> bool:
        return self.best_evidence_rank is None

    def to_artifact(self) -> KnowledgeRecallArtifactCaseResult:
        return KnowledgeRecallArtifactCaseResult(
            case_id=self.case_id,
            question=self.question,
            acceptable_evidence=tuple(
                pointer.to_artifact() for pointer in self.acceptable_evidence
            ),
            origin=dict(self.origin),
            best_evidence_rank=self.best_evidence_rank,
            missed=self.missed,
            reciprocal_rank=self.reciprocal_rank,
            hits_at_k=dict(sorted(self.hits_at_k.items())),
            semantic_available=self.semantic_available,
            semantic_degraded_reason=self.semantic_degraded_reason,
            ranked_results=tuple(
                result.to_artifact() for result in self.ranked_results
            ),
        )


@dataclass(frozen=True)
class KnowledgeRecallCaseError:
    """Cause that prevented one question from participating in the evaluation."""

    case_id: str
    cause: str

    def to_artifact(self) -> KnowledgeRecallArtifactCaseError:
        return KnowledgeRecallArtifactCaseError(
            case_id=self.case_id,
            cause=self.cause,
        )


@dataclass(frozen=True)
class KnowledgeRecallInvalidResult:
    """An unscored run whose case errors make metrics incomparable."""

    result_type: ClassVar[Literal["invalid"]] = "invalid"

    suite: str
    generated_at: str
    live_database: bool
    engine: KnowledgeRecallEngine
    org_id: str
    question_set: KnowledgeRecallQuestionSet
    k_values: tuple[int, ...]
    requested_search_limit: int
    corpus_fingerprint: KnowledgeRecallCorpusFingerprint
    errors: tuple[KnowledgeRecallCaseError, ...]

    def to_artifact(self) -> KnowledgeRecallInvalidArtifact:
        return KnowledgeRecallInvalidArtifact(
            schema_version=KNOWLEDGE_RECALL_ARTIFACT_SCHEMA_VERSION,
            result_type=self.result_type,
            suite=self.suite,
            generated_at=self.generated_at,
            live_database=self.live_database,
            engine=self.engine,
            question_set=self.question_set.to_artifact(),
            configuration=KnowledgeRecallInvalidArtifactConfiguration(
                org_id=self.org_id,
                k_values=self.k_values,
                requested_search_limit=self.requested_search_limit,
            ),
            corpus_fingerprint=self.corpus_fingerprint,
            errors=tuple(error.to_artifact() for error in self.errors),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.to_artifact().model_dump(mode="json")


@dataclass(frozen=True)
class KnowledgeRecallSuiteResult:
    """Valid ranked-retrieval metrics computed over the complete question set."""

    result_type: ClassVar[Literal["valid"]] = "valid"

    suite: str
    generated_at: str
    live_database: bool
    engine: KnowledgeRecallEngine
    org_id: str
    question_set: KnowledgeRecallQuestionSet
    k_values: tuple[int, ...]
    requested_search_limit: int
    effective_search_limit: int
    corpus_fingerprint: KnowledgeRecallCorpusFingerprint
    results: tuple[KnowledgeRecallCaseResult, ...]

    @property
    def semantic_available(self) -> bool:
        return all(result.semantic_available for result in self.results)

    @property
    def semantic_degraded_reasons(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    result.semantic_degraded_reason
                    for result in self.results
                    if result.semantic_degraded_reason
                }
            )
        )

    def to_artifact(self) -> KnowledgeRecallValidArtifact:
        total = len(self.results)
        hits = {
            k: sum(1 for result in self.results if result.hits_at_k[k])
            for k in self.k_values
        }
        recall_at_k = {
            k: _metric(hits[k] / total if total else 0.0)
            for k in self.k_values
        }
        reasons = self.semantic_degraded_reasons
        return KnowledgeRecallValidArtifact(
            schema_version=KNOWLEDGE_RECALL_ARTIFACT_SCHEMA_VERSION,
            result_type=self.result_type,
            suite=self.suite,
            generated_at=self.generated_at,
            live_database=self.live_database,
            engine=self.engine,
            question_set=self.question_set.to_artifact(),
            configuration=KnowledgeRecallArtifactConfiguration(
                org_id=self.org_id,
                k_values=self.k_values,
                requested_search_limit=self.requested_search_limit,
                effective_search_limit=self.effective_search_limit,
            ),
            corpus_fingerprint=self.corpus_fingerprint,
            semantic_available=self.semantic_available,
            semantic_degraded_reason=" | ".join(reasons) if reasons else None,
            summary=KnowledgeRecallArtifactSummary(
                total=total,
                missed=sum(result.missed for result in self.results),
                semantic_degraded_cases=sum(
                    not result.semantic_available for result in self.results
                ),
                hits_at_k=hits,
                recall_at_k=recall_at_k,
                mean_reciprocal_rank=_metric(
                    sum(result.reciprocal_rank for result in self.results) / total
                    if total
                    else 0.0
                ),
                mean_reciprocal_rank_cutoff=self.effective_search_limit,
            ),
            results=tuple(result.to_artifact() for result in self.results),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.to_artifact().model_dump(mode="json")


KnowledgeRecallEvaluationResult = (
    KnowledgeRecallInvalidResult | KnowledgeRecallSuiteResult
)


def _required_text(value: Any, *, field_name: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{field_name} is required")
    return clean


def load_knowledge_recall_question_set(
    path: str | Path = DEFAULT_QUESTION_SET_PATH,
) -> KnowledgeRecallQuestionSet:
    """Load and validate a versioned, data-backed recall question set."""

    question_set_path = Path(path)
    try:
        raw = json.loads(question_set_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid question-set JSON at {question_set_path}: {exc}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ValueError("question set must be a JSON object")

    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("question set must contain at least one case")

    cases: list[KnowledgeRecallQuestion] = []
    seen_case_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"cases[{index}] must be an object")
        case_id = _required_text(
            raw_case.get("case_id"),
            field_name=f"cases[{index}].case_id",
        )
        if case_id in seen_case_ids:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen_case_ids.add(case_id)

        raw_evidence = raw_case.get("acceptable_evidence")
        if not isinstance(raw_evidence, list) or not raw_evidence:
            raise ValueError(
                f"cases[{index}].acceptable_evidence must contain at least one pointer"
            )
        evidence: list[EvidencePointer] = []
        seen_evidence: set[tuple[str, str]] = set()
        for evidence_index, raw_pointer in enumerate(raw_evidence):
            if not isinstance(raw_pointer, Mapping):
                raise ValueError(
                    f"cases[{index}].acceptable_evidence[{evidence_index}] "
                    "must be an object"
                )
            pointer = EvidencePointer(
                source=_required_text(
                    raw_pointer.get("source"),
                    field_name=(
                        f"cases[{index}].acceptable_evidence"
                        f"[{evidence_index}].source"
                    ),
                ),
                source_ref=_required_text(
                    raw_pointer.get("source_ref"),
                    field_name=(
                        f"cases[{index}].acceptable_evidence"
                        f"[{evidence_index}].source_ref"
                    ),
                ),
            )
            key = (pointer.source, pointer.source_ref)
            if key in seen_evidence:
                raise ValueError(f"duplicate evidence pointer in case {case_id}: {key}")
            seen_evidence.add(key)
            evidence.append(pointer)

        origin = raw_case.get("origin") or {}
        if not isinstance(origin, Mapping):
            raise ValueError(f"cases[{index}].origin must be an object")
        cases.append(
            KnowledgeRecallQuestion(
                case_id=case_id,
                question=_required_text(
                    raw_case.get("question"),
                    field_name=f"cases[{index}].question",
                ),
                acceptable_evidence=tuple(evidence),
                origin=dict(origin),
            )
        )

    return KnowledgeRecallQuestionSet(
        question_set_id=_required_text(
            raw.get("question_set_id"),
            field_name="question_set_id",
        ),
        version=_required_text(raw.get("version"), field_name="version"),
        description=str(raw.get("description") or "").strip(),
        cases=tuple(cases),
    )


def normalize_k_values(k_values: Sequence[int]) -> tuple[int, ...]:
    normalized = tuple(sorted({int(value) for value in k_values}))
    if not normalized:
        raise ValueError("at least one k value is required")
    if normalized[0] < 1 or normalized[-1] > KNOWLEDGE_SEARCH_MAX_RESULTS:
        raise ValueError(
            "k values must be between 1 and "
            f"{KNOWLEDGE_SEARCH_MAX_RESULTS}"
        )
    return normalized


def _metric(value: float) -> float:
    return round(float(value), 8)


async def _embed_memory_recall_query(
    session: AsyncSession,
    query: str,
) -> Any:
    from brain.systems.reconstructive_memory.embeddings import embed_recall_query

    return await embed_recall_query(session, query)


async def search_memory_recall(
    session: AsyncSession,
    query: str,
    *,
    org_id: str,
    limit: int,
) -> KnowledgeSearchResponse:
    """Adapt reconstructive memory's own ranker to the recall harness contract."""

    requested_limit = int(limit)
    effective_limit = normalize_knowledge_search_limit(
        requested_limit,
        default=KNOWLEDGE_SEARCH_MAX_RESULTS,
    )
    query_embedding = await _embed_memory_recall_query(session, query)
    nodes = await MemoryNodeRepository(session).search_content_nodes(
        query=query,
        org_id=org_id,
        limit=effective_limit,
        query_embedding=(
            query_embedding.vector if query_embedding is not None else None
        ),
        embedding_model=(
            query_embedding.model if query_embedding is not None else None
        ),
    )
    semantic_available = query_embedding is not None
    lexical_weight = 0.25 if semantic_available else 0.95
    semantic_weight = 0.72 if semantic_available else 0.0
    storage_weight = 0.03 if semantic_available else 0.05
    hits: list[KnowledgeSearchHit] = []
    for rank, node in enumerate(nodes, start=1):
        lexical_score = float(getattr(node, "lexical_score", 0.0))
        semantic_score = getattr(node, "semantic_score", None)
        semantic_value = (
            float(semantic_score) if semantic_score is not None else None
        )
        hits.append(
            KnowledgeSearchHit(
                id=int(node.id),
                source="memory",
                kind=str(node.content_kind or node.node_kind),
                source_ref=f"memory_node:{node.id}",
                title=str(node.canonical_label),
                summary=str(node.text or node.canonical_label),
                resolution=None,
                entities=[],
                extra={
                    "confidence": float(node.confidence or 0.0),
                    "freshness_status": str(node.freshness_status),
                    "node_kind": str(node.node_kind),
                    "scope": str(node.scope_key),
                    "truth_status": str(node.truth_status),
                    "visibility": str(node.visibility),
                },
                source_created_at=_iso_utc(node.created_at),
                source_updated_at=_iso_utc(node.updated_at),
                scores=KnowledgeSearchScores(
                    rrf=float(getattr(node, "retrieval_score", 0.0)),
                    channels=KnowledgeSearchScoreChannels(
                        lexical=KnowledgeSearchChannelScore(
                            rank=rank,
                            score=lexical_score,
                            weight=lexical_weight,
                            contribution=lexical_weight * lexical_score,
                        ),
                        semantic=(
                            KnowledgeSearchChannelScore(
                                rank=rank,
                                score=semantic_value,
                                weight=semantic_weight,
                                contribution=semantic_weight * semantic_value,
                            )
                            if semantic_value is not None
                            else None
                        ),
                        recency=None,
                    ),
                ),
            )
        )
    return KnowledgeSearchResponse(
        query=query,
        org_id=org_id,
        sources=["memory"],
        kinds=[],
        semantic_available=semantic_available,
        semantic_degraded_reason=(
            None if semantic_available else "memory query embedding unavailable"
        ),
        weights=KnowledgeSearchChannelWeights(
            lexical=lexical_weight,
            semantic=semantic_weight,
            recency=storage_weight,
        ),
        requested_limit=requested_limit,
        effective_limit=effective_limit,
        results=hits,
    )


def _ranked_results(
    response: KnowledgeSearchResponse,
    *,
    acceptable_evidence: tuple[EvidencePointer, ...],
) -> tuple[RankedKnowledgeResult, ...]:
    acceptable = {
        (pointer.source, pointer.source_ref) for pointer in acceptable_evidence
    }
    ranked: list[RankedKnowledgeResult] = []
    for rank, result in enumerate(response.results, start=1):
        evidence = EvidencePointer(
            source=result.source,
            source_ref=result.source_ref,
        )
        ranked.append(
            RankedKnowledgeResult(
                rank=rank,
                evidence=evidence,
                kind=result.kind,
                title=result.title,
                matched=(evidence.source, evidence.source_ref) in acceptable,
                scores=result.scores,
            )
        )
    return tuple(ranked)


async def run_knowledge_recall_eval(
    session: AsyncSession,
    *,
    org_id: str,
    question_set: KnowledgeRecallQuestionSet | None = None,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
    search_limit: int = KNOWLEDGE_SEARCH_MAX_RESULTS,
    engine: KnowledgeRecallEngine = "knowledge",
    search: KnowledgeSearch | None = None,
    generated_at: str | None = None,
    live_database: bool = False,
) -> KnowledgeRecallEvaluationResult:
    """Score the complete question set, or return an unscored invalid result.

    A search, contract, or retrieval-depth failure on even one case invalidates
    the whole run. Metrics computed over only the successful subset would not
    be comparable with complete runs, and comparability is this artifact's job.
    """

    clean_org_id = _required_text(org_id, field_name="org_id")
    if engine not in ("knowledge", "memory"):
        raise ValueError("engine must be 'knowledge' or 'memory'")
    selected_search = search or (
        search_knowledge if engine == "knowledge" else search_memory_recall
    )
    loaded_question_set = question_set or load_knowledge_recall_question_set()
    normalized_k = normalize_k_values(k_values)
    requested_search_limit = int(search_limit)
    report_generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    corpus_fingerprint, reachable_pointers = await _recall_corpus(
        session,
        org_id=clean_org_id,
        engine=engine,
    )
    if not loaded_question_set.cases:
        return KnowledgeRecallInvalidResult(
            suite="knowledge-recall",
            generated_at=report_generated_at,
            live_database=live_database,
            engine=engine,
            org_id=clean_org_id,
            question_set=loaded_question_set,
            k_values=normalized_k,
            requested_search_limit=requested_search_limit,
            corpus_fingerprint=corpus_fingerprint,
            errors=(
                KnowledgeRecallCaseError(
                    case_id=loaded_question_set.question_set_id,
                    cause="question set contains no evaluable cases",
                ),
            ),
        )

    normalized_search_limit = normalize_knowledge_search_limit(
        requested_search_limit,
        default=KNOWLEDGE_SEARCH_MAX_RESULTS,
    )
    if (
        requested_search_limit != normalized_search_limit
        or normalized_search_limit < max(normalized_k)
    ):
        cause = (
            "search_limit must be at least the largest k value and between "
            f"1 and {KNOWLEDGE_SEARCH_MAX_RESULTS}"
        )
        return KnowledgeRecallInvalidResult(
            suite="knowledge-recall",
            generated_at=report_generated_at,
            live_database=live_database,
            engine=engine,
            org_id=clean_org_id,
            question_set=loaded_question_set,
            k_values=normalized_k,
            requested_search_limit=requested_search_limit,
            corpus_fingerprint=corpus_fingerprint,
            errors=tuple(
                KnowledgeRecallCaseError(case_id=case.case_id, cause=cause)
                for case in loaded_question_set.cases
            ),
        )

    evidence_pointers = [
        pointer
        for case in loaded_question_set.cases
        for pointer in case.acceptable_evidence
    ]
    reachable_count = sum(
        (pointer.source, pointer.source_ref) in reachable_pointers
        for pointer in evidence_pointers
    )
    if evidence_pointers and reachable_count == 0:
        evidence_source_counts = Counter(
            pointer.source for pointer in evidence_pointers
        )
        source_breakdown = ", ".join(
            f"{count}/{len(evidence_pointers)} `{source}`"
            for source, count in sorted(evidence_source_counts.items())
        )
        return KnowledgeRecallInvalidResult(
            suite="knowledge-recall",
            generated_at=report_generated_at,
            live_database=live_database,
            engine=engine,
            org_id=clean_org_id,
            question_set=loaded_question_set,
            k_values=normalized_k,
            requested_search_limit=requested_search_limit,
            corpus_fingerprint=corpus_fingerprint,
            errors=(
                KnowledgeRecallCaseError(
                    case_id=loaded_question_set.question_set_id,
                    cause=(
                        f"0 of {len(evidence_pointers)} acceptable evidence refs "
                        f"are reachable in the {engine} corpus; the question set "
                        f"evidence is {source_breakdown}"
                    ),
                ),
            ),
        )

    case_results: list[KnowledgeRecallCaseResult] = []
    case_errors: list[KnowledgeRecallCaseError] = []
    observed_depths: list[tuple[str, int]] = []

    for case in loaded_question_set.cases:
        try:
            raw_response = await selected_search(
                session,
                case.question,
                org_id=clean_org_id,
                limit=normalized_search_limit,
            )
            response = KnowledgeSearchResponse.model_validate(raw_response)
            if response.query != case.question:
                raise ValueError(
                    "knowledge_search.query does not match the evaluated question"
                )
            if response.org_id != clean_org_id:
                raise ValueError(
                    "knowledge_search.org_id does not match the evaluated organization"
                )
            if response.requested_limit != normalized_search_limit:
                raise ValueError(
                    "knowledge_search.requested_limit does not match the evaluator request"
                )
            if response.effective_limit < max(normalized_k):
                raise ValueError(
                    "knowledge search effective_limit "
                    f"{response.effective_limit} cannot support recall@"
                    f"{max(normalized_k)}"
                )
            observed_depths.append((case.case_id, response.effective_limit))
            ranked = _ranked_results(
                response,
                acceptable_evidence=case.acceptable_evidence,
            )
            semantic_available = response.semantic_available
            semantic_degraded_reason = response.semantic_degraded_reason
        except Exception as exc:
            case_errors.append(
                KnowledgeRecallCaseError(
                    case_id=case.case_id,
                    cause=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        best_rank = next((result.rank for result in ranked if result.matched), None)
        case_results.append(
            KnowledgeRecallCaseResult(
                case_id=case.case_id,
                question=case.question,
                acceptable_evidence=case.acceptable_evidence,
                origin=case.origin,
                best_evidence_rank=best_rank,
                reciprocal_rank=_metric(1.0 / best_rank if best_rank else 0.0),
                hits_at_k={
                    k: best_rank is not None and best_rank <= k for k in normalized_k
                },
                semantic_available=semantic_available,
                semantic_degraded_reason=semantic_degraded_reason,
                ranked_results=ranked,
            )
        )

    effective_limits = {depth for _case_id, depth in observed_depths}
    if len(effective_limits) > 1:
        observed = ", ".join(str(depth) for depth in sorted(effective_limits))
        case_errors.extend(
            KnowledgeRecallCaseError(
                case_id=case_id,
                cause=(
                    "knowledge search returned inconsistent effective retrieval "
                    f"depth {depth}; observed depths: {observed}"
                ),
            )
            for case_id, depth in observed_depths
        )

    if case_errors:
        return KnowledgeRecallInvalidResult(
            suite="knowledge-recall",
            generated_at=report_generated_at,
            live_database=live_database,
            engine=engine,
            org_id=clean_org_id,
            question_set=loaded_question_set,
            k_values=normalized_k,
            requested_search_limit=requested_search_limit,
            corpus_fingerprint=corpus_fingerprint,
            errors=tuple(case_errors),
        )

    return KnowledgeRecallSuiteResult(
        suite="knowledge-recall",
        generated_at=report_generated_at,
        live_database=live_database,
        engine=engine,
        org_id=clean_org_id,
        question_set=loaded_question_set,
        k_values=normalized_k,
        requested_search_limit=requested_search_limit,
        effective_search_limit=observed_depths[0][1],
        corpus_fingerprint=corpus_fingerprint,
        results=tuple(case_results),
    )


__all__ = [
    "DEFAULT_K_VALUES",
    "DEFAULT_QUESTION_SET_PATH",
    "EvidencePointer",
    "KnowledgeRecallCaseError",
    "KnowledgeRecallCaseResult",
    "KnowledgeRecallCorpusFingerprint",
    "KnowledgeRecallEvaluationResult",
    "KnowledgeRecallInvalidResult",
    "KnowledgeRecallQuestion",
    "KnowledgeRecallQuestionSet",
    "KnowledgeRecallSuiteResult",
    "RankedKnowledgeResult",
    "build_knowledge_recall_corpus_fingerprint",
    "build_memory_recall_corpus_fingerprint",
    "load_knowledge_recall_question_set",
    "normalize_k_values",
    "run_knowledge_recall_eval",
    "search_memory_recall",
]
