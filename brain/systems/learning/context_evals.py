"""Deterministic shadow evaluator for active context policy candidates.

The evaluator replays selected-memory examples through the PR-L10 context
policy contract and returns persist-compatible decision payloads.  It does not
read or write runtime flags, call models, or require database access.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from math import sqrt
from typing import Any

from brain.kernel.common.coercion import as_mapping as _shared_as_mapping
from brain.kernel.common.coercion import clamp as _shared_clamp
from brain.kernel.common.coercion import coerce_datetime as _shared_coerce_datetime
from brain.kernel.common.coercion import coerce_float as _shared_coerce_float
from brain.kernel.common.coercion import coerce_int as _shared_coerce_int
from brain.kernel.common.coercion import drop_none as _shared_drop_none
from brain.kernel.common.coercion import optional_text as _shared_optional_text
from brain.kernel.common.serialization import jsonable as _shared_jsonable
from brain.systems.context.policy import (
    CONTEXT_POLICY_VERSION,
    ContextPolicyAction,
    apply_active_context_policy,
    normalize_task_class,
)

CONTEXT_POLICY_EVAL_SCHEMA_VERSION = 1
CONTEXT_POLICY_EVALUATOR_VERSION = 1
DEFAULT_POLICY_CANDIDATE_ID = "pr-l10-active-context-policy-shadow"

_INACTIVE_TRUTH_STATUSES = {
    "archived",
    "expired",
    "quarantined",
    "superseded",
}


@dataclass(frozen=True)
class ContextPolicyEvalThresholds:
    """Promotion guardrails for a shadow context-policy candidate."""

    min_eval_cases: int = 1
    min_token_savings_rate: float = 0.05
    max_missed_critical_memory_rate: float = 0.0
    max_stale_conflicted_memory_inclusion_rate: float = 0.35
    max_fallback_rate: float = 0.10
    min_verifier_user_feedback_correlation: float = -1.0

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | "ContextPolicyEvalThresholds" | None) -> "ContextPolicyEvalThresholds":
        if value is None:
            return cls()
        if isinstance(value, ContextPolicyEvalThresholds):
            return value
        return cls(
            min_eval_cases=_coerce_int(value.get("min_eval_cases"), cls.min_eval_cases),
            min_token_savings_rate=_coerce_float(value.get("min_token_savings_rate"), cls.min_token_savings_rate),
            max_missed_critical_memory_rate=_coerce_float(
                value.get("max_missed_critical_memory_rate"),
                cls.max_missed_critical_memory_rate,
            ),
            max_stale_conflicted_memory_inclusion_rate=_coerce_float(
                value.get("max_stale_conflicted_memory_inclusion_rate"),
                cls.max_stale_conflicted_memory_inclusion_rate,
            ),
            max_fallback_rate=_coerce_float(value.get("max_fallback_rate"), cls.max_fallback_rate),
            min_verifier_user_feedback_correlation=_coerce_float(
                value.get("min_verifier_user_feedback_correlation"),
                cls.min_verifier_user_feedback_correlation,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_eval_cases": self.min_eval_cases,
            "min_token_savings_rate": round(_clamp_rate(self.min_token_savings_rate), 6),
            "max_missed_critical_memory_rate": round(_clamp_rate(self.max_missed_critical_memory_rate), 6),
            "max_stale_conflicted_memory_inclusion_rate": round(
                _clamp_rate(self.max_stale_conflicted_memory_inclusion_rate),
                6,
            ),
            "max_fallback_rate": round(_clamp_rate(self.max_fallback_rate), 6),
            "min_verifier_user_feedback_correlation": round(
                max(-1.0, min(1.0, self.min_verifier_user_feedback_correlation)),
                6,
            ),
        }


@dataclass(frozen=True)
class ContextPolicyCandidate:
    """A candidate wrapper around the active context policy contract."""

    candidate_id: str = DEFAULT_POLICY_CANDIDATE_ID
    policy_version: str = CONTEXT_POLICY_VERSION
    candidate_version: int = 1
    enabled: bool = True
    description: str = "Shadow replay of the PR-L10 active context policy contract."
    section_usefulness_history: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | "ContextPolicyCandidate" | None) -> "ContextPolicyCandidate":
        if value is None:
            return cls()
        if isinstance(value, ContextPolicyCandidate):
            return value
        return cls(
            candidate_id=_text(value.get("candidate_id")) or _text(value.get("id")) or DEFAULT_POLICY_CANDIDATE_ID,
            policy_version=_text(value.get("policy_version")) or CONTEXT_POLICY_VERSION,
            candidate_version=_coerce_int(value.get("candidate_version") or value.get("version"), 1),
            enabled=_coerce_bool(value.get("enabled"), True),
            description=_text(value.get("description")) or cls.description,
            section_usefulness_history=_mapping(value.get("section_usefulness_history")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "policy_version": self.policy_version,
            "candidate_version": self.candidate_version,
            "enabled": self.enabled,
            "description": self.description,
            "contract": "brain.systems.context.policy:apply_active_context_policy",
        }


@dataclass(frozen=True)
class ContextPolicyReplayCase:
    """Normalized replay input for one context-policy evaluation case."""

    case_id: str
    case_digest: str
    task_class: str
    memories: tuple[Mapping[str, Any], ...]
    critical_memory_ids: frozenset[str] = field(default_factory=frozenset)
    stale_conflicted_memory_ids: frozenset[str] = field(default_factory=frozenset)
    section_usefulness_history: Mapping[str, Any] = field(default_factory=dict)
    attention_decision: Mapping[str, Any] = field(default_factory=dict)
    attention_explain: Mapping[str, Any] = field(default_factory=dict)
    conflict_scout: tuple[Mapping[str, Any], ...] = ()
    verifier_score: float | None = None
    user_feedback_score: float | None = None
    source_ref: Mapping[str, Any] = field(default_factory=dict)

    @property
    def replayable(self) -> bool:
        return bool(self.memories)

    def to_safe_ref(self) -> dict[str, Any]:
        return _drop_none({
            "case_id": self.case_id,
            "case_digest": self.case_digest,
            "task_class": self.task_class,
            "memory_count": len(self.memories),
            "critical_memory_count": len(self.critical_memory_ids),
            "stale_conflicted_memory_count": len(self.stale_conflicted_memory_ids),
            "source": dict(self.source_ref),
        })


def evaluate_context_policy_candidates(
    examples: Sequence[Mapping[str, Any] | Any],
    *,
    candidates: Sequence[Mapping[str, Any] | ContextPolicyCandidate] | None = None,
    thresholds: Mapping[str, Any] | ContextPolicyEvalThresholds | None = None,
    evaluated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Replay examples against context-policy candidates.

    ``examples`` may be raw trajectories, eval-case rows, hosted/internal eval
    examples, or direct mapping-shaped examples containing selected memories.
    Only examples with recoverable selected-memory candidates are replayed.
    """
    clock = _coerce_datetime(evaluated_at) or datetime.now(timezone.utc)
    threshold_config = ContextPolicyEvalThresholds.from_value(thresholds)
    replay_cases = [normalize_context_policy_eval_case(source) for source in examples]
    playable_cases = [case for case in replay_cases if case.replayable]
    candidate_list = [ContextPolicyCandidate.from_value(item) for item in (candidates or [None])]

    evaluations = [
        _evaluate_candidate(
            candidate,
            cases=playable_cases,
            total_source_count=len(replay_cases),
            thresholds=threshold_config,
            evaluated_at=clock,
        )
        for candidate in candidate_list
    ]
    payload = {
        "schema_version": CONTEXT_POLICY_EVAL_SCHEMA_VERSION,
        "evaluator": {
            "name": "brain.systems.learning.context_evals",
            "version": CONTEXT_POLICY_EVALUATOR_VERSION,
            "contract": "brain.systems.context.policy:apply_active_context_policy",
        },
        "evaluated_at": clock.isoformat(),
        "active_policy": {
            "policy_version": CONTEXT_POLICY_VERSION,
            "runtime_flags_mutated": False,
        },
        "source_count": len(replay_cases),
        "replayable_case_count": len(playable_cases),
        "skipped_case_count": len(replay_cases) - len(playable_cases),
        "thresholds": threshold_config.to_dict(),
        "candidates": evaluations,
        "case_refs": [case.to_safe_ref() for case in playable_cases],
    }
    payload["evaluation_digest"] = _stable_digest(payload)
    return payload


def build_context_policy_candidate_decision(
    examples: Sequence[Mapping[str, Any] | Any],
    *,
    candidate: Mapping[str, Any] | ContextPolicyCandidate | None = None,
    thresholds: Mapping[str, Any] | ContextPolicyEvalThresholds | None = None,
    evaluated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Convenience wrapper returning the first candidate decision payload."""
    payload = evaluate_context_policy_candidates(
        examples,
        candidates=[ContextPolicyCandidate.from_value(candidate)],
        thresholds=thresholds,
        evaluated_at=evaluated_at,
    )
    return payload["candidates"][0]


def normalize_context_policy_eval_case(source: Mapping[str, Any] | Any) -> ContextPolicyReplayCase:
    """Normalize a raw trajectory/eval/example mapping into a replay case."""
    payload = _source_payload(source)
    memories = tuple(_extract_memories(payload))
    labels = _extract_context_labels(payload)
    conflict_scout = tuple(_mapping(item) for item in _list(payload.get("conflict_scout")))
    stale_conflicted_ids = _stale_conflicted_memory_ids(memories, conflict_scout)
    critical_ids = _critical_memory_ids(payload, memories, labels)
    task_class = normalize_task_class(
        _text(payload.get("task_class"))
        or _nested_text(payload, "run_snapshot", "scout_class")
        or _nested_text(payload, "run_snapshot", "contract_type")
        or _nested_text(payload, "input_envelope", "event")
        or _nested_text(payload, "input", "event")
        or _nested_text(payload, "replay", "input", "event")
    )
    case_ref = _source_ref(payload, source)
    case_basis = {
        "source": case_ref,
        "task_class": task_class,
        "memory_refs": [_safe_memory_ref(memory, index=index) for index, memory in enumerate(memories, start=1)],
        "critical_memory_ids": sorted(critical_ids),
        "stale_conflicted_memory_ids": sorted(stale_conflicted_ids),
    }
    case_digest = _text(payload.get("case_digest")) or _text(payload.get("digest")) or _stable_digest(case_basis)
    case_id = (
        _text(payload.get("case_id"))
        or _text(payload.get("example_id"))
        or _text(payload.get("eval_digest"))
        or _text(payload.get("trace_id"))
        or f"context_policy_eval_case:{case_digest[:24]}"
    )
    return ContextPolicyReplayCase(
        case_id=case_id,
        case_digest=case_digest,
        task_class=task_class,
        memories=memories,
        critical_memory_ids=frozenset(critical_ids),
        stale_conflicted_memory_ids=frozenset(stale_conflicted_ids),
        section_usefulness_history=_section_usefulness_history(payload, labels),
        attention_decision=_mapping(payload.get("attention_decision")),
        attention_explain=_mapping(payload.get("attention_explain")),
        conflict_scout=conflict_scout,
        verifier_score=_verifier_score(payload),
        user_feedback_score=_user_feedback_score(payload),
        source_ref=case_ref,
    )


def candidate_to_policy_update_values(
    evaluation_payload: Mapping[str, Any],
    *,
    status: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    visibility: str = "private",
) -> dict[str, Any]:
    """Return a portable policy-candidate payload."""
    candidate = _mapping(evaluation_payload.get("candidate"))
    candidate_digest = _text(evaluation_payload.get("candidate_digest")) or _stable_digest(evaluation_payload)
    return {
        "candidate_digest": candidate_digest,
        "candidate_type": "context_policy",
        "policy_payload": _jsonable(candidate),
        "evaluation_payload": _jsonable(dict(evaluation_payload)),
        "status": status or ("eligible" if evaluation_payload.get("eligible") else "shadow"),
        "user_id": user_id,
        "org_id": org_id,
        "visibility": visibility,
    }


def _evaluate_candidate(
    candidate: ContextPolicyCandidate,
    *,
    cases: Sequence[ContextPolicyReplayCase],
    total_source_count: int,
    thresholds: ContextPolicyEvalThresholds,
    evaluated_at: datetime,
) -> dict[str, Any]:
    case_results = [
        _replay_case(candidate, case, evaluated_at=evaluated_at)
        for case in cases
    ]
    metrics = _aggregate_metrics(case_results, cases)
    threshold_results = _threshold_results(
        metrics,
        replayable_case_count=len(cases),
        thresholds=thresholds,
    )
    eligible = all(item["passed"] for item in threshold_results.values())
    reasons = _decision_reasons(threshold_results, eligible=eligible)
    payload = {
        "candidate": candidate.to_dict(),
        "candidate_id": candidate.candidate_id,
        "candidate_version": candidate.candidate_version,
        "policy_version": candidate.policy_version,
        "evaluated_at": evaluated_at.isoformat(),
        "source_count": total_source_count,
        "replayable_case_count": len(cases),
        "metrics": metrics,
        "thresholds": thresholds.to_dict(),
        "threshold_results": threshold_results,
        "threshold_passed": eligible,
        "shadow": not eligible,
        "eligible": eligible,
        "status": "eligible" if eligible else "shadow",
        "active_policy_changed": False,
        "decision": "eligible_for_review" if eligible else "keep_shadow",
        "decision_reasons": reasons,
        "case_results": case_results,
    }
    payload["candidate_digest"] = _stable_digest({
        "candidate": payload["candidate"],
        "metrics": metrics,
        "threshold_results": threshold_results,
    })
    return payload


def _replay_case(
    candidate: ContextPolicyCandidate,
    case: ContextPolicyReplayCase,
    *,
    evaluated_at: datetime,
) -> dict[str, Any]:
    section_history = dict(candidate.section_usefulness_history or {})
    section_history.update(dict(case.section_usefulness_history or {}))
    application = apply_active_context_policy(
        memories=case.memories,
        task_class=case.task_class,
        attention_decision=case.attention_decision,
        attention_explain=case.attention_explain,
        section_usefulness_history=section_history,
        conflict_scout=case.conflict_scout,
        enabled=candidate.enabled,
        now=evaluated_at,
    )
    policy_payload = application.to_dict()
    baseline_token_count = sum(_memory_token_count(memory) for memory in case.memories)
    included_ids = {
        _memory_ref_id(memory, index=index)
        for index, memory in enumerate(application.included_memories, start=1)
    }
    lazy_ids = {
        _memory_ref_id(memory, index=index)
        for index, memory in enumerate(application.lazy_load_memories, start=1)
    }
    suppressed_ids = {
        _memory_ref_id(memory, index=index)
        for index, memory in enumerate(application.suppressed_memories, start=1)
    }
    included_token_count = sum(_memory_token_count(memory) for memory in application.included_memories)
    saved_tokens = max(0, baseline_token_count - included_token_count)
    missed_critical = sorted(
        memory_id
        for memory_id in case.critical_memory_ids
        if memory_id not in included_ids
    )
    stale_conflicted_included = sorted(
        memory_id
        for memory_id in case.stale_conflicted_memory_ids
        if memory_id in included_ids
    )
    fallback_decision_count = sum(1 for decision in application.decisions if decision.fallback_action is not None)
    decision_count = len(application.decisions)
    return {
        "case_id": case.case_id,
        "case_digest": case.case_digest,
        "task_class": case.task_class,
        "memory_count": len(case.memories),
        "critical_memory_count": len(case.critical_memory_ids),
        "stale_conflicted_memory_count": len(case.stale_conflicted_memory_ids),
        "baseline_prompt_tokens": baseline_token_count,
        "candidate_prompt_tokens": included_token_count,
        "saved_tokens": saved_tokens,
        "token_savings_rate": _safe_rate(saved_tokens, baseline_token_count),
        "included_memory_ids": sorted(included_ids),
        "lazy_load_memory_ids": sorted(lazy_ids),
        "suppressed_memory_ids": sorted(suppressed_ids),
        "missed_critical_memory_ids": missed_critical,
        "stale_conflicted_included_memory_ids": stale_conflicted_included,
        "fallback_decision_count": fallback_decision_count,
        "decision_count": decision_count,
        "fallback_rate": _safe_rate(fallback_decision_count, decision_count),
        "verifier_score": case.verifier_score,
        "user_feedback_score": case.user_feedback_score,
        "policy_application": policy_payload,
    }


def _aggregate_metrics(
    case_results: Sequence[Mapping[str, Any]],
    cases: Sequence[ContextPolicyReplayCase],
) -> dict[str, Any]:
    baseline_tokens = sum(_coerce_int(result.get("baseline_prompt_tokens"), 0) for result in case_results)
    candidate_tokens = sum(_coerce_int(result.get("candidate_prompt_tokens"), 0) for result in case_results)
    saved_tokens = sum(_coerce_int(result.get("saved_tokens"), 0) for result in case_results)
    critical_count = sum(_coerce_int(result.get("critical_memory_count"), 0) for result in case_results)
    missed_critical_count = sum(len(_list(result.get("missed_critical_memory_ids"))) for result in case_results)
    stale_conflicted_count = sum(_coerce_int(result.get("stale_conflicted_memory_count"), 0) for result in case_results)
    stale_conflicted_included_count = sum(
        len(_list(result.get("stale_conflicted_included_memory_ids")))
        for result in case_results
    )
    decision_count = sum(_coerce_int(result.get("decision_count"), 0) for result in case_results)
    fallback_decision_count = sum(_coerce_int(result.get("fallback_decision_count"), 0) for result in case_results)
    verifier_scores = [case.verifier_score for case in cases if case.verifier_score is not None]
    user_scores = [case.user_feedback_score for case in cases if case.user_feedback_score is not None]
    paired_scores = [
        (case.verifier_score, case.user_feedback_score)
        for case in cases
        if case.verifier_score is not None and case.user_feedback_score is not None
    ]
    correlation = _pearson_correlation(paired_scores)
    return {
        "case_count": len(case_results),
        "baseline_prompt_tokens": baseline_tokens,
        "candidate_prompt_tokens": candidate_tokens,
        "saved_tokens": saved_tokens,
        "token_savings_rate": _safe_rate(saved_tokens, baseline_tokens),
        "missed_critical_memory_count": missed_critical_count,
        "critical_memory_count": critical_count,
        "missed_critical_memory_rate": _safe_rate(missed_critical_count, critical_count),
        "stale_conflicted_memory_count": stale_conflicted_count,
        "stale_conflicted_memory_included_count": stale_conflicted_included_count,
        "stale_conflicted_memory_inclusion_rate": _safe_rate(
            stale_conflicted_included_count,
            stale_conflicted_count,
        ),
        "fallback_decision_count": fallback_decision_count,
        "decision_count": decision_count,
        "fallback_rate": _safe_rate(fallback_decision_count, decision_count),
        "verifier_user_feedback_correlation": correlation,
        "verifier_user_feedback_pair_count": len(paired_scores),
        "verifier_signal_count": len(verifier_scores),
        "user_feedback_signal_count": len(user_scores),
    }


def _threshold_results(
    metrics: Mapping[str, Any],
    *,
    replayable_case_count: int,
    thresholds: ContextPolicyEvalThresholds,
) -> dict[str, dict[str, Any]]:
    correlation = metrics.get("verifier_user_feedback_correlation")
    if correlation is None:
        correlation_passed = thresholds.min_verifier_user_feedback_correlation <= -1.0
    else:
        correlation_passed = float(correlation) >= thresholds.min_verifier_user_feedback_correlation
    return {
        "min_eval_cases": {
            "metric": replayable_case_count,
            "threshold": thresholds.min_eval_cases,
            "passed": replayable_case_count >= thresholds.min_eval_cases,
        },
        "min_token_savings_rate": {
            "metric": metrics.get("token_savings_rate"),
            "threshold": thresholds.min_token_savings_rate,
            "passed": float(metrics.get("token_savings_rate") or 0.0) >= thresholds.min_token_savings_rate,
        },
        "max_missed_critical_memory_rate": {
            "metric": metrics.get("missed_critical_memory_rate"),
            "threshold": thresholds.max_missed_critical_memory_rate,
            "passed": float(metrics.get("missed_critical_memory_rate") or 0.0)
            <= thresholds.max_missed_critical_memory_rate,
        },
        "max_stale_conflicted_memory_inclusion_rate": {
            "metric": metrics.get("stale_conflicted_memory_inclusion_rate"),
            "threshold": thresholds.max_stale_conflicted_memory_inclusion_rate,
            "passed": float(metrics.get("stale_conflicted_memory_inclusion_rate") or 0.0)
            <= thresholds.max_stale_conflicted_memory_inclusion_rate,
        },
        "max_fallback_rate": {
            "metric": metrics.get("fallback_rate"),
            "threshold": thresholds.max_fallback_rate,
            "passed": float(metrics.get("fallback_rate") or 0.0) <= thresholds.max_fallback_rate,
        },
        "min_verifier_user_feedback_correlation": {
            "metric": correlation,
            "threshold": thresholds.min_verifier_user_feedback_correlation,
            "passed": correlation_passed,
        },
    }


def _decision_reasons(threshold_results: Mapping[str, Mapping[str, Any]], *, eligible: bool) -> list[str]:
    failures = [
        key
        for key, result in sorted(threshold_results.items())
        if not result.get("passed")
    ]
    if not failures:
        return ["candidate_beats_all_thresholds", "runtime_flags_left_unchanged_pending_review"]
    reasons = [f"threshold_failed:{key}" for key in failures]
    if not eligible:
        reasons.append("candidate_kept_in_shadow")
    return reasons


def _source_payload(source: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(source, Mapping):
        payload = _jsonable(dict(source))
    else:
        row_payload = getattr(source, "payload", None)
        payload = _jsonable(dict(row_payload)) if isinstance(row_payload, Mapping) else {}
        for attr, key in (
            ("eval_digest", "eval_digest"),
            ("source_run_id", "run_id"),
            ("trace_id", "trace_id"),
            ("trajectory_digest", "trajectory_digest"),
            ("context_pack_digest", "context_digest"),
            ("skill_effective_digest", "skill_effective_digest"),
        ):
            value = getattr(source, attr, None)
            if value is not None:
                payload.setdefault(key, _jsonable(value))
        quality = getattr(source, "quality", None)
        if isinstance(quality, Mapping):
            payload.setdefault("quality", _jsonable(dict(quality)))
    if not payload:
        raise TypeError("source must be a mapping or eval-case-like row")
    return payload


def _extract_memories(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    direct = _list(payload.get("memories") or payload.get("selected_memories"))
    if direct:
        return [dict(item) for item in direct if isinstance(item, Mapping)]

    context_pack = _find_context_pack(payload)
    sections = _mapping(context_pack.get("sections"))
    selected_memories = _mapping(sections.get("selected_memories"))
    content = _mapping(selected_memories.get("content"))
    items = _list(content.get("items"))
    if items:
        return [dict(item) for item in items if isinstance(item, Mapping)]

    return []


def _find_context_pack(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    candidates = (
        payload.get("context_pack"),
        _nested(payload, "replay", "context", "context_pack"),
        _nested(payload, "runtime_metadata", "context_pack"),
        _nested(payload, "context", "context_pack"),
    )
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _extract_context_labels(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    sources = (
        payload.get("context_usefulness"),
        _nested(payload, "learning_signals", "context_usefulness"),
        _nested(payload, "scoring", "learning_signals", "context_usefulness"),
        _nested(payload, "payload", "context_usefulness"),
    )
    for source in sources:
        if isinstance(source, Mapping):
            labels = _list(source.get("labels"))
            if labels:
                return [dict(item) for item in labels if isinstance(item, Mapping)]
    labels = _list(payload.get("context_labels"))
    return [dict(item) for item in labels if isinstance(item, Mapping)]


def _critical_memory_ids(
    payload: Mapping[str, Any],
    memories: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
) -> set[str]:
    explicit = set()
    for key in (
        "critical_memory_ids",
        "critical_item_ids",
        "expected_critical_memory_ids",
        "required_memory_ids",
    ):
        explicit.update(str(item) for item in _list(payload.get(key)) if str(item or "").strip())

    by_digest: dict[str, str] = {}
    ids = set()
    for index, memory in enumerate(memories, start=1):
        ref_id = _memory_ref_id(memory, index=index)
        item_digest = _text(memory.get("item_digest"))
        if item_digest:
            by_digest[item_digest] = ref_id
        if ref_id in explicit or _text(memory.get("id")) in explicit or _text(memory.get("memory_id")) in explicit:
            ids.add(ref_id)
        if _coerce_bool(memory.get("critical"), False) or _coerce_bool(memory.get("required"), False):
            ids.add(ref_id)
        expected_label = str(memory.get("expected_label") or memory.get("label") or "").strip().lower()
        if expected_label in {"critical", "required", "useful"}:
            ids.add(ref_id)

    for label in labels:
        if _text(label.get("target_type")) != "memory":
            continue
        if _text(label.get("label")) not in {"useful", "missed"}:
            continue
        item_id = _text(label.get("item_id"))
        item_digest = _text(label.get("item_digest"))
        if item_id:
            ids.add(item_id)
        elif item_digest and item_digest in by_digest:
            ids.add(by_digest[item_digest])
    return ids


def _stale_conflicted_memory_ids(
    memories: Sequence[Mapping[str, Any]],
    conflict_scout: Sequence[Mapping[str, Any]],
) -> set[str]:
    conflict_ids = {
        str(item)
        for notice in conflict_scout
        for item in _list(notice.get("conflict_ids"))
        if str(item or "").strip()
    }
    ids = set()
    for index, memory in enumerate(memories, start=1):
        ref_id = _memory_ref_id(memory, index=index)
        raw_id = _text(memory.get("id")) or _text(memory.get("memory_id"))
        if _is_stale_or_conflicted(memory) or ref_id in conflict_ids or (raw_id and raw_id in conflict_ids):
            ids.add(ref_id)
    return ids


def _is_stale_or_conflicted(memory: Mapping[str, Any]) -> bool:
    freshness = memory.get("source_freshness") or memory.get("freshness")
    freshness_map = _mapping(freshness)
    freshness_status = (
        _text(freshness_map.get("status"))
        or _text(freshness_map.get("freshness_status"))
        or (_text(freshness) if isinstance(freshness, str) else None)
        or _text(memory.get("freshness_status"))
        or _text(memory.get("source_freshness_status"))
    )
    if freshness_status in {"stale", "possibly_stale"}:
        return True
    staleness_score = _first_float(
        freshness_map.get("staleness_score"),
        memory.get("staleness_score"),
    )
    if staleness_score is not None and staleness_score >= 0.85:
        return True
    truth_status = _text(memory.get("truth_status")) or _nested_text(memory, "truth_state", "truth_status")
    if truth_status in _INACTIVE_TRUTH_STATUSES:
        return True
    if _coerce_bool(memory.get("has_open_contradiction"), False):
        return True
    if _coerce_int(memory.get("open_contradiction_count"), 0) > 0:
        return True
    if _coerce_bool(memory.get("conflicted"), False):
        return True
    return False


def _section_usefulness_history(
    payload: Mapping[str, Any],
    labels: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    history = _mapping(payload.get("section_usefulness_history"))
    if history:
        return history
    counts: dict[str, dict[str, int]] = {}
    for label in labels:
        if _text(label.get("target_type")) != "section":
            continue
        section = _text(label.get("section")) or "selected_memories"
        bucket = counts.setdefault(section, {"useful_count": 0, "unused_count": 0, "missed_count": 0, "over_budget_count": 0})
        label_name = _text(label.get("label"))
        if label_name == "useful":
            bucket["useful_count"] += 1
        elif label_name == "unused":
            bucket["unused_count"] += 1
        elif label_name == "missed":
            bucket["missed_count"] += 1
        elif label_name == "over_budget":
            bucket["over_budget_count"] += 1
    return counts


def _verifier_score(payload: Mapping[str, Any]) -> float | None:
    values = [
        _nested(payload, "quality", "verifier_status"),
        _nested(payload, "quality", "outcome_label", "verifier_signal"),
        _nested(payload, "learning_signals", "outcome_label", "verifier_signal"),
        _nested(payload, "scoring", "score_targets", "verifier_signal"),
        _nested(payload, "scoring", "quality", "verifier_status"),
        _nested(payload, "scoring", "outcome_label", "verifier_signal"),
        _nested(payload, "verifier_summary", "status"),
        _nested(payload, "replay", "verifier_summary", "status"),
    ]
    for value in values:
        score = _score_signal(value, positive={"passed", "pass", "satisfied", "success"}, negative={"failed", "fail", "unsatisfied", "error"})
        if score is not None:
            return score
    return None


def _user_feedback_score(payload: Mapping[str, Any]) -> float | None:
    feedback = (
        _nested(payload, "learning_signals", "feedback")
        or _nested(payload, "scoring", "learning_signals", "feedback")
        or payload.get("user_feedback")
        or _nested(payload, "replay", "user_feedback")
    )
    feedback_map = _mapping(feedback)
    candidates = [
        _nested(payload, "quality", "outcome_label", "user_feedback_signal"),
        _nested(payload, "learning_signals", "outcome_label", "user_feedback_signal"),
        _nested(payload, "scoring", "outcome_label", "user_feedback_signal"),
        feedback_map.get("user_feedback_signal"),
        feedback_map.get("skill_feedback"),
        feedback_map.get("feedback"),
    ]
    tags = [
        str(item).strip().lower()
        for item in _list(feedback_map.get("implicit_feedback_tags"))
        if str(item or "").strip()
    ]
    candidates.extend(tags)
    for value in candidates:
        score = _score_signal(
            value,
            positive={"good", "great", "positive", "satisfied", "accepted", "approved", "helpful"},
            negative={"bad", "poor", "negative", "rejected", "correction", "unhelpful", "followup_correction"},
        )
        if score is not None:
            return score
    return None


def _score_signal(value: Any, *, positive: set[str], negative: set[str]) -> float | None:
    text = str(value or "").strip().lower()
    if not text or text in {"none", "neutral", "unknown", "normal"}:
        return 0.0 if text in {"neutral"} else None
    if text in positive:
        return 1.0
    if text in negative:
        return -1.0
    if any(marker in text for marker in positive):
        return 1.0
    if any(marker in text for marker in negative):
        return -1.0
    return None


def _pearson_correlation(pairs: Sequence[tuple[float | None, float | None]]) -> float | None:
    clean = [(float(left), float(right)) for left, right in pairs if left is not None and right is not None]
    if len(clean) < 2:
        return None
    left_values = [left for left, _ in clean]
    right_values = [right for _, right in clean]
    left_mean = sum(left_values) / len(left_values)
    right_mean = sum(right_values) / len(right_values)
    numerator = sum((left - left_mean) * (right - right_mean) for left, right in clean)
    left_var = sum((left - left_mean) ** 2 for left in left_values)
    right_var = sum((right - right_mean) ** 2 for right in right_values)
    denominator = sqrt(left_var * right_var)
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def _safe_memory_ref(memory: Mapping[str, Any], *, index: int) -> dict[str, Any]:
    return _drop_none({
        "item_id": _memory_ref_id(memory, index=index),
        "item_digest": _text(memory.get("item_digest")),
        "content_digest": (
            _text(memory.get("content_digest"))
            or _text(memory.get("claim_digest"))
            or _text(memory.get("source_digest"))
            or _stable_text_digest(memory.get("content"))
        ),
        "memory_id": _text(memory.get("id")) or _text(memory.get("memory_id")),
        "rank": index,
    })


def _memory_ref_id(memory: Mapping[str, Any], *, index: int) -> str:
    item_id = _text(memory.get("item_id"))
    if item_id:
        return item_id
    memory_id = _text(memory.get("id")) or _text(memory.get("memory_id"))
    if memory_id:
        return f"memory:{memory_id}"
    item_digest = _text(memory.get("item_digest")) or _stable_digest({"memory": _jsonable(memory), "index": index})[:24]
    return f"memory:{item_digest}"


def _memory_token_count(memory: Mapping[str, Any]) -> int:
    for key in ("token_count", "tokens", "estimated_tokens"):
        value = _coerce_int(memory.get(key), 0)
        if value > 0:
            return value
    budget = _mapping(memory.get("token_budget"))
    value = _coerce_int(budget.get("estimated_tokens"), 0)
    if value > 0:
        return value
    content = memory.get("content")
    if content is None:
        content = {key: value for key, value in memory.items() if key not in {"item_digest", "content_digest"}}
    return _estimate_tokens(content)


def _source_ref(payload: Mapping[str, Any], original: Any) -> dict[str, Any]:
    return _drop_none({
        "kind": _source_kind(payload),
        "case_id": _text(payload.get("case_id")) or _text(payload.get("example_id")),
        "eval_digest": _text(payload.get("eval_digest")) or _text(payload.get("digest")),
        "run_id": payload.get("run_id"),
        "trace_id": _text(payload.get("trace_id")),
        "trajectory_digest": _text(payload.get("trajectory_digest")),
        "context_pack_digest": (
            _text(payload.get("context_digest"))
            or _text(payload.get("context_pack_digest"))
            or _nested_text(payload, "context", "context_pack_digest")
        ),
        "row_type": original.__class__.__name__ if not isinstance(original, Mapping) else None,
    })


def _source_kind(payload: Mapping[str, Any]) -> str:
    if "example_id" in payload and "replay" in payload:
        return "eval_example"
    if "input_envelope" in payload or "quality_signals" in payload:
        return "run_trajectory"
    if "input" in payload and "quality" in payload:
        return "trajectory_eval_case"
    return "mapping_example"


def _estimate_tokens(value: Any) -> int:
    if value is None:
        return 0
    text = value if isinstance(value, str) else json.dumps(_jsonable(value), sort_keys=True, default=str)
    return max(1, (len(text) + 3) // 4) if text else 0


def _safe_rate(numerator: int | float, denominator: int | float) -> float:
    denominator = float(denominator or 0)
    if denominator <= 0:
        return 0.0
    return round(float(numerator or 0) / denominator, 6)


def _clamp_rate(value: float | int | None) -> float:
    return _shared_clamp(value)


def _stable_digest(payload: Any, *, length: int = 64) -> str:
    raw = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _stable_text_digest(value: Any, *, length: int = 64) -> str | None:
    text = _text(value)
    if not text:
        return None
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _coerce_datetime(value: datetime | str | None) -> datetime | None:
    return _shared_coerce_datetime(value)


def _mapping(value: Any) -> dict[str, Any]:
    return _shared_as_mapping(value)


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _nested(value: Mapping[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _nested_text(value: Mapping[str, Any], *path: str) -> str | None:
    return _text(_nested(value, *path))


def _first_float(*values: Any) -> float | None:
    for value in values:
        try:
            if value is None:
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _coerce_float(value: Any, default: float = 0.0) -> float:
    return _shared_coerce_float(value, default=default)


def _coerce_int(value: Any, default: int = 0) -> int:
    return _shared_coerce_int(value, default=default)


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _text(value: Any) -> str | None:
    return _shared_optional_text(value)


def _drop_none(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _shared_drop_none(payload)


def _jsonable(value: Any) -> Any:
    return _shared_jsonable(value)


__all__ = [
    "CONTEXT_POLICY_EVAL_SCHEMA_VERSION",
    "CONTEXT_POLICY_EVALUATOR_VERSION",
    "DEFAULT_POLICY_CANDIDATE_ID",
    "ContextPolicyCandidate",
    "ContextPolicyEvalThresholds",
    "ContextPolicyReplayCase",
    "build_context_policy_candidate_decision",
    "candidate_to_policy_update_values",
    "evaluate_context_policy_candidates",
    "normalize_context_policy_eval_case",
]
