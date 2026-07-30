"""Ranked-retrieval evaluation for the Illo Knowledge search surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, ClassVar, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems.knowledge.search import search_knowledge
from brain.systems.knowledge.search_contract import (
    KNOWLEDGE_SEARCH_MAX_RESULTS,
    KnowledgeSearchResponse,
    KnowledgeSearchScores,
    normalize_knowledge_search_limit,
)

DEFAULT_K_VALUES = (3, 10)
DEFAULT_QUESTION_SET_PATH = (
    Path(__file__).parent / "knowledge_recall_seed_v1.json"
)

KnowledgeSearch = Callable[..., Awaitable[Any]]


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
    scores: KnowledgeSearchScores

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "evidence": self.evidence.to_dict(),
            "kind": self.kind,
            "title": self.title,
            "matched": self.matched,
            "scores": self.scores.model_dump(mode="json"),
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
            "ranked_results": [result.to_dict() for result in self.ranked_results],
        }


@dataclass(frozen=True)
class KnowledgeRecallCaseError:
    """Cause that prevented one question from participating in the evaluation."""

    case_id: str
    cause: str

    def to_dict(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "cause": self.cause,
        }


@dataclass(frozen=True)
class KnowledgeRecallInvalidResult:
    """An unscored run whose case errors make metrics incomparable."""

    result_type: ClassVar[Literal["invalid"]] = "invalid"

    suite: str
    generated_at: str
    live_database: bool
    org_id: str
    question_set: KnowledgeRecallQuestionSet
    k_values: tuple[int, ...]
    requested_search_limit: int
    errors: tuple[KnowledgeRecallCaseError, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_type": self.result_type,
            "suite": self.suite,
            "generated_at": self.generated_at,
            "live_database": self.live_database,
            "question_set": self.question_set.identity_dict(),
            "configuration": {
                "org_id": self.org_id,
                "k_values": list(self.k_values),
                "requested_search_limit": self.requested_search_limit,
            },
            "errors": [error.to_dict() for error in self.errors],
        }


@dataclass(frozen=True)
class KnowledgeRecallSuiteResult:
    """Valid ranked-retrieval metrics computed over the complete question set."""

    result_type: ClassVar[Literal["valid"]] = "valid"

    suite: str
    generated_at: str
    live_database: bool
    org_id: str
    question_set: KnowledgeRecallQuestionSet
    k_values: tuple[int, ...]
    requested_search_limit: int
    effective_search_limit: int
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
            "result_type": self.result_type,
            "suite": self.suite,
            "generated_at": self.generated_at,
            "live_database": self.live_database,
            "question_set": self.question_set.identity_dict(),
            "configuration": {
                "org_id": self.org_id,
                "k_values": list(self.k_values),
                "requested_search_limit": self.requested_search_limit,
                "effective_search_limit": self.effective_search_limit,
            },
            "semantic_available": self.semantic_available,
            "semantic_degraded_reason": " | ".join(reasons) if reasons else None,
            "summary": {
                "total": total,
                "missed": sum(result.missed for result in self.results),
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
                "mean_reciprocal_rank_cutoff": self.effective_search_limit,
            },
            "results": [result.to_dict() for result in self.results],
        }


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
    search: KnowledgeSearch = search_knowledge,
    generated_at: str | None = None,
    live_database: bool = False,
) -> KnowledgeRecallEvaluationResult:
    """Score the complete question set, or return an unscored invalid result.

    A search, contract, or retrieval-depth failure on even one case invalidates
    the whole run. Metrics computed over only the successful subset would not
    be comparable with complete runs, and comparability is this artifact's job.
    """

    clean_org_id = _required_text(org_id, field_name="org_id")
    loaded_question_set = question_set or load_knowledge_recall_question_set()
    normalized_k = normalize_k_values(k_values)
    requested_search_limit = int(search_limit)
    report_generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    if not loaded_question_set.cases:
        return KnowledgeRecallInvalidResult(
            suite="knowledge-recall",
            generated_at=report_generated_at,
            live_database=live_database,
            org_id=clean_org_id,
            question_set=loaded_question_set,
            k_values=normalized_k,
            requested_search_limit=requested_search_limit,
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
            org_id=clean_org_id,
            question_set=loaded_question_set,
            k_values=normalized_k,
            requested_search_limit=requested_search_limit,
            errors=tuple(
                KnowledgeRecallCaseError(case_id=case.case_id, cause=cause)
                for case in loaded_question_set.cases
            ),
        )

    case_results: list[KnowledgeRecallCaseResult] = []
    case_errors: list[KnowledgeRecallCaseError] = []
    observed_depths: list[tuple[str, int]] = []

    for case in loaded_question_set.cases:
        try:
            raw_response = await search(
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
            org_id=clean_org_id,
            question_set=loaded_question_set,
            k_values=normalized_k,
            requested_search_limit=requested_search_limit,
            errors=tuple(case_errors),
        )

    return KnowledgeRecallSuiteResult(
        suite="knowledge-recall",
        generated_at=report_generated_at,
        live_database=live_database,
        org_id=clean_org_id,
        question_set=loaded_question_set,
        k_values=normalized_k,
        requested_search_limit=requested_search_limit,
        effective_search_limit=observed_depths[0][1],
        results=tuple(case_results),
    )


__all__ = [
    "DEFAULT_K_VALUES",
    "DEFAULT_QUESTION_SET_PATH",
    "EvidencePointer",
    "KnowledgeRecallCaseError",
    "KnowledgeRecallCaseResult",
    "KnowledgeRecallEvaluationResult",
    "KnowledgeRecallInvalidResult",
    "KnowledgeRecallQuestion",
    "KnowledgeRecallQuestionSet",
    "KnowledgeRecallSuiteResult",
    "RankedKnowledgeResult",
    "load_knowledge_recall_question_set",
    "normalize_k_values",
    "run_knowledge_recall_eval",
]
