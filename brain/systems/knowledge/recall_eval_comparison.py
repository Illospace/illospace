"""Offline comparison of serialized knowledge-recall evaluation artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from brain.systems.knowledge.recall_eval import KnowledgeRecallCorpusFingerprint


@dataclass(frozen=True)
class _ValidArtifact:
    question_set_digest: str
    org_id: str
    k_values: tuple[int, ...]
    effective_search_limit: int
    corpus_fingerprint: KnowledgeRecallCorpusFingerprint | None
    summary: Mapping[str, Any]
    results: tuple[Mapping[str, Any], ...]


def _mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return value


def _required_text(value: Any, *, field_name: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{field_name} is required")
    return clean


def _result_type(artifact: Mapping[str, Any], *, label: str) -> str:
    result_type = artifact.get("result_type")
    if result_type not in {"valid", "invalid"}:
        raise ValueError(f"{label}.result_type must be valid or invalid")
    return str(result_type)


def _load_valid_artifact(
    artifact: Mapping[str, Any],
    *,
    label: str,
) -> _ValidArtifact:
    question_set = _mapping(
        artifact.get("question_set"),
        field_name=f"{label}.question_set",
    )
    configuration = _mapping(
        artifact.get("configuration"),
        field_name=f"{label}.configuration",
    )
    raw_k_values = configuration.get("k_values")
    if not isinstance(raw_k_values, list):
        raise ValueError(f"{label}.configuration.k_values must be a list")
    try:
        k_values = tuple(int(value) for value in raw_k_values)
        effective_search_limit = int(configuration["effective_search_limit"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label} search configuration: {exc}") from exc

    raw_fingerprint = artifact.get("corpus_fingerprint")
    corpus_fingerprint = None
    if raw_fingerprint is not None:
        corpus_fingerprint = KnowledgeRecallCorpusFingerprint.from_dict(
            _mapping(
                raw_fingerprint,
                field_name=f"{label}.corpus_fingerprint",
            )
        )

    raw_results = artifact.get("results")
    if not isinstance(raw_results, list):
        raise ValueError(f"{label}.results must be a list")
    return _ValidArtifact(
        question_set_digest=_required_text(
            question_set.get("digest"),
            field_name=f"{label}.question_set.digest",
        ),
        org_id=_required_text(
            configuration.get("org_id"),
            field_name=f"{label}.configuration.org_id",
        ),
        k_values=k_values,
        effective_search_limit=effective_search_limit,
        corpus_fingerprint=corpus_fingerprint,
        summary=_mapping(
            artifact.get("summary"),
            field_name=f"{label}.summary",
        ),
        results=tuple(
            _mapping(result, field_name=f"{label}.results[{index}]")
            for index, result in enumerate(raw_results)
        ),
    )


def _check(baseline: Any, candidate: Any) -> dict[str, Any]:
    return {
        "baseline": baseline,
        "candidate": candidate,
        "matches": baseline == candidate,
    }


def _invalid_comparison(
    *,
    verdict: Literal["invalid-artifact", "not-comparable"],
    reason: str,
    differences: Sequence[str] = (),
    checks: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "comparison_type": "knowledge-recall",
        "comparability": {
            "comparable": False,
            "computed": False,
            "verdict": verdict,
            "reason": reason,
            "differences": list(differences),
            "checks": dict(checks or {}),
            "corpus_changed_fields": {},
        },
    }


def _delta(baseline: float, candidate: float) -> dict[str, float]:
    return {
        "baseline": baseline,
        "candidate": candidate,
        "delta": round(candidate - baseline, 8),
    }


def _case_rank_deltas(
    baseline_results: Sequence[Mapping[str, Any]],
    candidate_results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidate_by_case = {
        _required_text(result.get("case_id"), field_name="candidate case_id"): result
        for result in candidate_results
    }
    if len(candidate_by_case) != len(candidate_results):
        raise ValueError("candidate results contain duplicate case_id values")

    comparisons: list[dict[str, Any]] = []
    baseline_case_ids: set[str] = set()
    for result in baseline_results:
        case_id = _required_text(result.get("case_id"), field_name="baseline case_id")
        if case_id in baseline_case_ids:
            raise ValueError("baseline results contain duplicate case_id values")
        baseline_case_ids.add(case_id)
        if case_id not in candidate_by_case:
            raise ValueError(f"candidate artifact is missing case_id {case_id}")
        baseline_rank = result.get("best_evidence_rank")
        candidate_rank = candidate_by_case[case_id].get("best_evidence_rank")
        if baseline_rank is not None:
            baseline_rank = int(baseline_rank)
        if candidate_rank is not None:
            candidate_rank = int(candidate_rank)
        if baseline_rank is None and candidate_rank is not None:
            change = "miss-to-hit"
        elif baseline_rank is not None and candidate_rank is None:
            change = "hit-to-miss"
        elif baseline_rank == candidate_rank:
            change = "unchanged"
        else:
            change = "rank-changed"
        comparisons.append(
            {
                "case_id": case_id,
                "best_evidence_rank": {
                    "baseline": baseline_rank,
                    "candidate": candidate_rank,
                    "delta": (
                        candidate_rank - baseline_rank
                        if baseline_rank is not None and candidate_rank is not None
                        else None
                    ),
                    "change": change,
                },
            }
        )
    extra_cases = sorted(set(candidate_by_case) - baseline_case_ids)
    if extra_cases:
        raise ValueError(
            "candidate artifact contains unexpected case_id values: "
            + ", ".join(extra_cases)
        )
    return comparisons


def _metric_deltas(
    baseline: _ValidArtifact,
    candidate: _ValidArtifact,
) -> dict[str, Any]:
    baseline_recall = _mapping(
        baseline.summary.get("recall_at_k"),
        field_name="baseline.summary.recall_at_k",
    )
    candidate_recall = _mapping(
        candidate.summary.get("recall_at_k"),
        field_name="candidate.summary.recall_at_k",
    )
    shared_k_values = sorted(set(baseline.k_values) & set(candidate.k_values))
    recall_at_k = {
        str(k): _delta(
            float(baseline_recall[str(k)]),
            float(candidate_recall[str(k)]),
        )
        for k in shared_k_values
    }
    return {
        "recall_at_k": recall_at_k,
        "mean_reciprocal_rank": _delta(
            float(baseline.summary["mean_reciprocal_rank"]),
            float(candidate.summary["mean_reciprocal_rank"]),
        ),
    }


def compare_knowledge_recall_artifacts(
    baseline_artifact: Mapping[str, Any],
    candidate_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare two eval reports without accessing the knowledge database."""

    baseline_type = _result_type(baseline_artifact, label="baseline")
    candidate_type = _result_type(candidate_artifact, label="candidate")
    invalid_labels = [
        label
        for label, result_type in (
            ("baseline", baseline_type),
            ("candidate", candidate_type),
        )
        if result_type == "invalid"
    ]
    if invalid_labels:
        if len(invalid_labels) == 1:
            invalid_reason = f"{invalid_labels[0]} artifact is invalid."
        else:
            invalid_reason = "baseline and candidate artifacts are invalid."
        return _invalid_comparison(
            verdict="invalid-artifact",
            reason=(
                "Metric deltas were not computed because "
                + invalid_reason
            ),
        )

    baseline = _load_valid_artifact(baseline_artifact, label="baseline")
    candidate = _load_valid_artifact(candidate_artifact, label="candidate")
    baseline_fingerprint = (
        baseline.corpus_fingerprint.fingerprint
        if baseline.corpus_fingerprint is not None
        else None
    )
    candidate_fingerprint = (
        candidate.corpus_fingerprint.fingerprint
        if candidate.corpus_fingerprint is not None
        else None
    )
    checks = {
        "question_set_digest": _check(
            baseline.question_set_digest,
            candidate.question_set_digest,
        ),
        "org_id": _check(baseline.org_id, candidate.org_id),
        "k_values": _check(list(baseline.k_values), list(candidate.k_values)),
        "effective_search_limit": _check(
            baseline.effective_search_limit,
            candidate.effective_search_limit,
        ),
        "corpus_fingerprint": _check(
            baseline_fingerprint,
            candidate_fingerprint,
        ),
    }
    differences = [name for name, check in checks.items() if not check["matches"]]
    blocking_differences = [
        name
        for name in ("question_set_digest", "org_id")
        if name in differences
    ]
    if blocking_differences:
        return _invalid_comparison(
            verdict="not-comparable",
            reason=(
                "Metric deltas were not computed because the runs differ in "
                + " and ".join(blocking_differences)
                + "."
            ),
            differences=differences,
            checks=checks,
        )
    if baseline.corpus_fingerprint is None or candidate.corpus_fingerprint is None:
        return _invalid_comparison(
            verdict="not-comparable",
            reason=(
                "Metric deltas were not computed because both artifacts need a "
                "corpus fingerprint."
            ),
            differences=differences,
            checks=checks,
        )

    corpus_changed_fields = candidate.corpus_fingerprint.changed_fields_from(
        baseline.corpus_fingerprint
    )
    if corpus_changed_fields:
        verdict = "corpus-attributable"
        reason = (
            "Metric deltas were computed, but they are not attributable to code "
            "or ranking because the corpus moved."
        )
    else:
        verdict = "ranking-attributable"
        reason = (
            "The question set, organization, and corpus match. Metric deltas are "
            "attributable to code or ranking."
        )

    return {
        "comparison_type": "knowledge-recall",
        "comparability": {
            "comparable": True,
            "computed": True,
            "verdict": verdict,
            "reason": reason,
            "differences": differences,
            "checks": checks,
            "corpus_changed_fields": corpus_changed_fields,
        },
        "case_rank_deltas": _case_rank_deltas(
            baseline.results,
            candidate.results,
        ),
        "metric_deltas": _metric_deltas(baseline, candidate),
    }


__all__ = ["compare_knowledge_recall_artifacts"]
