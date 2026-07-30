"""Ranked-retrieval evaluation for the Illo Knowledge search surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems.knowledge.search import search_knowledge

DEFAULT_K_VALUES = (3, 10)
DEFAULT_SEARCH_LIMIT = 50
DEFAULT_QUESTION_SET_PATH = (
    Path(__file__).resolve().parent / "data" / "knowledge_recall_seed_v1.json"
)

KnowledgeSearch = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class EvidencePointer:
    """Stable provenance for an acceptable known-best knowledge item."""

    source: str
    source_ref: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "source_ref": self.source_ref,
        }


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

    def identity_dict(self) -> dict[str, Any]:
        return {
            "id": self.question_set_id,
            "version": self.version,
            "digest": self.digest,
            "case_count": len(self.cases),
        }


@dataclass(frozen=True)
class RankedKnowledgeResult:
    rank: int
    evidence: EvidencePointer
    kind: str
    title: str
    matched: bool
    scores: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "evidence": self.evidence.to_dict(),
            "kind": self.kind,
            "title": self.title,
            "matched": self.matched,
            "scores": dict(self.scores),
        }


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
    error: str | None = None

    @property
    def missed(self) -> bool:
        return self.best_evidence_rank is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "question": self.question,
            "acceptable_evidence": [
                pointer.to_dict() for pointer in self.acceptable_evidence
            ],
            "origin": dict(self.origin),
            "best_evidence_rank": self.best_evidence_rank,
            "missed": self.missed,
            "reciprocal_rank": self.reciprocal_rank,
            "hits_at_k": {
                str(k): hit for k, hit in sorted(self.hits_at_k.items())
            },
            "semantic_available": self.semantic_available,
            "semantic_degraded_reason": self.semantic_degraded_reason,
            "error": self.error,
            "ranked_results": [result.to_dict() for result in self.ranked_results],
        }


@dataclass(frozen=True)
class KnowledgeRecallSuiteResult:
    """Eval report envelope extended for ranked retrieval rather than pass/fail."""

    suite: str
    generated_at: str
    live_database: bool
    org_id: str
    question_set: KnowledgeRecallQuestionSet
    k_values: tuple[int, ...]
    search_limit: int
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

    def to_dict(self) -> dict[str, Any]:
        total = len(self.results)
        hits = {
            str(k): sum(1 for result in self.results if result.hits_at_k[k])
            for k in self.k_values
        }
        recall_at_k = {
            str(k): _metric(hits[str(k)] / total if total else 0.0)
            for k in self.k_values
        }
        reasons = self.semantic_degraded_reasons
        return {
            "suite": self.suite,
            "generated_at": self.generated_at,
            "live_database": self.live_database,
            "question_set": self.question_set.identity_dict(),
            "configuration": {
                "org_id": self.org_id,
                "k_values": list(self.k_values),
                "search_limit": self.search_limit,
            },
            "semantic_available": self.semantic_available,
            "semantic_degraded_reason": " | ".join(reasons) if reasons else None,
            "summary": {
                "total": total,
                "missed": sum(result.missed for result in self.results),
                "search_errors": sum(result.error is not None for result in self.results),
                "semantic_degraded_cases": sum(
                    not result.semantic_available for result in self.results
                ),
                "hits_at_k": hits,
                "recall_at_k": recall_at_k,
                "mean_reciprocal_rank": _metric(
                    sum(result.reciprocal_rank for result in self.results) / total
                    if total
                    else 0.0
                ),
                "mean_reciprocal_rank_cutoff": self.search_limit,
            },
            "results": [result.to_dict() for result in self.results],
        }


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
    if normalized[0] < 1 or normalized[-1] > 50:
        raise ValueError("k values must be between 1 and 50")
    return normalized


def _metric(value: float) -> float:
    return round(float(value), 8)


def _scores(raw_scores: Any) -> dict[str, Any]:
    scores = raw_scores if isinstance(raw_scores, Mapping) else {}
    raw_channels = scores.get("channels")
    channels = raw_channels if isinstance(raw_channels, Mapping) else {}
    normalized_channels: dict[str, Any] = {}
    for channel_name in ("lexical", "semantic", "recency"):
        raw_channel = channels.get(channel_name)
        if not isinstance(raw_channel, Mapping):
            normalized_channels[channel_name] = None
            continue
        channel = dict(raw_channel)
        channel.setdefault("rank", None)
        # Recency currently has no independent raw score. Keep that absence
        # explicit while preserving its weighted RRF contribution.
        channel.setdefault("score", None)
        channel.setdefault("weight", None)
        channel.setdefault("contribution", None)
        normalized_channels[channel_name] = channel
    return {
        "rrf": scores.get("rrf"),
        "channels": normalized_channels,
    }


def _ranked_results(
    raw_results: Any,
    *,
    acceptable_evidence: tuple[EvidencePointer, ...],
) -> tuple[RankedKnowledgeResult, ...]:
    rows = raw_results if isinstance(raw_results, list) else []
    acceptable = {
        (pointer.source, pointer.source_ref) for pointer in acceptable_evidence
    }
    ranked: list[RankedKnowledgeResult] = []
    for rank, raw_result in enumerate(rows, start=1):
        if not isinstance(raw_result, Mapping):
            continue
        evidence = EvidencePointer(
            source=str(raw_result.get("source") or ""),
            source_ref=str(raw_result.get("source_ref") or ""),
        )
        ranked.append(
            RankedKnowledgeResult(
                rank=rank,
                evidence=evidence,
                kind=str(raw_result.get("kind") or ""),
                title=str(raw_result.get("title") or ""),
                matched=(evidence.source, evidence.source_ref) in acceptable,
                scores=_scores(raw_result.get("scores")),
            )
        )
    return tuple(ranked)


async def run_knowledge_recall_eval(
    session: AsyncSession,
    *,
    org_id: str,
    question_set: KnowledgeRecallQuestionSet | None = None,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    search: KnowledgeSearch = search_knowledge,
    generated_at: str | None = None,
    live_database: bool = False,
) -> KnowledgeRecallSuiteResult:
    """Score known-best provenance with recall@k and mean reciprocal rank."""

    clean_org_id = _required_text(org_id, field_name="org_id")
    loaded_question_set = question_set or load_knowledge_recall_question_set()
    normalized_k = normalize_k_values(k_values)
    normalized_search_limit = int(search_limit)
    if normalized_search_limit < max(normalized_k) or normalized_search_limit > 50:
        raise ValueError(
            "search_limit must be at least the largest k value and no greater than 50"
        )
    case_results: list[KnowledgeRecallCaseResult] = []

    for case in loaded_question_set.cases:
        error = None
        try:
            response = await search(
                session,
                case.question,
                org_id=clean_org_id,
                limit=normalized_search_limit,
            )
            if not isinstance(response, Mapping):
                raise TypeError("knowledge search returned a non-object payload")
            ranked = _ranked_results(
                response.get("results"),
                acceptable_evidence=case.acceptable_evidence,
            )
            semantic_available = response.get("semantic_available") is True
            raw_reason = response.get("semantic_degraded_reason")
            semantic_degraded_reason = (
                str(raw_reason) if raw_reason is not None else None
            )
            if not semantic_available and not semantic_degraded_reason:
                semantic_degraded_reason = (
                    "semantic channel unavailable without a reported reason"
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            ranked = ()
            semantic_available = False
            semantic_degraded_reason = f"search failed: {error}"

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
                error=error,
            )
        )

    return KnowledgeRecallSuiteResult(
        suite="knowledge-recall",
        generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
        live_database=live_database,
        org_id=clean_org_id,
        question_set=loaded_question_set,
        k_values=normalized_k,
        search_limit=normalized_search_limit,
        results=tuple(case_results),
    )


__all__ = [
    "DEFAULT_K_VALUES",
    "DEFAULT_QUESTION_SET_PATH",
    "DEFAULT_SEARCH_LIMIT",
    "EvidencePointer",
    "KnowledgeRecallCaseResult",
    "KnowledgeRecallQuestion",
    "KnowledgeRecallQuestionSet",
    "KnowledgeRecallSuiteResult",
    "RankedKnowledgeResult",
    "load_knowledge_recall_question_set",
    "normalize_k_values",
    "run_knowledge_recall_eval",
]
