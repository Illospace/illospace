"""Active context compiler policy.

The policy is deliberately conservative and feature-gated.  It can defer or
suppress memory candidates only when there is strong, privacy-safe evidence
that the item is stale or conflicted and the item itself has weak confidence
signals.  Otherwise the caller keeps baseline inclusion.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from brain.kernel.common.coercion import clamp as _shared_clamp
from brain.kernel.common.coercion import coerce_datetime as _shared_coerce_datetime
from brain.kernel.common.coercion import coerce_float as _shared_coerce_float
from brain.kernel.common.coercion import coerce_int as _shared_coerce_int
from brain.kernel.common.coercion import drop_none as _shared_drop_none
from brain.kernel.common.coercion import optional_text as _shared_optional_text
from brain.kernel.common.serialization import jsonable as _shared_jsonable
from brain.systems.memory.conflict_scout import scout_memory_conflicts
from brain.systems.memory.truth_maintenance import build_truth_state

CONTEXT_POLICY_ACTIVE_FLAG = "CONTEXT_POLICY_ACTIVE_ENABLED"
CONTEXT_POLICY_GLOBAL_ACTIVE_FLAG = "LEARNING_POLICY_ACTIVE_CONTEXT_POLICY_ENABLED"
CONTEXT_POLICY_GLOBAL_DISABLED_FLAG = "LEARNING_POLICY_ACTIVE_CONTEXT_POLICY_DISABLED"
CONTEXT_POLICY_VERSION = "active-context-policy-v1"

_POLICY_ACTION_CONFIDENCE_FLOOR = 0.70
_SUPPRESS_CONFIDENCE_FLOOR = 0.78
_LOW_ITEM_CONFIDENCE = 0.45
_LOW_ATTENTION_SCORE = 0.32
_LOW_USEFULNESS_SCORE = 0.18


class ContextPolicyAction(StrEnum):
    """Prompt-placement decision for a context item."""

    INCLUDE_IN_PROMPT = "include_in_prompt"
    LAZY_LOAD_ONLY = "lazy_load_only"
    SUPPRESS = "suppress"
    INCLUDE_CONFLICT_WARNING = "include_conflict_warning"


class MemoryFreshnessStatus(StrEnum):
    """Normalized source freshness signal understood by the policy."""

    FRESH = "fresh"
    POSSIBLY_STALE = "possibly_stale"
    STALE = "stale"
    UNKNOWN = "unknown"


class MemoryTruthStatus(StrEnum):
    """Common truth-maintenance states used by policy decisions."""

    REVIEWED = "reviewed"
    TENTATIVE = "tentative"
    UNKNOWN = "unknown"
    QUARANTINED = "quarantined"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class ContextTaskClass(StrEnum):
    """Broad task classes that may tune future policy behavior."""

    GENERAL = "general"
    FREEFORM = "freeform"
    IMPLEMENT = "implement"
    INVESTIGATE = "investigate"
    REVIEW = "review"
    ENCODE = "encode"
    PR = "pr"
    ISSUE = "issue"
    COMMIT = "commit"
    FILE = "file"
    DOCUMENT = "document"


@dataclass(frozen=True)
class SectionUsefulnessHistory:
    """Past usefulness signal for a context-pack section."""

    section_name: str
    useful_count: int = 0
    unused_count: int = 0
    missed_count: int = 0
    over_budget_count: int = 0
    score: float | None = None

    @property
    def usefulness_score(self) -> float:
        if self.score is not None:
            return _clamp(self.score)
        total = self.useful_count + self.unused_count + self.missed_count
        if total <= 0:
            return 0.5
        weighted = self.useful_count + (self.missed_count * 0.35)
        return _clamp(weighted / total)

    @classmethod
    def from_value(cls, section_name: str, value: Any) -> "SectionUsefulnessHistory":
        if isinstance(value, SectionUsefulnessHistory):
            return value
        if isinstance(value, Mapping):
            return cls(
                section_name=section_name,
                useful_count=_coerce_int(value.get("useful_count") or value.get("useful"), 0),
                unused_count=_coerce_int(value.get("unused_count") or value.get("unused"), 0),
                missed_count=_coerce_int(value.get("missed_count") or value.get("missed"), 0),
                over_budget_count=_coerce_int(value.get("over_budget_count") or value.get("over_budget"), 0),
                score=_coerce_float(
                    value.get("usefulness_score")
                    if value.get("usefulness_score") is not None
                    else value.get("score")
                ),
            )
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            counts: Counter[str] = Counter()
            for item in value:
                if isinstance(item, Mapping):
                    label = str(item.get("label") or "").strip().lower()
                    if label:
                        counts[label] += 1
            return cls(
                section_name=section_name,
                useful_count=counts["useful"],
                unused_count=counts["unused"],
                missed_count=counts["missed"],
                over_budget_count=counts["over_budget"],
            )
        return cls(section_name=section_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_name": self.section_name,
            "useful_count": self.useful_count,
            "unused_count": self.unused_count,
            "missed_count": self.missed_count,
            "over_budget_count": self.over_budget_count,
            "usefulness_score": round(self.usefulness_score, 6),
        }


@dataclass(frozen=True)
class MemoryPolicyInput:
    """Normalized, policy-relevant metadata for one memory candidate."""

    item_id: str
    item_digest: str
    content_digest: str | None
    rank: int
    memory_id: str | None = None
    memory_type: str | None = None
    tier: str | None = None
    freshness_status: MemoryFreshnessStatus = MemoryFreshnessStatus.UNKNOWN
    freshness_confidence: float = 0.0
    freshness_score: float | None = None
    staleness_score: float | None = None
    truth_status: str = MemoryTruthStatus.UNKNOWN.value
    truth_confidence: float = 0.5
    has_open_contradiction: bool = False
    open_contradiction_count: int = 0
    attention_score: float | None = None
    prior_usefulness_score: float | None = None
    item_confidence: float = 0.5
    recency_days: float | None = None
    task_class: str = ContextTaskClass.GENERAL.value
    source: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_memory(
        cls,
        memory: Mapping[str, Any],
        *,
        index: int,
        task_class: str,
        now: datetime,
        attention_override: Mapping[str, Any] | None = None,
    ) -> "MemoryPolicyInput":
        data = dict(memory)
        attention_override = attention_override if isinstance(attention_override, Mapping) else {}
        memory_id = _text(data.get("id")) or _text(data.get("memory_id"))
        memory_type = _text(data.get("type")) or _text(data.get("memory_type"))
        tier = _text(data.get("tier")) or _text(data.get("memory_tier"))
        content_digest = (
            _text(data.get("content_digest"))
            or _text(data.get("claim_digest"))
            or _text(data.get("source_digest"))
            or _stable_text_digest(data.get("content"))
        )
        identity = {
            "id": memory_id,
            "type": memory_type,
            "tier": tier,
            "source": _text(data.get("source")),
            "source_ref": _text(data.get("source_ref")),
            "content_digest": content_digest,
        }
        item_digest = _stable_context_item_digest("memory", identity)
        item_id = f"memory:{memory_id or item_digest}"
        freshness_status, freshness_confidence, freshness_score, staleness_score = _freshness_signals(data)
        truth_state = _truth_state(data)
        attention_score = _first_float(
            data.get("attention_score"),
            data.get("_attention_score"),
            _nested(data, "scores", "attention"),
            attention_override.get("attention_score"),
        )
        prior_usefulness_score = _first_float(
            data.get("prior_usefulness_score"),
            data.get("usefulness_score"),
            _nested(data, "scores", "prior_usefulness"),
            attention_override.get("prior_usefulness_score"),
        )
        item_confidence = _first_float(
            data.get("confidence"),
            data.get("harvest_confidence"),
            data.get("label_confidence"),
            truth_state.get("confidence"),
            default=0.5,
        )

        return cls(
            item_id=item_id,
            item_digest=item_digest,
            content_digest=content_digest,
            rank=index,
            memory_id=memory_id,
            memory_type=memory_type,
            tier=tier,
            freshness_status=freshness_status,
            freshness_confidence=_clamp(freshness_confidence),
            freshness_score=_clamp_or_none(freshness_score),
            staleness_score=_clamp_or_none(staleness_score),
            truth_status=str(truth_state.get("truth_status") or data.get("truth_status") or "unknown").strip().lower()
            or "unknown",
            truth_confidence=_clamp(_coerce_float(truth_state.get("confidence"), 0.5) or 0.5),
            has_open_contradiction=bool(truth_state.get("has_open_contradiction")),
            open_contradiction_count=_coerce_int(truth_state.get("open_contradiction_count"), 0),
            attention_score=_clamp_or_none(attention_score),
            prior_usefulness_score=_clamp_or_none(prior_usefulness_score),
            item_confidence=_clamp(item_confidence or 0.5),
            recency_days=_recency_days(data, now=now),
            task_class=task_class,
            source=_text(data.get("source")),
            raw=data,
        )

    def safe_ref(self) -> dict[str, Any]:
        return _drop_none({
            "item_id": self.item_id,
            "item_digest": self.item_digest,
            "content_digest": self.content_digest,
            "memory_id": self.memory_id,
            "rank": self.rank,
        })


@dataclass(frozen=True)
class ContextPolicyDecisionRecord:
    """A privacy-safe decision record for a context item."""

    target_type: str
    item_id: str
    item_digest: str | None
    action: ContextPolicyAction
    confidence: float
    reasons: tuple[str, ...] = ()
    fallback_action: ContextPolicyAction | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _drop_none({
            "target_type": self.target_type,
            "item_id": self.item_id,
            "item_digest": self.item_digest,
            "action": self.action.value,
            "confidence": round(_clamp(self.confidence), 6),
            "reasons": list(self.reasons),
            "fallback_action": self.fallback_action.value if self.fallback_action else None,
            "metadata": _jsonable(self.metadata),
        })


@dataclass(frozen=True)
class ActiveContextPolicyApplication:
    """Policy output plus materialized memory placement."""

    enabled: bool
    policy_version: str
    task_class: str
    decisions: tuple[ContextPolicyDecisionRecord, ...]
    included_memories: tuple[Mapping[str, Any], ...] = field(default_factory=tuple, repr=False, compare=False)
    lazy_load_memories: tuple[Mapping[str, Any], ...] = field(default_factory=tuple, repr=False, compare=False)
    suppressed_memories: tuple[Mapping[str, Any], ...] = field(default_factory=tuple, repr=False, compare=False)
    conflict_scout: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    section_usefulness_history: tuple[SectionUsefulnessHistory, ...] = field(default_factory=tuple)
    fallback_used: bool = False
    fallback_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        counts = Counter(decision.action.value for decision in self.decisions)
        return {
            "enabled": self.enabled,
            "policy_version": self.policy_version,
            "task_class": self.task_class,
            "fallback_used": self.fallback_used,
            "fallback_reasons": list(self.fallback_reasons),
            "decision_counts": {
                action.value: counts.get(action.value, 0)
                for action in ContextPolicyAction
            },
            "decisions": [decision.to_dict() for decision in self.decisions],
            "lazy_load_items": [
                decision.to_dict()
                for decision in self.decisions
                if decision.action == ContextPolicyAction.LAZY_LOAD_ONLY
            ],
            "suppressed_items": [
                decision.to_dict()
                for decision in self.decisions
                if decision.action == ContextPolicyAction.SUPPRESS
            ],
            "conflict_warnings": [
                decision.to_dict()
                for decision in self.decisions
                if decision.action == ContextPolicyAction.INCLUDE_CONFLICT_WARNING
            ],
            "conflict_scout": [_jsonable(dict(notice)) for notice in self.conflict_scout],
            "section_usefulness_history": [history.to_dict() for history in self.section_usefulness_history],
        }


def context_policy_active_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the active context policy should affect prompt assembly."""
    source = env if env is not None else os.environ
    local_enabled = str(source.get(CONTEXT_POLICY_ACTIVE_FLAG, "0")).strip().lower() in {"1", "true", "yes", "on"}
    return bool(local_enabled and _learning_policy_context_enabled(source))


def _learning_policy_context_enabled(env: Mapping[str, str]) -> bool:
    try:
        from brain.systems.learning.policy import build_learning_policy

        return build_learning_policy(env=env).active_context_policy_enabled
    except Exception:
        return True


def context_policy_runtime_hints() -> dict[str, Any]:
    """Return compact versioned thresholds for hot-path runtime hints."""
    return {
        "policy_version": CONTEXT_POLICY_VERSION,
        "active_flag": CONTEXT_POLICY_ACTIVE_FLAG,
        "thresholds": {
            "action_confidence_floor": _POLICY_ACTION_CONFIDENCE_FLOOR,
            "suppress_confidence_floor": _SUPPRESS_CONFIDENCE_FLOOR,
            "low_item_confidence": _LOW_ITEM_CONFIDENCE,
            "low_attention_score": _LOW_ATTENTION_SCORE,
            "low_usefulness_score": _LOW_USEFULNESS_SCORE,
        },
    }


def normalize_task_class(value: Any) -> str:
    """Normalize caller task-class hints without rejecting future classes."""
    text = str(value or "").strip().lower().replace(" ", "_")
    if not text:
        return ContextTaskClass.GENERAL.value
    aliases = {
        "debug": ContextTaskClass.INVESTIGATE.value,
        "audit": ContextTaskClass.REVIEW.value,
        "memory_write": ContextTaskClass.ENCODE.value,
    }
    return aliases.get(text, text)


def apply_active_context_policy(
    *,
    memories: Sequence[Mapping[str, Any]] | None,
    task_class: str | ContextTaskClass | None = None,
    attention_decision: Mapping[str, Any] | None = None,
    attention_explain: Mapping[str, Any] | None = None,
    section_usefulness_history: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    conflict_scout: Sequence[Any] | None = None,
    enabled: bool | None = None,
    now: datetime | None = None,
) -> ActiveContextPolicyApplication:
    """Apply active policy to already-selected memory candidates.

    Disabled mode returns baseline inclusion for every memory.  Active mode may
    move items to lazy-load or suppression only when the decision clears the
    confidence floor; otherwise it emits an inclusion fallback decision.
    """
    active = context_policy_active_enabled() if enabled is None else bool(enabled)
    memory_list = [memory for memory in (memories or []) if isinstance(memory, Mapping)]
    normalized_task_class = normalize_task_class(task_class)
    clock = _coerce_datetime(now) or datetime.now(timezone.utc)
    histories = _section_histories(section_usefulness_history)
    selected_memory_history = histories.get("selected_memories", SectionUsefulnessHistory("selected_memories"))
    attention_overrides = _attention_overrides_by_id(attention_decision, attention_explain)
    inputs = tuple(
        MemoryPolicyInput.from_memory(
            memory,
            index=index,
            task_class=normalized_task_class,
            now=clock,
            attention_override=attention_overrides.get(_memory_key(memory), {}),
        )
        for index, memory in enumerate(memory_list, start=1)
    )

    if not active:
        decisions = tuple(
            _include_decision(
                item,
                reasons=("feature_flag_disabled",),
                confidence=1.0,
            )
            for item in inputs
        )
        return ActiveContextPolicyApplication(
            enabled=False,
            policy_version=CONTEXT_POLICY_VERSION,
            task_class=normalized_task_class,
            decisions=decisions,
            included_memories=tuple(memory_list),
            section_usefulness_history=tuple(histories.values()),
        )

    notices = tuple(_safe_notice_dict(notice) for notice in _policy_conflict_notices(
        memory_list,
        inputs=inputs,
        conflict_scout=conflict_scout,
    ))
    conflicts_by_id = _conflicts_by_memory_id(notices)

    decisions: list[ContextPolicyDecisionRecord] = []
    included: list[Mapping[str, Any]] = []
    lazy_load: list[Mapping[str, Any]] = []
    suppressed: list[Mapping[str, Any]] = []
    fallback_reasons: list[str] = []

    for memory, item in zip(memory_list, inputs):
        decision = _decide_memory(
            item,
            conflicts=conflicts_by_id.get(item.memory_id or "", ()),
            selected_memory_history=selected_memory_history,
        )
        decisions.append(decision)
        if decision.fallback_action is not None:
            fallback_reasons.append(f"{item.item_id}:{decision.fallback_action.value}_below_confidence_floor")

        if decision.action == ContextPolicyAction.SUPPRESS:
            suppressed.append(memory)
        elif decision.action == ContextPolicyAction.LAZY_LOAD_ONLY:
            lazy_load.append(memory)
        else:
            included.append(memory)

    return ActiveContextPolicyApplication(
        enabled=True,
        policy_version=CONTEXT_POLICY_VERSION,
        task_class=normalized_task_class,
        decisions=tuple(decisions),
        included_memories=tuple(included),
        lazy_load_memories=tuple(lazy_load),
        suppressed_memories=tuple(suppressed),
        conflict_scout=notices,
        section_usefulness_history=tuple(histories.values()),
        fallback_used=bool(fallback_reasons),
        fallback_reasons=tuple(fallback_reasons),
    )


def _decide_memory(
    item: MemoryPolicyInput,
    *,
    conflicts: Sequence[Mapping[str, Any]],
    selected_memory_history: SectionUsefulnessHistory,
) -> ContextPolicyDecisionRecord:
    reasons: list[str] = []
    stale_confidence = _stale_confidence(item)
    possibly_stale_confidence = (
        item.freshness_confidence
        if item.freshness_status == MemoryFreshnessStatus.POSSIBLY_STALE
        else 0.0
    )
    inactive_truth_confidence = _inactive_truth_confidence(item)
    conflict_state = _conflict_state(item, conflicts)
    low_reliability = _is_low_reliability(item)
    weak_section_history = (
        selected_memory_history.usefulness_score <= 0.25
        and selected_memory_history.unused_count >= max(2, selected_memory_history.useful_count + 1)
    )

    strong_bad_confidence = max(
        stale_confidence,
        inactive_truth_confidence,
        conflict_state["non_preferred_confidence"],
    )
    stale_or_conflicted = bool(strong_bad_confidence > 0.0)

    if conflict_state["non_preferred_confidence"] > 0:
        reasons.append("conflict_scout_non_preferred")
    if conflict_state["advisory_confidence"] > 0:
        reasons.append("conflict_scout_warning")
    if stale_confidence > 0:
        reasons.append("source_freshness_stale")
    if inactive_truth_confidence > 0:
        reasons.append("truth_status_inactive_or_open_contradiction")
    if low_reliability:
        reasons.append("low_item_confidence")

    if stale_or_conflicted and low_reliability:
        confidence = _clamp(strong_bad_confidence)
        if confidence >= _SUPPRESS_CONFIDENCE_FLOOR:
            return _decision(
                item,
                ContextPolicyAction.SUPPRESS,
                confidence=confidence,
                reasons=tuple(reasons + ["suppressed_only_after_strong_stale_or_conflict_evidence"]),
                conflicts=conflicts,
                selected_memory_history=selected_memory_history,
            )
        return _fallback_include(
            item,
            attempted_action=ContextPolicyAction.SUPPRESS,
            confidence=confidence,
            reasons=tuple(reasons + ["policy_confidence_below_suppression_floor"]),
            conflicts=conflicts,
            selected_memory_history=selected_memory_history,
        )

    if (
        item.freshness_status == MemoryFreshnessStatus.POSSIBLY_STALE
        and low_reliability
        and weak_section_history
    ):
        confidence = _clamp(max(possibly_stale_confidence, selected_memory_history.unused_count / 10.0))
        reasons = reasons + ["source_freshness_possibly_stale", "section_usefulness_history_weak"]
        if confidence >= _POLICY_ACTION_CONFIDENCE_FLOOR:
            return _decision(
                item,
                ContextPolicyAction.LAZY_LOAD_ONLY,
                confidence=confidence,
                reasons=tuple(reasons),
                conflicts=conflicts,
                selected_memory_history=selected_memory_history,
            )
        return _fallback_include(
            item,
            attempted_action=ContextPolicyAction.LAZY_LOAD_ONLY,
            confidence=confidence,
            reasons=tuple(reasons + ["policy_confidence_below_lazy_load_floor"]),
            conflicts=conflicts,
            selected_memory_history=selected_memory_history,
        )

    warning_confidence = max(
        conflict_state["advisory_confidence"],
        conflict_state["non_preferred_confidence"],
        stale_confidence,
        inactive_truth_confidence,
    )
    if warning_confidence > 0:
        return _decision(
            item,
            ContextPolicyAction.INCLUDE_CONFLICT_WARNING,
            confidence=max(0.50, warning_confidence),
            reasons=tuple(reasons or ["included_with_context_policy_warning"]),
            conflicts=conflicts,
            selected_memory_history=selected_memory_history,
        )

    return _include_decision(
        item,
        reasons=("baseline_inclusion",),
        confidence=1.0,
        selected_memory_history=selected_memory_history,
    )


def _include_decision(
    item: MemoryPolicyInput,
    *,
    reasons: Sequence[str],
    confidence: float,
    selected_memory_history: SectionUsefulnessHistory | None = None,
) -> ContextPolicyDecisionRecord:
    return _decision(
        item,
        ContextPolicyAction.INCLUDE_IN_PROMPT,
        confidence=confidence,
        reasons=tuple(reasons),
        selected_memory_history=selected_memory_history,
    )


def _fallback_include(
    item: MemoryPolicyInput,
    *,
    attempted_action: ContextPolicyAction,
    confidence: float,
    reasons: Sequence[str],
    conflicts: Sequence[Mapping[str, Any]],
    selected_memory_history: SectionUsefulnessHistory,
) -> ContextPolicyDecisionRecord:
    return _decision(
        item,
        ContextPolicyAction.INCLUDE_IN_PROMPT,
        confidence=confidence,
        reasons=tuple(reasons),
        fallback_action=attempted_action,
        conflicts=conflicts,
        selected_memory_history=selected_memory_history,
    )


def _decision(
    item: MemoryPolicyInput,
    action: ContextPolicyAction,
    *,
    confidence: float,
    reasons: Sequence[str],
    fallback_action: ContextPolicyAction | None = None,
    conflicts: Sequence[Mapping[str, Any]] = (),
    selected_memory_history: SectionUsefulnessHistory | None = None,
) -> ContextPolicyDecisionRecord:
    metadata: dict[str, Any] = {
        "memory_id": item.memory_id,
        "rank": item.rank,
        "memory_type": item.memory_type,
        "tier": item.tier,
        "freshness_status": item.freshness_status.value,
        "freshness_confidence": round(_clamp(item.freshness_confidence), 6),
        "freshness_score": _round_or_none(item.freshness_score),
        "staleness_score": _round_or_none(item.staleness_score),
        "truth_status": item.truth_status,
        "truth_confidence": round(_clamp(item.truth_confidence), 6),
        "has_open_contradiction": item.has_open_contradiction,
        "open_contradiction_count": item.open_contradiction_count,
        "attention_score": _round_or_none(item.attention_score),
        "prior_usefulness_score": _round_or_none(item.prior_usefulness_score),
        "item_confidence": round(_clamp(item.item_confidence), 6),
        "recency_days": _round_or_none(item.recency_days),
        "task_class": item.task_class,
        "conflict_ids": [notice.get("conflict_ids") for notice in conflicts if notice.get("conflict_ids")],
        "section_usefulness_score": (
            round(selected_memory_history.usefulness_score, 6)
            if selected_memory_history is not None
            else None
        ),
    }
    return ContextPolicyDecisionRecord(
        target_type="memory",
        item_id=item.item_id,
        item_digest=item.item_digest,
        action=action,
        confidence=_clamp(confidence),
        reasons=tuple(reason for reason in reasons if reason),
        fallback_action=fallback_action,
        metadata=_drop_none(metadata),
    )


def _policy_conflict_notices(
    memories: Sequence[Mapping[str, Any]],
    *,
    inputs: Sequence[MemoryPolicyInput],
    conflict_scout: Sequence[Any] | None,
) -> tuple[Any, ...]:
    if conflict_scout is not None:
        return tuple(conflict_scout)
    freshness = {
        item.memory_id: item.freshness_status.value
        for item in inputs
        if item.memory_id
    }
    try:
        return tuple(scout_memory_conflicts(memories, freshness=freshness))
    except Exception:
        return ()


def _conflicts_by_memory_id(notices: Sequence[Mapping[str, Any]]) -> dict[str, tuple[Mapping[str, Any], ...]]:
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for notice in notices:
        for memory_id in notice.get("conflict_ids") or []:
            key = str(memory_id)
            buckets.setdefault(key, []).append(notice)
    return {key: tuple(value) for key, value in buckets.items()}


def _conflict_state(item: MemoryPolicyInput, conflicts: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    non_preferred = 0.0
    advisory = 0.0
    memory_id = str(item.memory_id or "")
    for notice in conflicts:
        confidence = _clamp(_coerce_float(notice.get("confidence"), 0.0) or 0.0)
        action = str(notice.get("recommended_action") or "").strip().lower()
        preferred = str(notice.get("preferred_memory_id") or "")
        if action == "include_neither":
            non_preferred = max(non_preferred, confidence)
        elif action == "include_one" and preferred and preferred != memory_id:
            non_preferred = max(non_preferred, confidence)
        elif action in {"include_one", "include_both_with_warning", "defer"}:
            advisory = max(advisory, confidence)
    return {
        "non_preferred_confidence": non_preferred,
        "advisory_confidence": advisory,
    }


def _stale_confidence(item: MemoryPolicyInput) -> float:
    if item.freshness_status == MemoryFreshnessStatus.STALE:
        return _clamp(item.freshness_confidence)
    if item.staleness_score is not None and item.staleness_score >= 0.85:
        return max(0.80, _clamp(item.freshness_confidence))
    return 0.0


def _inactive_truth_confidence(item: MemoryPolicyInput) -> float:
    inactive_statuses = {
        MemoryTruthStatus.QUARANTINED.value,
        MemoryTruthStatus.EXPIRED.value,
        MemoryTruthStatus.SUPERSEDED.value,
        MemoryTruthStatus.ARCHIVED.value,
    }
    if item.truth_status in inactive_statuses:
        return max(0.88, item.truth_confidence)
    if item.has_open_contradiction:
        return max(0.78, item.truth_confidence)
    return 0.0


def _is_low_reliability(item: MemoryPolicyInput) -> bool:
    low_item_confidence = item.item_confidence <= _LOW_ITEM_CONFIDENCE
    low_attention = item.attention_score is not None and item.attention_score <= _LOW_ATTENTION_SCORE
    low_usefulness = (
        item.prior_usefulness_score is not None
        and item.prior_usefulness_score <= _LOW_USEFULNESS_SCORE
    )
    return low_item_confidence or low_attention or low_usefulness


def _section_histories(
    value: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
) -> dict[str, SectionUsefulnessHistory]:
    if value is None:
        return {"selected_memories": SectionUsefulnessHistory("selected_memories")}
    if isinstance(value, Mapping):
        if "selected_memories" in value:
            return {
                str(key): SectionUsefulnessHistory.from_value(str(key), section_value)
                for key, section_value in value.items()
            }
        section_name = str(value.get("section_name") or value.get("section") or "selected_memories")
        return {section_name: SectionUsefulnessHistory.from_value(section_name, value)}
    return {"selected_memories": SectionUsefulnessHistory.from_value("selected_memories", value)}


def _attention_overrides_by_id(
    attention_decision: Mapping[str, Any] | None,
    attention_explain: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    overrides: dict[str, Mapping[str, Any]] = {}
    for source in (attention_decision, attention_explain):
        if not isinstance(source, Mapping):
            continue
        candidates = _nested(source, "debug", "candidates")
        if candidates is None:
            candidates = _nested(source, "decision", "debug", "candidates")
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
            continue
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            item_id = candidate.get("item_id")
            if item_id is None:
                continue
            overrides[str(item_id)] = candidate
    return overrides


def _safe_notice_dict(notice: Any) -> Mapping[str, Any]:
    if hasattr(notice, "to_dict") and callable(notice.to_dict):
        try:
            notice = notice.to_dict()
        except Exception:
            notice = {}
    if not isinstance(notice, Mapping):
        return {}
    return _drop_none({
        "conflict_ids": [str(item) for item in (notice.get("conflict_ids") or [])],
        "severity": _text(notice.get("severity")),
        "recommended_action": _text(notice.get("recommended_action")),
        "reasons": [str(reason) for reason in (notice.get("reasons") or [])],
        "confidence": _round_or_none(_coerce_float(notice.get("confidence"))),
        "preferred_memory_id": _text(notice.get("preferred_memory_id")),
        "preferred_reason": _text(notice.get("preferred_reason")),
    })


def _freshness_signals(
    data: Mapping[str, Any],
) -> tuple[MemoryFreshnessStatus, float, float | None, float | None]:
    source = data.get("source_freshness") or data.get("freshness")
    source_map = source if isinstance(source, Mapping) else {}
    explicit_status = source_map.get("status") or source_map.get("freshness_status")
    if explicit_status is None and isinstance(source, str):
        explicit_status = source
    status = _coerce_freshness_status(
        explicit_status
        or data.get("freshness_status")
        or data.get("source_freshness_status")
    )
    freshness_score = _first_float(
        source_map.get("score"),
        source_map.get("freshness_score"),
        data.get("freshness_score"),
    )
    staleness_score = _first_float(
        source_map.get("staleness_score"),
        data.get("staleness_score"),
    )
    if freshness_score is None and staleness_score is not None:
        freshness_score = 1.0 - staleness_score
    if staleness_score is None and freshness_score is not None:
        staleness_score = 1.0 - freshness_score

    confidence = _first_float(source_map.get("confidence"), data.get("freshness_confidence"))
    if confidence is None:
        if staleness_score is not None:
            confidence = 0.82 if staleness_score >= 0.85 else 0.65 if staleness_score >= 0.50 else 0.35
        elif status == MemoryFreshnessStatus.STALE:
            confidence = 0.70
        elif status == MemoryFreshnessStatus.POSSIBLY_STALE:
            confidence = 0.55
        elif status == MemoryFreshnessStatus.FRESH:
            confidence = 0.35
        else:
            confidence = 0.0

    if status == MemoryFreshnessStatus.UNKNOWN and staleness_score is not None:
        if staleness_score >= 0.85:
            status = MemoryFreshnessStatus.STALE
        elif staleness_score >= 0.50:
            status = MemoryFreshnessStatus.POSSIBLY_STALE
        else:
            status = MemoryFreshnessStatus.FRESH

    return status, _clamp(confidence), freshness_score, staleness_score


def _coerce_freshness_status(value: Any) -> MemoryFreshnessStatus:
    text = str(value or "").strip().lower()
    if text in {status.value for status in MemoryFreshnessStatus}:
        return MemoryFreshnessStatus(text)
    return MemoryFreshnessStatus.UNKNOWN


def _truth_state(data: Mapping[str, Any]) -> dict[str, Any]:
    try:
        state = build_truth_state(data)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _memory_key(memory: Mapping[str, Any]) -> str:
    value = memory.get("id") or memory.get("memory_id")
    return str(value) if value is not None else ""


def _recency_days(data: Mapping[str, Any], *, now: datetime) -> float | None:
    for key in ("last_accessed", "observed_at", "created_at", "valid_from"):
        timestamp = _coerce_datetime(data.get(key))
        if timestamp is None:
            continue
        return max(0.0, (now - timestamp).total_seconds() / 86_400)
    return None


def _stable_context_item_digest(kind: str, payload: Mapping[str, Any], *, length: int = 24) -> str:
    basis = {
        "schema_version": 1,
        "kind": str(kind or "context_item"),
        "payload": _jsonable(payload),
    }
    raw = json.dumps(basis, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _stable_text_digest(value: Any, *, length: int = 64) -> str | None:
    text = _text(value)
    if not text:
        return None
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _coerce_datetime(value: Any) -> datetime | None:
    return _shared_coerce_datetime(value)


def _nested(value: Mapping[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first_float(*values: Any, default: float | None = None) -> float | None:
    for value in values:
        coerced = _coerce_float(value)
        if coerced is not None:
            return coerced
    return default


def _coerce_float(value: Any, default: float | None = None) -> float | None:
    return _shared_coerce_float(value, default=default)


def _coerce_int(value: Any, default: int = 0) -> int:
    return _shared_coerce_int(value, default=default)


def _clamp(value: float | int | None) -> float:
    return _shared_clamp(value)


def _clamp_or_none(value: float | int | None) -> float | None:
    if value is None:
        return None
    return _clamp(value)


def _round_or_none(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _text(value: Any) -> str | None:
    return _shared_optional_text(value)


def _drop_none(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _shared_drop_none(payload)


def _jsonable(value: Any) -> Any:
    return _shared_jsonable(value, enum_values=True)


__all__ = [
    "ActiveContextPolicyApplication",
    "CONTEXT_POLICY_ACTIVE_FLAG",
    "CONTEXT_POLICY_GLOBAL_ACTIVE_FLAG",
    "CONTEXT_POLICY_GLOBAL_DISABLED_FLAG",
    "CONTEXT_POLICY_VERSION",
    "ContextPolicyAction",
    "ContextPolicyDecisionRecord",
    "ContextTaskClass",
    "MemoryFreshnessStatus",
    "MemoryPolicyInput",
    "MemoryTruthStatus",
    "SectionUsefulnessHistory",
    "apply_active_context_policy",
    "context_policy_active_enabled",
    "context_policy_runtime_hints",
    "normalize_task_class",
]
