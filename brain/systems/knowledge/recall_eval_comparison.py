"""Offline comparison of serialized knowledge-recall evaluation artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from brain.systems.knowledge.recall_eval_contract import (
    KnowledgeRecallArtifact,
    KnowledgeRecallArtifactCaseResult,
    KnowledgeRecallCorpusFingerprint,
    KnowledgeRecallInvalidArtifact,
    KnowledgeRecallLegacyInvalidArtifact,
    KnowledgeRecallLegacyValidArtifact,
    KnowledgeRecallValidArtifact,
    KnowledgeRecallValidArtifactContract,
)


def _check(baseline: Any, candidate: Any) -> dict[str, Any]:
    if baseline is None or candidate is None:
        state = "unknown"
    elif baseline == candidate:
        state = "match"
    else:
        state = "different"
    return {
        "baseline": baseline,
        "candidate": candidate,
        "state": state,
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
    baseline_results: Sequence[KnowledgeRecallArtifactCaseResult],
    candidate_results: Sequence[KnowledgeRecallArtifactCaseResult],
) -> list[dict[str, Any]]:
    candidate_by_case = {result.case_id: result for result in candidate_results}
    if len(candidate_by_case) != len(candidate_results):
        raise ValueError("candidate results contain duplicate case_id values")

    comparisons: list[dict[str, Any]] = []
    baseline_case_ids: set[str] = set()
    for result in baseline_results:
        case_id = result.case_id
        if case_id in baseline_case_ids:
            raise ValueError("baseline results contain duplicate case_id values")
        baseline_case_ids.add(case_id)
        if case_id not in candidate_by_case:
            raise ValueError(f"candidate artifact is missing case_id {case_id}")
        baseline_rank = result.best_evidence_rank
        candidate_rank = candidate_by_case[case_id].best_evidence_rank
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
    baseline: KnowledgeRecallValidArtifactContract,
    candidate: KnowledgeRecallValidArtifactContract,
) -> dict[str, Any]:
    recall_at_k = {
        str(k): _delta(
            baseline.summary.recall_at_k[k],
            candidate.summary.recall_at_k[k],
        )
        for k in baseline.configuration.k_values
    }
    return {
        "recall_at_k": recall_at_k,
        "mean_reciprocal_rank": _delta(
            baseline.summary.mean_reciprocal_rank,
            candidate.summary.mean_reciprocal_rank,
        ),
    }


def _fingerprint(
    artifact: KnowledgeRecallValidArtifactContract,
) -> KnowledgeRecallCorpusFingerprint | None:
    if isinstance(artifact, KnowledgeRecallValidArtifact):
        return artifact.corpus_fingerprint
    return None


def _computed_comparison(
    *,
    baseline: KnowledgeRecallValidArtifactContract,
    candidate: KnowledgeRecallValidArtifactContract,
    verdict: Literal["corpus-unknown", "corpus-changed", "ranking-attributable"],
    reason: str,
    differences: Sequence[str],
    checks: Mapping[str, Any],
    corpus_changed_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "comparison_type": "knowledge-recall",
        "comparability": {
            "comparable": True,
            "computed": True,
            "verdict": verdict,
            "reason": reason,
            "differences": list(differences),
            "checks": dict(checks),
            "corpus_changed_fields": dict(corpus_changed_fields or {}),
        },
        "case_rank_deltas": _case_rank_deltas(
            baseline.results,
            candidate.results,
        ),
        "metric_deltas": _metric_deltas(baseline, candidate),
    }


def compare_knowledge_recall_artifacts(
    baseline_artifact: KnowledgeRecallArtifact,
    candidate_artifact: KnowledgeRecallArtifact,
) -> dict[str, Any]:
    """Compare two strict artifacts without accessing the knowledge database.

    A corpus change is a confounder, not proof of causation. True corpus-versus-
    ranking attribution needs a controlled comparison that holds the ranker
    constant across two corpus snapshots.
    """

    invalid_types = (
        KnowledgeRecallInvalidArtifact,
        KnowledgeRecallLegacyInvalidArtifact,
    )
    invalid_labels = [
        label
        for label, artifact in (
            ("baseline", baseline_artifact),
            ("candidate", candidate_artifact),
        )
        if isinstance(artifact, invalid_types)
    ]
    if invalid_labels:
        invalid_reason = (
            f"{invalid_labels[0]} artifact is invalid."
            if len(invalid_labels) == 1
            else "baseline and candidate artifacts are invalid."
        )
        return _invalid_comparison(
            verdict="invalid-artifact",
            reason="Metric deltas were not computed because " + invalid_reason,
        )

    if not isinstance(
        baseline_artifact,
        (KnowledgeRecallValidArtifact, KnowledgeRecallLegacyValidArtifact),
    ) or not isinstance(
        candidate_artifact,
        (KnowledgeRecallValidArtifact, KnowledgeRecallLegacyValidArtifact),
    ):
        raise TypeError("comparison requires knowledge-recall artifacts")
    baseline = baseline_artifact
    candidate = candidate_artifact
    baseline_corpus = _fingerprint(baseline)
    candidate_corpus = _fingerprint(candidate)
    checks = {
        "question_set_digest": _check(
            baseline.question_set.digest,
            candidate.question_set.digest,
        ),
        "org_id": _check(
            baseline.configuration.org_id,
            candidate.configuration.org_id,
        ),
        "k_values": _check(
            list(baseline.configuration.k_values),
            list(candidate.configuration.k_values),
        ),
        "requested_search_limit": _check(
            baseline.configuration.requested_search_limit,
            candidate.configuration.requested_search_limit,
        ),
        "effective_search_limit": _check(
            baseline.configuration.effective_search_limit,
            candidate.configuration.effective_search_limit,
        ),
        "corpus_fingerprint": _check(
            baseline_corpus.fingerprint if baseline_corpus is not None else None,
            candidate_corpus.fingerprint if candidate_corpus is not None else None,
        ),
    }
    differences = [
        name for name, check in checks.items() if check["state"] == "different"
    ]
    blocking_differences = [
        name
        for name in (
            "question_set_digest",
            "org_id",
            "k_values",
            "requested_search_limit",
            "effective_search_limit",
        )
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

    if baseline_corpus is None or candidate_corpus is None:
        return _computed_comparison(
            baseline=baseline,
            candidate=candidate,
            verdict="corpus-unknown",
            reason=(
                "Metric deltas were computed, but one or both artifacts have no "
                "corpus fingerprint. The corpus state is unknown, so no "
                "attribution is possible."
            ),
            differences=differences,
            checks=checks,
        )

    if baseline_corpus.fingerprint != candidate_corpus.fingerprint:
        corpus_changed_fields = candidate_corpus.changed_fields_from(
            baseline_corpus
        )
        return _computed_comparison(
            baseline=baseline,
            candidate=candidate,
            verdict="corpus-changed",
            reason=(
                "The corpus changed. Metric deltas may combine corpus and code "
                "movement, so these runs cannot separate them."
            ),
            differences=differences,
            checks=checks,
            corpus_changed_fields=corpus_changed_fields,
        )

    return _computed_comparison(
        baseline=baseline,
        candidate=candidate,
        verdict="ranking-attributable",
        reason=(
            "The question set, organization, measurement configuration, and "
            "corpus match. Metric deltas are attributable to code or ranking."
        ),
        differences=differences,
        checks=checks,
    )


__all__ = ["compare_knowledge_recall_artifacts"]
