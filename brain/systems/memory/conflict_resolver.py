"""Nightly memory conflict resolution planning.

This module is deliberately deterministic.  It consumes conflict signals that
were already discovered elsewhere and emits reversible action plans; it never
calls a model or deletes memory content.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal

from brain.kernel.common.coercion import coerce_datetime as _shared_coerce_datetime
from brain.kernel.common.coercion import coerce_float as _shared_coerce_float
from brain.kernel.common.coercion import object_to_dict as _shared_object_to_dict
from brain.systems.learning.budget import LearningBudgetLedger, LearningBudgetPolicy
from brain.systems.learning.night_budget import (
    NightBudgetCandidate,
    NightBudgetSettings,
    NightWorkType,
    build_night_budget_plan,
)
from brain.systems.memory.truth_maintenance import normalize_memory_claim_metadata

ResolutionActionName = Literal[
    "keep_both_with_windows",
    "supersede_older",
    "archive_stale",
    "quarantine_uncertain",
    "admin_review",
]
FreshnessStatus = Literal["fresh", "possibly_stale", "stale", "unknown"]

_RESOLVED_STATUSES = {"resolved", "closed", "dismissed", "accepted"}
_INACTIVE_TRUTH_STATUSES = {"archived", "expired", "quarantined", "superseded"}
_CORRECTION_RE = re.compile(
    r"(?i)\b(?:actually\s+)?use\s+(?P<new>.+?)\s+instead\s+of\s+(?P<old>.+?)(?:[.!?]|$)"
)
_NOT_CORRECTION_RE = re.compile(
    r"(?i)\buse\s+(?P<new>.+?),?\s+not\s+(?P<old>.+?)(?:[.!?]|$)"
)

_ACTION_PRECEDENCE: Mapping[ResolutionActionName, int] = {
    "supersede_older": 0,
    "archive_stale": 1,
    "keep_both_with_windows": 2,
    "quarantine_uncertain": 3,
    "admin_review": 4,
}
_ACTION_TOKEN_ESTIMATES: Mapping[ResolutionActionName, int] = {
    "keep_both_with_windows": 80,
    "supersede_older": 140,
    "archive_stale": 90,
    "quarantine_uncertain": 160,
    "admin_review": 320,
}


@dataclass(frozen=True)
class MemoryConflictEvidence:
    """Normalized memory claim metadata used by deterministic rules."""

    memory_id: str
    content: str = ""
    truth_status: str = "unknown"
    review_status: str = "unreviewed"
    confidence: float = 0.5
    freshness_status: FreshnessStatus = "unknown"
    freshness_confidence: float | None = None
    staleness_score: float | None = None
    freshness_score: float | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    observed_at: datetime | None = None
    created_at: datetime | None = None
    superseded_by: str | None = None
    subject_key: str | None = None
    source_digest: str | None = None
    org_id: str | None = None
    user_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.truth_status not in _INACTIVE_TRUTH_STATUSES and not bool(self.raw.get("archived"))

    @property
    def sort_time(self) -> datetime | None:
        return self.valid_from or self.observed_at or self.created_at


@dataclass(frozen=True)
class MemoryConflictResolutionAction:
    """A reversible action proposed by the nightly resolver."""

    action: ResolutionActionName
    memory_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    confidence: float
    reasons: tuple[str, ...]
    targets: Mapping[str, Any]
    rollback_metadata: Mapping[str, Any]
    estimated_tokens: int
    deferred: bool = False
    budget_decision: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        return str(self.rollback_metadata.get("idempotency_key") or "")

    def with_budget(self, decision: Mapping[str, Any] | None, *, deferred: bool) -> "MemoryConflictResolutionAction":
        return replace(self, budget_decision=dict(decision or {}), deferred=deferred)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "memory_ids": list(self.memory_ids),
            "source_ids": list(self.source_ids),
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "targets": _jsonable(self.targets),
            "rollback_metadata": _jsonable(self.rollback_metadata),
            "estimated_tokens": self.estimated_tokens,
            "deferred": self.deferred,
            "budget_decision": _jsonable(self.budget_decision or {}),
            "metadata": _jsonable(self.metadata),
        }


@dataclass(frozen=True)
class MemoryConflictResolutionPlan:
    """Budget-aware set of actions for a nightly memory quality run."""

    actions: tuple[MemoryConflictResolutionAction, ...]
    budget_summary: Mapping[str, Any] = field(default_factory=dict)
    input_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def allowed_actions(self) -> tuple[MemoryConflictResolutionAction, ...]:
        return tuple(action for action in self.actions if not action.deferred)

    @property
    def deferred_actions(self) -> tuple[MemoryConflictResolutionAction, ...]:
        return tuple(action for action in self.actions if action.deferred)

    @property
    def idempotency_key(self) -> str:
        return _stable_digest(
            {
                "actions": [
                    action.rollback_metadata.get("idempotency_key")
                    for action in self.actions
                ]
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "allowed_count": len(self.allowed_actions),
            "deferred_count": len(self.deferred_actions),
            "input_counts": dict(self.input_counts),
            "budget_summary": _jsonable(self.budget_summary),
            "actions": [action.to_dict() for action in self.actions],
        }


def resolve_memory_conflicts(
    *,
    context_notices: Sequence[Any] | None = None,
    contradiction_rows: Sequence[Any] | None = None,
    freshness_signals: Any = None,
    user_corrections: Sequence[Any] | None = None,
    memories: Sequence[Any] | None = None,
    policy: LearningBudgetPolicy | None = None,
    ledger: LearningBudgetLedger | None = None,
    budget_settings: NightBudgetSettings | None = None,
    use_night_budget: bool = True,
) -> MemoryConflictResolutionPlan:
    """Return deterministic, budget-aware memory conflict actions.

    The inputs intentionally accept dicts, ORM rows, Pydantic models, dataclass
    objects, and L08 ``ContextConflictNotice`` instances.  Ambiguous semantic
    cases are represented as ``admin_review`` or ``quarantine_uncertain``
    actions with deferred escalation metadata instead of provider calls.
    """
    memory_map = _build_memory_map(memories)
    normalized_freshness = _normalize_freshness_signals(freshness_signals)
    for signal in normalized_freshness:
        memory_id = str(signal.get("memory_id") or "")
        if memory_id:
            memory_map[memory_id] = _merge_memory_evidence(
                memory_map.get(memory_id),
                _normalize_memory_evidence(signal),
            )

    actions: list[MemoryConflictResolutionAction] = []
    actions.extend(_actions_from_user_corrections(user_corrections or (), memory_map))
    actions.extend(_actions_from_context_notices(context_notices or (), memory_map, normalized_freshness))
    actions.extend(_actions_from_contradictions(contradiction_rows or (), memory_map))
    actions.extend(_actions_from_freshness(normalized_freshness, memory_map))

    deduped = _dedupe_actions(actions)
    if use_night_budget:
        deduped, budget_summary = _apply_night_budget(
            deduped,
            policy=policy,
            ledger=ledger,
            settings=budget_settings,
        )
    else:
        budget_summary = {"enabled": False}

    return MemoryConflictResolutionPlan(
        actions=tuple(deduped),
        budget_summary=budget_summary,
        input_counts={
            "context_notices": len(context_notices or ()),
            "contradiction_rows": len(contradiction_rows or ()),
            "freshness_signals": len(normalized_freshness),
            "user_corrections": len(user_corrections or ()),
            "memories": len(memories or ()),
        },
    )


def _build_memory_map(memories: Sequence[Any] | None) -> dict[str, MemoryConflictEvidence]:
    memory_map: dict[str, MemoryConflictEvidence] = {}
    for memory in memories or ():
        evidence = _normalize_memory_evidence(memory)
        if evidence.memory_id:
            memory_map[evidence.memory_id] = _merge_memory_evidence(memory_map.get(evidence.memory_id), evidence)
    return memory_map


def _normalize_memory_evidence(value: Any) -> MemoryConflictEvidence:
    data = _object_to_dict(value)
    claim = normalize_memory_claim_metadata(data)
    memory_id = _clean_id(data.get("memory_id") or data.get("id"))
    freshness_status, freshness_confidence = _freshness_from_mapping(data)
    freshness_score = _coerce_float(data.get("freshness_score"))
    staleness_score = _coerce_float(data.get("staleness_score"))
    if staleness_score is None:
        staleness_score = _coerce_float(claim.get("staleness_score"))
    if freshness_status == "unknown" and staleness_score is not None:
        freshness_status = _freshness_from_staleness(staleness_score)
    if freshness_status == "unknown" and freshness_score is not None:
        freshness_status = _freshness_from_staleness(1.0 - _clamp(freshness_score))

    return MemoryConflictEvidence(
        memory_id=memory_id,
        content=str(data.get("content") or data.get("text") or data.get("claim") or ""),
        truth_status=str(data.get("truth_status") or "unknown").strip().lower() or "unknown",
        review_status=str(data.get("review_status") or "unreviewed").strip().lower() or "unreviewed",
        confidence=_clamp(_coerce_float(data.get("confidence"), 0.5) or 0.5),
        freshness_status=freshness_status,
        freshness_confidence=freshness_confidence,
        staleness_score=staleness_score,
        freshness_score=freshness_score,
        valid_from=_coerce_datetime(data.get("valid_from")) or claim.get("valid_from"),
        valid_until=_coerce_datetime(data.get("valid_until")) or claim.get("valid_until"),
        observed_at=_coerce_datetime(data.get("observed_at")) or claim.get("observed_at"),
        created_at=_coerce_datetime(data.get("created_at")),
        superseded_by=_clean_id(data.get("superseded_by")),
        subject_key=_subject_key(data, claim),
        source_digest=str(data.get("source_digest") or claim.get("source_digest") or "") or None,
        org_id=_clean_id(data.get("org_id")),
        user_id=_clean_id(data.get("user_id")),
        raw=data,
    )


def _merge_memory_evidence(
    existing: MemoryConflictEvidence | None,
    incoming: MemoryConflictEvidence,
) -> MemoryConflictEvidence:
    if existing is None or not existing.memory_id:
        return incoming
    raw = {**existing.raw, **incoming.raw}
    return replace(
        existing,
        content=incoming.content or existing.content,
        truth_status=incoming.truth_status if incoming.truth_status != "unknown" else existing.truth_status,
        review_status=incoming.review_status if incoming.review_status != "unreviewed" else existing.review_status,
        confidence=max(existing.confidence, incoming.confidence),
        freshness_status=_stronger_freshness(existing.freshness_status, incoming.freshness_status),
        freshness_confidence=incoming.freshness_confidence or existing.freshness_confidence,
        staleness_score=incoming.staleness_score if incoming.staleness_score is not None else existing.staleness_score,
        freshness_score=incoming.freshness_score if incoming.freshness_score is not None else existing.freshness_score,
        valid_from=incoming.valid_from or existing.valid_from,
        valid_until=incoming.valid_until or existing.valid_until,
        observed_at=incoming.observed_at or existing.observed_at,
        created_at=incoming.created_at or existing.created_at,
        superseded_by=incoming.superseded_by or existing.superseded_by,
        subject_key=incoming.subject_key or existing.subject_key,
        source_digest=incoming.source_digest or existing.source_digest,
        org_id=incoming.org_id or existing.org_id,
        user_id=incoming.user_id or existing.user_id,
        raw=raw,
    )


def _actions_from_user_corrections(
    corrections: Sequence[Any],
    memory_map: Mapping[str, MemoryConflictEvidence],
) -> list[MemoryConflictResolutionAction]:
    actions: list[MemoryConflictResolutionAction] = []
    for correction in corrections:
        data = _object_to_dict(correction)
        correction_id = _source_id("user_correction", data)
        old_id = _clean_id(
            data.get("old_memory_id")
            or data.get("replaced_memory_id")
            or data.get("superseded_memory_id")
            or data.get("stale_memory_id")
        )
        new_id = _clean_id(
            data.get("new_memory_id")
            or data.get("preferred_memory_id")
            or data.get("superseding_memory_id")
        )
        memory_id = _clean_id(data.get("memory_id") or data.get("id"))
        confidence = _clamp(_coerce_float(data.get("confidence"), 0.98) or 0.98)
        reason = str(data.get("reason") or data.get("rationale") or "user_correction").strip()

        if old_id and new_id and old_id != new_id:
            actions.append(
                _make_action(
                    "supersede_older",
                    memory_ids=_ordered_ids((old_id, new_id)),
                    source_ids=(correction_id,),
                    confidence=max(confidence, 0.96),
                    reasons=(reason, "user_correction_precedence"),
                    targets={"superseded_memory_id": old_id, "superseding_memory_id": new_id},
                    memory_map=memory_map,
                    metadata={"source": "user_correction", "llm_call": False},
                )
            )
            continue

        stale_id = old_id or _clean_id(data.get("archive_memory_id") or data.get("stale_id"))
        action_name = str(data.get("action") or "").strip().lower()
        if stale_id and action_name in {"archive", "archive_stale", "stale", "mark_stale"}:
            actions.append(
                _make_action(
                    "archive_stale",
                    memory_ids=(stale_id,),
                    source_ids=(correction_id,),
                    confidence=max(confidence, 0.94),
                    reasons=(reason, "user_marked_memory_stale"),
                    targets={"stale_memory_id": stale_id},
                    memory_map=memory_map,
                    metadata={"source": "user_correction", "llm_call": False},
                )
            )
            continue

        content = str(data.get("content") or data.get("text") or "")
        if _extract_correction_terms(content):
            ids = tuple(mid for mid in _ordered_ids((old_id, new_id, memory_id)) if mid)
            actions.append(
                _make_action(
                    "admin_review",
                    memory_ids=ids,
                    source_ids=(correction_id,),
                    confidence=min(confidence, 0.72),
                    reasons=("user_correction_missing_memory_ids", reason),
                    targets={"candidate_memory_id": memory_id or None},
                    memory_map=memory_map,
                    metadata=_deferred_semantic_metadata("correction_text_needs_memory_match"),
                )
            )
    return actions


def _actions_from_context_notices(
    notices: Sequence[Any],
    memory_map: Mapping[str, MemoryConflictEvidence],
    freshness_signals: Sequence[Mapping[str, Any]],
) -> list[MemoryConflictResolutionAction]:
    freshness_by_memory = {
        str(signal.get("memory_id")): signal
        for signal in freshness_signals
        if signal.get("memory_id") is not None
    }
    actions: list[MemoryConflictResolutionAction] = []
    for notice in notices:
        data = _object_to_dict(notice)
        conflict_ids = _coerce_id_tuple(data.get("conflict_ids") or data.get("memory_ids"))
        if len(conflict_ids) < 2:
            continue
        reasons = _string_tuple(data.get("reasons")) or ("context_conflict_notice",)
        confidence = _clamp(_coerce_float(data.get("confidence"), 0.62) or 0.62)
        preferred_id = _clean_id(data.get("preferred_memory_id"))
        source_id = _source_id("context_notice", {**data, "id": data.get("id") or "-".join(conflict_ids)})

        if preferred_id and str(data.get("recommended_action") or "") == "include_one":
            other_ids = [memory_id for memory_id in conflict_ids if memory_id != preferred_id]
            superseded_id = _choose_superseded_memory(other_ids, preferred_id, memory_map)
            actions.append(
                _make_action(
                    "supersede_older",
                    memory_ids=_ordered_ids((superseded_id, preferred_id)),
                    source_ids=(source_id,),
                    confidence=max(confidence, 0.78),
                    reasons=(*reasons, str(data.get("preferred_reason") or "preferred_memory_notice")),
                    targets={"superseded_memory_id": superseded_id, "superseding_memory_id": preferred_id},
                    memory_map=memory_map,
                    metadata={"source": "context_conflict_notice", "llm_call": False},
                )
            )
            continue

        stale_id = _fresh_stale_preference(conflict_ids, memory_map, freshness_by_memory)
        if stale_id:
            actions.append(
                _make_action(
                    "archive_stale",
                    memory_ids=_ordered_ids(conflict_ids),
                    source_ids=(source_id,),
                    confidence=max(confidence, 0.72),
                    reasons=(*reasons, "source_freshness_mismatch"),
                    targets={"stale_memory_id": stale_id},
                    memory_map=memory_map,
                    metadata={"source": "context_conflict_notice", "llm_call": False},
                )
            )
            continue

        if _has_non_overlapping_windows(conflict_ids, memory_map):
            actions.append(
                _make_action(
                    "keep_both_with_windows",
                    memory_ids=_ordered_ids(conflict_ids),
                    source_ids=(source_id,),
                    confidence=max(confidence, 0.8),
                    reasons=(*reasons, "validity_windows_do_not_overlap"),
                    targets={"windowed_memory_ids": list(conflict_ids)},
                    memory_map=memory_map,
                    metadata={"source": "context_conflict_notice", "llm_call": False},
                )
            )
            continue

        actions.append(
            _make_action(
                "admin_review",
                memory_ids=_ordered_ids(conflict_ids),
                source_ids=(source_id,),
                confidence=min(confidence, 0.66),
                reasons=(*reasons, "semantic_adjudication_deferred"),
                targets={"review_memory_ids": list(conflict_ids)},
                memory_map=memory_map,
                metadata=_deferred_semantic_metadata("ambiguous_context_conflict"),
            )
        )
    return actions


def _actions_from_contradictions(
    rows: Sequence[Any],
    memory_map: Mapping[str, MemoryConflictEvidence],
) -> list[MemoryConflictResolutionAction]:
    actions: list[MemoryConflictResolutionAction] = []
    for row in rows:
        data = _object_to_dict(row)
        status = str(data.get("status") or "open").strip().lower()
        if status in _RESOLVED_STATUSES:
            continue
        left_id = _clean_id(data.get("left_memory_id"))
        right_id = _clean_id(data.get("right_memory_id"))
        if not left_id or not right_id or left_id == right_id:
            continue
        evidence = _coerce_jsonish(data.get("evidence"))
        if not isinstance(evidence, Mapping):
            evidence = {}
        severity = _clamp(_coerce_float(data.get("severity"), 0.5) or 0.5)
        confidence = _clamp(
            _coerce_float(data.get("confidence"))
            or _coerce_float(evidence.get("confidence"))
            or severity
        )
        contradiction_type = str(data.get("contradiction_type") or "memory_contradiction")
        source_id = _source_id("contradiction", data)
        reasons = (contradiction_type, f"truth_maintenance_status:{status}")

        preferred_id = _preferred_from_evidence(evidence)
        if preferred_id in {left_id, right_id}:
            superseded_id = right_id if preferred_id == left_id else left_id
            actions.append(
                _make_action(
                    "supersede_older",
                    memory_ids=_ordered_ids((left_id, right_id)),
                    source_ids=(source_id,),
                    confidence=max(confidence, 0.82),
                    reasons=(*reasons, "contradiction_evidence_preference"),
                    targets={"superseded_memory_id": superseded_id, "superseding_memory_id": preferred_id},
                    memory_map=memory_map,
                    metadata={"source": "truth_maintenance", "llm_call": False},
                )
            )
            continue

        older_id, newer_id = _older_newer(left_id, right_id, memory_map)
        if "supersession" in contradiction_type and older_id and newer_id:
            actions.append(
                _make_action(
                    "supersede_older",
                    memory_ids=_ordered_ids((left_id, right_id)),
                    source_ids=(source_id,),
                    confidence=max(confidence, 0.78),
                    reasons=(*reasons, "semantic_supersession_row"),
                    targets={"superseded_memory_id": older_id, "superseding_memory_id": newer_id},
                    memory_map=memory_map,
                    metadata={"source": "truth_maintenance", "llm_call": False},
                )
            )
            continue

        stale_id = _fresh_stale_preference((left_id, right_id), memory_map, {})
        if stale_id and severity >= 0.55:
            actions.append(
                _make_action(
                    "archive_stale",
                    memory_ids=_ordered_ids((left_id, right_id)),
                    source_ids=(source_id,),
                    confidence=max(confidence, 0.72),
                    reasons=(*reasons, "stale_side_of_contradiction"),
                    targets={"stale_memory_id": stale_id},
                    memory_map=memory_map,
                    metadata={"source": "truth_maintenance", "llm_call": False},
                )
            )
            continue

        if _has_non_overlapping_windows((left_id, right_id), memory_map):
            actions.append(
                _make_action(
                    "keep_both_with_windows",
                    memory_ids=_ordered_ids((left_id, right_id)),
                    source_ids=(source_id,),
                    confidence=max(confidence, 0.76),
                    reasons=(*reasons, "temporal_claim_windows_separate"),
                    targets={"windowed_memory_ids": [left_id, right_id]},
                    memory_map=memory_map,
                    metadata={"source": "truth_maintenance", "llm_call": False},
                )
            )
            continue

        action_name: ResolutionActionName = "quarantine_uncertain" if severity >= 0.82 else "admin_review"
        actions.append(
            _make_action(
                action_name,
                memory_ids=_ordered_ids((left_id, right_id)),
                source_ids=(source_id,),
                confidence=min(confidence, 0.68 if action_name == "quarantine_uncertain" else 0.62),
                reasons=(*reasons, "no_deterministic_winner", "semantic_adjudication_deferred"),
                targets={"review_memory_ids": [left_id, right_id]},
                memory_map=memory_map,
                metadata=_deferred_semantic_metadata("truth_contradiction_needs_review"),
            )
        )
    return actions


def _actions_from_freshness(
    signals: Sequence[Mapping[str, Any]],
    memory_map: Mapping[str, MemoryConflictEvidence],
) -> list[MemoryConflictResolutionAction]:
    actions: list[MemoryConflictResolutionAction] = []
    for signal in signals:
        memory_id = _clean_id(signal.get("memory_id"))
        if not memory_id:
            continue
        status = _coerce_freshness_status(signal.get("status") or signal.get("freshness_status"))
        if status not in {"stale", "possibly_stale"}:
            continue
        source_id = _source_id("freshness", signal)
        confidence = _clamp(_coerce_float(signal.get("confidence"), 0.0) or 0.0)
        score = _coerce_float(signal.get("score"))
        staleness = _coerce_float(signal.get("staleness_score"))
        if staleness is None and score is not None:
            staleness = 1.0 - _clamp(score)
        if confidence <= 0.0:
            confidence = 0.84 if status == "stale" else 0.55
        reasons = _string_tuple(signal.get("reasons")) or (f"source_freshness:{status}",)
        if status == "stale" and (staleness is None or staleness >= 0.75 or confidence >= 0.8):
            actions.append(
                _make_action(
                    "archive_stale",
                    memory_ids=(memory_id,),
                    source_ids=(source_id,),
                    confidence=max(confidence, 0.8),
                    reasons=(*reasons, "stale_source_signal"),
                    targets={"stale_memory_id": memory_id},
                    memory_map=memory_map,
                    metadata={"source": "source_freshness", "llm_call": False},
                )
            )
        else:
            actions.append(
                _make_action(
                    "admin_review",
                    memory_ids=(memory_id,),
                    source_ids=(source_id,),
                    confidence=min(confidence, 0.58),
                    reasons=(*reasons, "possibly_stale_needs_review"),
                    targets={"review_memory_ids": [memory_id]},
                    memory_map=memory_map,
                    metadata={"source": "source_freshness", "llm_call": False},
                )
            )
    return actions


def _make_action(
    action: ResolutionActionName,
    *,
    memory_ids: Sequence[str],
    source_ids: Sequence[str],
    confidence: float,
    reasons: Sequence[str],
    targets: Mapping[str, Any],
    memory_map: Mapping[str, MemoryConflictEvidence],
    metadata: Mapping[str, Any] | None = None,
) -> MemoryConflictResolutionAction:
    normalized_ids = tuple(_clean_id(memory_id) for memory_id in memory_ids if _clean_id(memory_id))
    normalized_sources = tuple(sorted({str(source_id) for source_id in source_ids if source_id}))
    normalized_reasons = tuple(_dedupe(str(reason) for reason in reasons if str(reason or "").strip()))
    normalized_targets = _jsonable(dict(targets))
    key = _stable_digest(
        {
            "action": action,
            "memory_ids": normalized_ids,
            "targets": normalized_targets,
        }
    )
    rollback_ids = tuple(_target_memory_ids(action, normalized_ids, normalized_targets))
    rollback_metadata = {
        "idempotency_key": key,
        "inverse_action": "restore_memory_truth_state",
        "affected_memory_ids": list(rollback_ids),
        "restore_fields": {
            memory_id: _rollback_fields(memory_map.get(memory_id))
            for memory_id in rollback_ids
        },
        "preserves_memory_content": True,
        "safe_to_replay": True,
    }
    return MemoryConflictResolutionAction(
        action=action,
        memory_ids=normalized_ids,
        source_ids=normalized_sources,
        confidence=round(_clamp(confidence), 3),
        reasons=normalized_reasons,
        targets=normalized_targets,
        rollback_metadata=rollback_metadata,
        estimated_tokens=int(_ACTION_TOKEN_ESTIMATES[action]),
        metadata=dict(metadata or {}),
    )


def _apply_night_budget(
    actions: Sequence[MemoryConflictResolutionAction],
    *,
    policy: LearningBudgetPolicy | None,
    ledger: LearningBudgetLedger | None,
    settings: NightBudgetSettings | None,
) -> tuple[list[MemoryConflictResolutionAction], dict[str, Any]]:
    if not actions:
        return [], {"enabled": True, "allowed_count": 0, "deferred_count": 0}
    candidates = [
        NightBudgetCandidate(
            candidate_id=action.idempotency_key,
            work_type=NightWorkType.MEMORY_CONFLICT_RESOLUTION,
            estimated_tokens=action.estimated_tokens,
            org_id=_first_scope(action, "org_id"),
            user_id=_first_scope(action, "user_id"),
            subject_ref=",".join(action.memory_ids),
            impact_score=_action_impact_score(action),
            signals={
                "conflict_severity": action.confidence,
                "review_status": "needs_review" if action.action in {"admin_review", "quarantine_uncertain"} else "reviewed",
                "truth_status": "stale" if action.action == "archive_stale" else "conflict",
            },
            metadata={"resolution_action": action.action},
        )
        for action in actions
    ]
    plan = build_night_budget_plan(
        candidates,
        policy=policy,
        ledger=ledger,
        settings=settings,
    )
    by_id = {item.candidate.candidate_id: item for item in plan.items}
    budgeted: list[MemoryConflictResolutionAction] = []
    for action in actions:
        item = by_id.get(action.idempotency_key)
        if item is None:
            budgeted.append(action)
            continue
        budgeted.append(
            action.with_budget(
                {
                    "allowed": item.allowed,
                    "action": str(item.decision.action),
                    "reason": item.decision.reason,
                    "remaining_tokens": item.decision.remaining_tokens,
                    "would_spend_tokens": item.decision.would_spend_tokens,
                    "sequence_no": item.sequence_no,
                    "priority_score": item.priority_score,
                    "tenant_key": item.tenant_key,
                },
                deferred=not item.allowed,
            )
        )
    return budgeted, {**plan.to_payload(), "enabled": True}


def _dedupe_actions(
    actions: Sequence[MemoryConflictResolutionAction],
) -> list[MemoryConflictResolutionAction]:
    merged: dict[tuple[str, tuple[tuple[str, Any], ...]], MemoryConflictResolutionAction] = {}
    target_winners: dict[tuple[str, str], MemoryConflictResolutionAction] = {}

    for action in sorted(actions, key=_action_sort_key):
        merge_key = (
            action.action,
            tuple(sorted((key, json.dumps(value, sort_keys=True, default=str)) for key, value in action.targets.items())),
        )
        existing = merged.get(merge_key)
        if existing is not None:
            merged[merge_key] = _merge_actions(existing, action)
            continue

        target_key = _single_target_key(action)
        if target_key is not None and target_key in target_winners:
            winner = target_winners[target_key]
            if _ACTION_PRECEDENCE[action.action] < _ACTION_PRECEDENCE[winner.action]:
                merged.pop(_merge_key_for_action(winner), None)
                merged[merge_key] = _merge_actions(action, winner)
                target_winners[target_key] = merged[merge_key]
            else:
                merged[_merge_key_for_action(winner)] = _merge_actions(winner, action)
            continue

        merged[merge_key] = action
        if target_key is not None:
            target_winners[target_key] = action

    return sorted(merged.values(), key=_action_sort_key)


def _merge_actions(
    preferred: MemoryConflictResolutionAction,
    other: MemoryConflictResolutionAction,
) -> MemoryConflictResolutionAction:
    reasons = tuple(_dedupe((*preferred.reasons, *other.reasons)))
    source_ids = tuple(sorted({*preferred.source_ids, *other.source_ids}))
    memory_ids = tuple(_ordered_ids((*preferred.memory_ids, *other.memory_ids)))
    metadata = {**other.metadata, **preferred.metadata}
    return replace(
        preferred,
        memory_ids=memory_ids,
        source_ids=source_ids,
        confidence=round(max(preferred.confidence, other.confidence), 3),
        reasons=reasons,
        metadata=metadata,
    )


def _merge_key_for_action(action: MemoryConflictResolutionAction) -> tuple[str, tuple[tuple[str, Any], ...]]:
    return (
        action.action,
        tuple(sorted((key, json.dumps(value, sort_keys=True, default=str)) for key, value in action.targets.items())),
    )


def _single_target_key(action: MemoryConflictResolutionAction) -> tuple[str, str] | None:
    for key in ("superseded_memory_id", "stale_memory_id"):
        value = _clean_id(action.targets.get(key))
        if value:
            return "memory", value
    return None


def _action_sort_key(action: MemoryConflictResolutionAction) -> tuple[int, int, tuple[str, ...], str]:
    return (
        _ACTION_PRECEDENCE[action.action],
        -int(action.confidence * 1000),
        action.memory_ids,
        action.idempotency_key,
    )


def _normalize_freshness_signals(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        signals: list[dict[str, Any]] = []
        for key, item in value.items():
            if isinstance(item, Mapping):
                data = dict(item)
            else:
                data = {"status": item}
            data.setdefault("memory_id", key)
            data.setdefault("id", key)
            signals.append(data)
        return signals
    signals = []
    for item in value or ():
        data = _object_to_dict(item)
        if "memory_id" not in data and "id" in data:
            data["memory_id"] = data["id"]
        signals.append(data)
    return signals


def _freshness_from_mapping(data: Mapping[str, Any]) -> tuple[FreshnessStatus, float | None]:
    explicit = data.get("source_freshness") or data.get("freshness")
    if isinstance(explicit, Mapping):
        status = _coerce_freshness_status(explicit.get("status") or explicit.get("freshness_status"))
        return status, _coerce_float(explicit.get("confidence"))
    status = _coerce_freshness_status(data.get("status") or data.get("freshness_status") or explicit)
    confidence = _coerce_float(data.get("freshness_confidence") or data.get("confidence"))
    return status, confidence


def _freshness_from_staleness(staleness: float) -> FreshnessStatus:
    score = _clamp(staleness)
    if score >= 0.85:
        return "stale"
    if score >= 0.5:
        return "possibly_stale"
    return "fresh"


def _stronger_freshness(left: FreshnessStatus, right: FreshnessStatus) -> FreshnessStatus:
    ranks = {"unknown": 0, "fresh": 1, "possibly_stale": 2, "stale": 3}
    return right if ranks.get(right, 0) > ranks.get(left, 0) else left


def _fresh_stale_preference(
    memory_ids: Sequence[str],
    memory_map: Mapping[str, MemoryConflictEvidence],
    freshness_by_memory: Mapping[str, Mapping[str, Any]],
) -> str | None:
    stale_ids: list[str] = []
    fresh_ids: list[str] = []
    for memory_id in memory_ids:
        evidence = memory_map.get(memory_id)
        status = evidence.freshness_status if evidence else "unknown"
        signal = freshness_by_memory.get(memory_id)
        if signal is not None:
            status = _coerce_freshness_status(signal.get("status") or signal.get("freshness_status"))
        if status == "stale":
            stale_ids.append(memory_id)
        elif status == "fresh":
            fresh_ids.append(memory_id)
    if stale_ids and fresh_ids:
        return sorted(stale_ids)[0]
    return None


def _has_non_overlapping_windows(
    memory_ids: Sequence[str],
    memory_map: Mapping[str, MemoryConflictEvidence],
) -> bool:
    if len(memory_ids) != 2:
        return False
    left = memory_map.get(memory_ids[0])
    right = memory_map.get(memory_ids[1])
    if left is None or right is None:
        return False
    if left.valid_from is None and left.valid_until is None:
        return False
    if right.valid_from is None and right.valid_until is None:
        return False
    left_start = left.valid_from or datetime.min.replace(tzinfo=timezone.utc)
    left_end = left.valid_until or datetime.max.replace(tzinfo=timezone.utc)
    right_start = right.valid_from or datetime.min.replace(tzinfo=timezone.utc)
    right_end = right.valid_until or datetime.max.replace(tzinfo=timezone.utc)
    return left_end < right_start or right_end < left_start


def _choose_superseded_memory(
    other_ids: Sequence[str],
    preferred_id: str,
    memory_map: Mapping[str, MemoryConflictEvidence],
) -> str:
    if not other_ids:
        return preferred_id
    inactive = [memory_id for memory_id in other_ids if not (memory_map.get(memory_id) or MemoryConflictEvidence(memory_id)).active]
    if inactive:
        return sorted(inactive)[0]
    older_id, _newer_id = _older_newer(other_ids[0], preferred_id, memory_map)
    return older_id or sorted(other_ids)[0]


def _older_newer(
    left_id: str,
    right_id: str,
    memory_map: Mapping[str, MemoryConflictEvidence],
) -> tuple[str | None, str | None]:
    left = memory_map.get(left_id)
    right = memory_map.get(right_id)
    if left is None or right is None or left.sort_time is None or right.sort_time is None:
        return None, None
    if left.sort_time <= right.sort_time:
        return left_id, right_id
    return right_id, left_id


def _preferred_from_evidence(evidence: Mapping[str, Any]) -> str | None:
    for key in ("preferred_memory_id", "resolution_memory_id", "superseding_memory_id", "winner_memory_id"):
        value = _clean_id(evidence.get(key))
        if value:
            return value
    adjudication = evidence.get("adjudication")
    if isinstance(adjudication, Mapping):
        return _clean_id(
            adjudication.get("preferred_memory_id")
            or adjudication.get("superseding_memory_id")
            or adjudication.get("resolution_memory_id")
        ) or None
    return None


def _target_memory_ids(
    action: ResolutionActionName,
    memory_ids: Sequence[str],
    targets: Mapping[str, Any],
) -> tuple[str, ...]:
    if action == "supersede_older":
        return tuple(mid for mid in (_clean_id(targets.get("superseded_memory_id")),) if mid)
    if action == "archive_stale":
        return tuple(mid for mid in (_clean_id(targets.get("stale_memory_id")),) if mid)
    if action == "quarantine_uncertain":
        return tuple(memory_ids)
    return ()


def _rollback_fields(evidence: MemoryConflictEvidence | None) -> dict[str, Any]:
    if evidence is None:
        return {
            "truth_status": None,
            "review_status": None,
            "archived": None,
            "superseded_by": None,
            "valid_until": None,
            "demotion_reason": None,
        }
    raw = evidence.raw
    return {
        "truth_status": raw.get("truth_status", evidence.truth_status),
        "review_status": raw.get("review_status", evidence.review_status),
        "archived": raw.get("archived"),
        "superseded_by": raw.get("superseded_by", evidence.superseded_by),
        "valid_until": raw.get("valid_until", evidence.valid_until),
        "demotion_reason": raw.get("demotion_reason"),
        "freshness_score": raw.get("freshness_score", evidence.freshness_score),
        "staleness_score": raw.get("staleness_score", evidence.staleness_score),
    }


def _deferred_semantic_metadata(reason: str) -> dict[str, Any]:
    return {
        "llm_call": False,
        "deferred_escalations": [
            {
                "kind": "semantic_adjudication",
                "reason": reason,
                "provider_call_permitted": False,
            }
        ],
    }


def _action_impact_score(action: MemoryConflictResolutionAction) -> float:
    base = {
        "supersede_older": 45.0,
        "archive_stale": 32.0,
        "quarantine_uncertain": 38.0,
        "keep_both_with_windows": 22.0,
        "admin_review": 28.0,
    }[action.action]
    return round(base + action.confidence * 20.0, 3)


def _first_scope(action: MemoryConflictResolutionAction, key: str) -> str | None:
    scope = action.metadata.get(key)
    return str(scope) if scope else None


def _subject_key(data: Mapping[str, Any], claim: Mapping[str, Any]) -> str | None:
    subject_ref = claim.get("subject_ref") or data.get("subject_ref")
    if subject_ref:
        subject_type = claim.get("subject_type") or data.get("subject_type") or "subject"
        return f"{str(subject_type).strip().lower()}:{str(subject_ref).strip().lower()}"
    policy_scope = data.get("policy_scope")
    if policy_scope:
        return f"policy:{str(policy_scope).strip().lower()}"
    return None


def _source_id(prefix: str, data: Mapping[str, Any]) -> str:
    explicit = data.get("source_id") or data.get("id")
    if explicit not in (None, ""):
        return f"{prefix}:{explicit}"
    return f"{prefix}:{_stable_digest(data)}"


def _extract_correction_terms(content: str) -> tuple[str, str] | None:
    text = " ".join(str(content or "").split())
    if not text:
        return None
    for pattern in (_CORRECTION_RE, _NOT_CORRECTION_RE):
        match = pattern.search(text)
        if not match:
            continue
        preferred = _clean_term(match.group("new"))
        replaced = _clean_term(match.group("old"))
        if preferred and replaced:
            return preferred, replaced
    return None


def _clean_term(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t\n\r\"'`.,;:!?")[:160]


def _coerce_id_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(_clean_id(item) for item in value if _clean_id(item))
    return (_clean_id(value),) if _clean_id(value) else ()


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(str(item) for item in value if str(item or "").strip())
    return (str(value),) if str(value or "").strip() else ()


def _ordered_ids(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({_clean_id(value) for value in values if _clean_id(value)}))


def _clean_id(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _coerce_freshness_status(value: Any) -> FreshnessStatus:
    status = str(value or "").strip().lower()
    if status in {"fresh", "possibly_stale", "stale", "unknown"}:
        return status  # type: ignore[return-value]
    return "unknown"


def _coerce_datetime(value: Any) -> datetime | None:
    return _shared_coerce_datetime(value)


def _coerce_float(value: Any, default: float | None = None) -> float | None:
    return _shared_coerce_float(value, default=default)


def _clamp(value: float | None, lower: float = 0.0, upper: float = 1.0) -> float:
    if value is None:
        value = 0.0
    return max(lower, min(upper, float(value)))


def _coerce_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _object_to_dict(value: Any) -> dict[str, Any]:
    return _shared_object_to_dict(value)


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(_jsonable(item) for item in value)
    return value


__all__ = [
    "MemoryConflictEvidence",
    "MemoryConflictResolutionAction",
    "MemoryConflictResolutionPlan",
    "ResolutionActionName",
    "resolve_memory_conflicts",
]
