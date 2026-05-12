"""Truth-maintenance helpers for memories.

The first PR-11 slice keeps this layer additive and conservative:

- normalize sparse/legacy memory truth metadata on read
- record structured contradiction and review rows
- optionally filter quarantined/expired memories when a feature flag is on
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text

from brain.kernel.common.coercion import coerce_datetime as _shared_coerce_datetime
from brain.kernel.common.coercion import coerce_float as _shared_coerce_float
from brain.kernel.common.coercion import coerce_int as _shared_coerce_int
from brain.kernel.common.coercion import object_to_dict as _shared_object_to_dict

_TRUTH_QUARANTINE_FLAG = "MEMORY_QUARANTINE_FILTER_ENABLED"
_TRUTH_LLM_ADJUDICATION_FLAG = "MEMORY_TRUTH_LLM_ADJUDICATION_ENABLED"
_TRUTH_ADJUDICATION_MODEL = "MEMORY_TRUTH_ADJUDICATION_MODEL"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_RESOLVED_CONTRADICTION_STATUSES = {"resolved", "closed", "dismissed", "accepted"}
_ACTIVE_TIER_ORDER = {"policy": 0, "procedural": 1, "semantic": 2, "episodic": 3}
_PROMOTION_MIN_SUPPORT = {
    ("episodic", "semantic"): 3,
    ("semantic", "procedural"): 2,
    ("procedural", "policy"): 3,
}
_HIGH_CONFIDENCE_CONTRADICTION = 0.78
_REVIEW_CONFIDENCE_FLOOR = 0.1
_PROMOTION_ADJUDICATION_FLOOR = 0.7
_CORRECTION_RE = re.compile(
    r"(?i)\b(?:actually\s+)?use\s+(?P<new>.+?)\s+instead\s+of\s+(?P<old>.+?)(?:[.!?]|$)"
)
_NOT_CORRECTION_RE = re.compile(
    r"(?i)\buse\s+(?P<new>.+?),?\s+not\s+(?P<old>.+?)(?:[.!?]|$)"
)

logger = logging.getLogger(__name__)

TruthRelation = Literal["supports", "duplicates", "contradicts", "supersedes", "unrelated", "uncertain"]
TruthAction = Literal[
    "none",
    "needs_review",
    "record",
    "quarantine_candidate",
    "quarantine_existing",
    "supersede_existing",
]


class TruthAdjudicationPayload(BaseModel):
    """Strict LLM contract for semantic memory truth adjudication."""

    relation: TruthRelation
    action: TruthAction = "none"
    confidence: float = Field(ge=0.0, le=1.0)
    severity: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=500)
    evidence: list[str] = Field(default_factory=list, max_length=4)

    model_config = ConfigDict(extra="forbid")

    @field_validator("rationale")
    @classmethod
    def _strip_rationale(cls, value: str) -> str:
        cleaned = " ".join(str(value).split())
        if not cleaned:
            raise ValueError("rationale cannot be empty")
        return cleaned[:500]

    @field_validator("evidence")
    @classmethod
    def _strip_evidence(cls, value: list[Any]) -> list[str]:
        snippets: list[str] = []
        for item in value or []:
            snippet = " ".join(str(item).split())
            if snippet:
                snippets.append(snippet[:300])
            if len(snippets) >= 4:
                break
        return snippets


@dataclass(frozen=True)
class CorrectionTerms:
    """Small deterministic correction cue used only to choose adjudication candidates."""

    preferred: str
    replaced: str


def _truth_flag_enabled(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def quarantine_filter_enabled() -> bool:
    """Return whether retrieval should suppress quarantined memories."""
    return _truth_flag_enabled(_TRUTH_QUARANTINE_FLAG, default=True)


def llm_adjudication_enabled() -> bool:
    """Return whether provider-backed semantic adjudication should be attempted."""
    if not _learning_policy_night_llm_adjudication_enabled():
        return False
    if os.getenv(_TRUTH_ADJUDICATION_MODEL):
        return True
    return _truth_flag_enabled(_TRUTH_LLM_ADJUDICATION_FLAG, default=False)


def _object_to_dict(value: Any) -> dict[str, Any]:
    return _shared_object_to_dict(value)


def _learning_policy_night_llm_adjudication_enabled() -> bool:
    try:
        from brain.systems.learning.policy import build_learning_policy_from_env

        return build_learning_policy_from_env().night_llm_adjudication_enabled
    except Exception:
        return True


def _coerce_datetime(value: Any) -> datetime | None:
    return _shared_coerce_datetime(value)


def _coerce_float(value: Any, default: float | None = None) -> float | None:
    return _shared_coerce_float(value, default=default)


def _coerce_int(value: Any, default: int = 0) -> int:
    return _shared_coerce_int(value, default=default)


def _coerce_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _jsonable_evidence(value: Any) -> Any:
    value = _coerce_jsonish(value)
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    if value is None:
        return {}
    return {"value": value}


def _evidence_present(value: Any) -> bool:
    value = _coerce_jsonish(value)
    if isinstance(value, Mapping):
        return any(v not in (None, "", [], {}) for v in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value) > 0
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def validate_truth_action_context(
    *,
    action: str,
    evidence: Any,
    confidence: float | None,
) -> dict[str, Any]:
    """Validate evidence/confidence required for active truth-state changes."""
    action_name = str(action or "review").strip().lower()
    normalized_confidence = _coerce_float(confidence)
    if normalized_confidence is None:
        raise ValueError(f"{action_name} requires confidence")
    normalized_confidence = max(0.0, min(1.0, normalized_confidence))
    if normalized_confidence < _REVIEW_CONFIDENCE_FLOOR:
        raise ValueError(f"{action_name} confidence is too low")
    if not _evidence_present(evidence):
        raise ValueError(f"{action_name} requires evidence")
    evidence_payload = _jsonable_evidence(evidence)
    if isinstance(evidence_payload, dict):
        evidence_payload = dict(evidence_payload)
        evidence_payload.setdefault("confidence", normalized_confidence)
    else:
        evidence_payload = {
            "items": evidence_payload,
            "confidence": normalized_confidence,
        }
    return {
        "action": action_name,
        "confidence": normalized_confidence,
        "evidence": evidence_payload,
    }


def _extract_correction_terms(content: str) -> CorrectionTerms | None:
    text_value = " ".join(str(content or "").split())
    if not text_value:
        return None
    for pattern in (_CORRECTION_RE, _NOT_CORRECTION_RE):
        match = pattern.search(text_value)
        if not match:
            continue
        preferred = _clean_correction_term(match.group("new"))
        replaced = _clean_correction_term(match.group("old"))
        if preferred and replaced:
            return CorrectionTerms(preferred=preferred, replaced=replaced)
    return None


def _clean_correction_term(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" \t\n\r\"'`.,;:!?")
    return cleaned[:160]


def _infer_source_type(data: dict[str, Any]) -> str:
    source = data.get("source")
    if source:
        return str(source)
    if data.get("source_type"):
        return str(data["source_type"])
    if str(data.get("memory_tier") or "").strip().lower() == "policy":
        return "policy"
    return "direct"


def _infer_source_kind(data: dict[str, Any]) -> str:
    explicit = data.get("source_kind")
    if explicit:
        return str(explicit)
    source_type = data.get("source_type")
    if source_type:
        return str(source_type)
    return _infer_source_type(data)


def _infer_source_ref(data: dict[str, Any]) -> str | None:
    if data.get("source_ref"):
        return str(data["source_ref"])
    source_session = data.get("source_session")
    if source_session:
        return str(source_session)
    source_memory_ids = data.get("source_memory_ids")
    if isinstance(source_memory_ids, Sequence) and not isinstance(source_memory_ids, (str, bytes)):
        if source_memory_ids:
            return ",".join(str(item) for item in source_memory_ids)
    return None


def _infer_policy_kind(data: dict[str, Any]) -> str | None:
    explicit = data.get("policy_kind")
    if explicit:
        return str(explicit)
    if str(data.get("memory_tier") or "").strip().lower() == "policy":
        source_type = str(data.get("source_type") or "").strip().lower()
        if source_type and source_type != "direct":
            return source_type
        return "runtime"
    return None


def _infer_policy_scope(data: dict[str, Any]) -> str | None:
    explicit = data.get("policy_scope")
    if explicit:
        return str(explicit)
    if str(data.get("memory_tier") or "").strip().lower() == "policy":
        scope = data.get("scope") or data.get("source_ref")
        if scope:
            return str(scope)
    return None


def _infer_truth_status(data: dict[str, Any]) -> str:
    explicit = data.get("truth_status")
    if explicit:
        return str(explicit)

    review_status = str(data.get("review_status") or "").strip().lower()
    if data.get("demoted_at") or review_status in {"rejected", "quarantined"}:
        return "quarantined"

    valid_until = _coerce_datetime(data.get("valid_until"))
    if valid_until is not None and valid_until < datetime.now(timezone.utc):
        return "expired"

    if data.get("superseded_by") is not None:
        return "superseded"

    if data.get("reviewed_at") is not None or review_status == "reviewed" or data.get("promoted_at") is not None:
        return "reviewed"

    if data.get("consolidated") or str(data.get("memory_tier") or "").lower() in {"semantic", "procedural", "policy"}:
        return "tentative"

    return "unknown"


def _infer_review_status(data: dict[str, Any]) -> str:
    explicit = data.get("review_status")
    if explicit:
        return str(explicit)
    if data.get("reviewed_at") is not None or data.get("promoted_at") is not None or data.get("consolidated"):
        return "reviewed"
    return "unreviewed"


def _infer_confidence(data: dict[str, Any]) -> float:
    explicit = _coerce_float(data.get("confidence"))
    if explicit is not None:
        return max(0.0, min(1.0, explicit))

    harvest_confidence = _coerce_float(data.get("harvest_confidence"))
    if harvest_confidence is not None:
        return max(0.0, min(1.0, harvest_confidence))

    if data.get("reviewed_at") is not None or data.get("promoted_at") is not None or data.get("consolidated"):
        return 0.6

    return 0.5


def _infer_freshness_score(data: dict[str, Any]) -> float:
    explicit = _coerce_float(data.get("freshness_score"))
    if explicit is not None:
        return max(0.0, min(1.0, explicit))

    timestamp = _coerce_datetime(data.get("last_accessed") or data.get("created_at"))
    if timestamp is None:
        return 0.5

    age_days = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds() / 86_400)
    return max(0.0, min(1.0, 1.0 / (1.0 + (age_days / 30.0))))


def _infer_staleness_score(data: dict[str, Any], *, freshness_score: float | None = None) -> float:
    explicit = _coerce_float(data.get("staleness_score"))
    if explicit is not None:
        return max(0.0, min(1.0, explicit))
    freshness = freshness_score if freshness_score is not None else _infer_freshness_score(data)
    return max(0.0, min(1.0, 1.0 - freshness))


def _infer_subject_ref(data: dict[str, Any]) -> str | None:
    explicit = data.get("subject_ref")
    if explicit:
        return str(explicit)
    for key in ("file_path", "path", "worktree_path", "commit_sha", "context_pack_digest"):
        value = data.get(key)
        if value:
            return str(value)
    return None


def normalize_memory_claim_metadata(value: Any) -> dict[str, Any]:
    """Return temporal/source metadata for a memory claim without mutating storage.

    Legacy rows often only have ``created_at`` plus ``source_type``/``source_ref``.
    This helper treats those as conservative read-level defaults while preserving
    explicit temporal claim fields when newer writers provide them.
    """
    data = _object_to_dict(value)
    if not data:
        return {
            "observed_at": None,
            "valid_from": None,
            "valid_until": None,
            "source_kind": "direct",
            "source_ref": None,
            "source_digest": None,
            "subject_type": None,
            "subject_ref": None,
            "staleness_score": 0.5,
        }

    valid_from = _coerce_datetime(data.get("valid_from") or data.get("created_at") or data.get("promoted_at"))
    freshness_score = _infer_freshness_score(data)
    return {
        "observed_at": _coerce_datetime(data.get("observed_at") or valid_from),
        "valid_from": valid_from,
        "valid_until": _coerce_datetime(data.get("valid_until")),
        "source_kind": _infer_source_kind(data),
        "source_ref": _infer_source_ref(data),
        "source_digest": str(data["source_digest"]) if data.get("source_digest") else None,
        "subject_type": str(data["subject_type"]) if data.get("subject_type") else None,
        "subject_ref": _infer_subject_ref(data),
        "staleness_score": _infer_staleness_score(data, freshness_score=freshness_score),
    }


def _memory_tier(value: Any) -> str:
    data = _object_to_dict(value)
    return str(data.get("memory_tier") or "episodic").strip().lower()


def can_promote_memory(
    *,
    from_tier: str,
    to_tier: str,
    confidence: float | None = None,
    evidence: Any = None,
    support_count: int = 0,
    reviewed: bool = False,
    adjudication: Any = None,
    policy_kind: str | None = None,
    policy_scope: str | None = None,
    open_contradiction_count: int = 0,
) -> tuple[bool, str]:
    """Return whether a tier promotion is safe enough to apply."""
    from_tier = str(from_tier or "episodic").strip().lower()
    to_tier = str(to_tier or from_tier).strip().lower()
    support_count = max(0, int(support_count))
    open_contradiction_count = max(0, int(open_contradiction_count))

    if from_tier == to_tier:
        return True, "same tier"

    try:
        action_context = validate_truth_action_context(
            action="promote",
            evidence=evidence,
            confidence=confidence,
        )
    except ValueError as exc:
        return False, str(exc)

    if (from_tier, to_tier) not in _PROMOTION_MIN_SUPPORT:
        return False, f"unsupported promotion path {from_tier}->{to_tier}"

    min_support = _PROMOTION_MIN_SUPPORT[(from_tier, to_tier)]
    if support_count < min_support:
        return False, f"needs at least {min_support} supporting memories"

    if open_contradiction_count > 0 and not reviewed:
        return False, "open contradictions block promotion"
    if open_contradiction_count > 0 and action_context["confidence"] < _HIGH_CONFIDENCE_CONTRADICTION:
        return False, "promoting contradicted memory requires high-confidence human review"

    adjudication_state = normalize_truth_adjudication(adjudication)
    if not reviewed and not adjudication_state["promotion_safe"]:
        return False, "automated promotions require semantic adjudication or human review"

    if to_tier == "policy":
        if not policy_kind or not policy_scope:
            return False, "policy promotions require explicit kind and scope"
        if not reviewed and support_count < 4:
            return False, "policy promotions require review or stronger multi-source evidence"

    return True, "promotion permitted"


def build_promotion_truth_fields(
    *,
    source_kind: str,
    source_ref: str | None,
    target_tier: str,
    confidence: float,
    evidence: Any = None,
    support_count: int,
    reviewed_by: str | None = None,
    adjudication: Any = None,
    policy_kind: str | None = None,
    policy_scope: str | None = None,
    open_contradiction_count: int = 0,
) -> dict[str, Any]:
    """Return conservative truth fields for tier promotion."""
    target_tier = str(target_tier or "episodic").strip().lower()
    support_count = max(0, int(support_count))
    reviewed = reviewed_by is not None
    confidence = max(0.0, min(1.0, confidence))
    promotion_evidence = evidence or {
        "source_kind": source_kind,
        "source_ref": source_ref,
        "support_count": support_count,
    }
    min_support = _PROMOTION_MIN_SUPPORT.get(({
        "semantic": "episodic",
        "procedural": "semantic",
        "policy": "procedural",
    }.get(target_tier, "episodic"), target_tier), 1)

    safe_to_promote, _reason = can_promote_memory(
        from_tier={
            "semantic": "episodic",
            "procedural": "semantic",
            "policy": "procedural",
        }.get(target_tier, "episodic"),
        to_tier=target_tier,
        confidence=confidence,
        evidence=promotion_evidence,
        support_count=support_count,
        reviewed=reviewed,
        adjudication=adjudication,
        policy_kind=policy_kind,
        policy_scope=policy_scope,
        open_contradiction_count=open_contradiction_count,
    )

    truth_status = "reviewed" if safe_to_promote and reviewed and target_tier == "policy" else "tentative"
    review_status = "reviewed" if safe_to_promote and reviewed else "adjudicated" if safe_to_promote else "unreviewed"

    now = datetime.now(timezone.utc)
    fields: dict[str, Any] = {
        "truth_status": truth_status,
        "review_status": review_status,
        "confidence": confidence if safe_to_promote else min(confidence, 0.7),
        "freshness_score": 1.0 if support_count >= min_support else 0.9,
        "staleness_score": 0.0 if support_count >= min_support else 0.1,
        "source_type": source_kind,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "observed_at": now,
        "valid_from": now,
        "valid_until": None,
        "reviewed_at": now if safe_to_promote and reviewed else None,
        "reviewed_by": reviewed_by if safe_to_promote and reviewed else None,
        "demoted_at": None,
        "demotion_reason": None,
        "policy_kind": policy_kind if target_tier == "policy" else None,
        "policy_scope": policy_scope if target_tier == "policy" else None,
    }
    return fields


def build_policy_truth_fields(
    *,
    source_kind: str,
    source_ref: str | None,
    confidence: float,
    evidence: Any = None,
    support_count: int,
    policy_kind: str,
    policy_scope: str,
    reviewed_by: str | None = None,
    adjudication: Any = None,
    open_contradiction_count: int = 0,
) -> dict[str, Any]:
    """Return conservative truth fields for a policy-tier memory."""
    return build_promotion_truth_fields(
        source_kind=source_kind,
        source_ref=source_ref,
        target_tier="policy",
        confidence=confidence,
        evidence=evidence,
        support_count=support_count,
        reviewed_by=reviewed_by,
        adjudication=adjudication,
        policy_kind=policy_kind,
        policy_scope=policy_scope,
        open_contradiction_count=open_contradiction_count,
    )


def build_demotion_truth_fields(
    *,
    reason: str,
    confidence: float | None,
    evidence: Any,
    reviewed_by: str | None = None,
    quarantine: bool = True,
) -> dict[str, Any]:
    """Return fields for a demoted or quarantined memory."""
    action_context = validate_truth_action_context(
        action="quarantine" if quarantine else "demote",
        evidence=evidence,
        confidence=confidence,
    )
    now = datetime.now(timezone.utc)
    truth_status = "quarantined" if quarantine else "reviewed"
    review_status = "rejected" if quarantine else "reviewed"
    return {
        "truth_status": truth_status,
        "review_status": review_status,
        "confidence": 0.0 if quarantine else min(0.4, action_context["confidence"]),
        "freshness_score": 0.0,
        "staleness_score": 1.0,
        "source_type": "review",
        "source_kind": "review",
        "source_ref": reason,
        "observed_at": now,
        "valid_from": now,
        "valid_until": now if quarantine else None,
        "policy_kind": None,
        "policy_scope": None,
        "reviewed_at": now,
        "reviewed_by": reviewed_by,
        "demoted_at": now,
        "demotion_reason": reason,
    }


def memory_retrieval_priority(value: Any) -> tuple[int, int, int, float, float]:
    """Return a stable sort key that prefers reviewed, active memories."""
    state = build_truth_state(value)
    active_rank = 0 if state["is_reviewed_active"] else 1 if state["is_active"] else 3
    contradiction_rank = 0 if state["contradiction_status"] == "none" else 1 if state["contradiction_status"] == "resolved" else 2
    tier_rank = _ACTIVE_TIER_ORDER.get(str(state["memory_tier"]).strip().lower(), 4)
    return (
        active_rank,
        contradiction_rank,
        tier_rank,
        -float(state["confidence"] or 0.0),
        -float(state["freshness_score"] or 0.0),
    )


def memory_retrieval_bonus(value: Any) -> float:
    """Return a conservative score adjustment for retrieval ranking."""
    state = build_truth_state(value)
    bonus = 0.0
    if state["is_reviewed_active"]:
        bonus += 0.12
    elif state["is_active"]:
        bonus += 0.04

    tier = str(state["memory_tier"]).strip().lower()
    if tier == "policy":
        bonus += 0.08 if state["is_policy_effective"] else -0.06
    elif tier == "procedural":
        bonus += 0.04 if state["is_reviewed_active"] else 0.02
    elif tier == "semantic":
        bonus += 0.02 if state["is_reviewed_active"] else 0.0

    if state["has_open_contradiction"]:
        bonus -= min(0.16, 0.05 * max(1, int(state["open_contradiction_count"])))

    if state["is_quarantined"] or state["is_expired"] or state["is_superseded"]:
        bonus -= 0.35

    bonus += max(-0.05, min(0.05, (float(state["confidence"] or 0.5) - 0.5) * 0.1))
    bonus += max(-0.03, min(0.03, (float(state["freshness_score"] or 0.5) - 0.5) * 0.06))
    return round(bonus, 3)


def normalize_truth_adjudication(value: Any) -> dict[str, Any]:
    """Normalize an LLM or deterministic truth-adjudication decision."""
    if isinstance(value, TruthAdjudicationPayload):
        data = value.model_dump()
    elif isinstance(value, Mapping):
        try:
            data = TruthAdjudicationPayload.model_validate(dict(value)).model_dump()
        except ValidationError:
            raw_evidence = value.get("evidence") or []
            if isinstance(raw_evidence, str):
                raw_evidence = [raw_evidence]
            data = {
                "relation": str(value.get("relation") or "uncertain").strip().lower(),
                "action": str(value.get("action") or "needs_review").strip().lower(),
                "confidence": _coerce_float(value.get("confidence"), 0.0) or 0.0,
                "severity": _coerce_float(value.get("severity"), 0.0) or 0.0,
                "rationale": str(value.get("rationale") or "Invalid adjudication payload"),
                "evidence": list(raw_evidence),
            }
    else:
        data = {
            "relation": "uncertain",
            "action": "none",
            "confidence": 0.0,
            "severity": 0.0,
            "rationale": "No adjudication available",
            "evidence": [],
        }

    relation = str(data.get("relation") or "uncertain").strip().lower()
    if relation not in {"supports", "duplicates", "contradicts", "supersedes", "unrelated", "uncertain"}:
        relation = "uncertain"
    action = str(data.get("action") or "none").strip().lower()
    if action not in {"none", "needs_review", "record", "quarantine_candidate", "quarantine_existing", "supersede_existing"}:
        action = "needs_review"
    confidence = max(0.0, min(1.0, _coerce_float(data.get("confidence"), 0.0) or 0.0))
    severity = max(0.0, min(1.0, _coerce_float(data.get("severity"), 0.0) or 0.0))
    is_conflict = relation in {"contradicts", "supersedes"}
    high_confidence = bool(is_conflict and confidence >= _HIGH_CONFIDENCE_CONTRADICTION and severity >= 0.6)
    promotion_safe = bool(
        relation in {"supports", "duplicates", "unrelated"}
        and action == "none"
        and confidence >= _PROMOTION_ADJUDICATION_FLOOR
    )
    return {
        "relation": relation,
        "action": action,
        "confidence": confidence,
        "severity": severity,
        "rationale": str(data.get("rationale") or "")[:500],
        "evidence": list(data.get("evidence") or [])[:4],
        "is_conflict": is_conflict,
        "is_high_confidence_conflict": high_confidence,
        "promotion_safe": promotion_safe,
    }


def adjudicate_memory_pair(
    *,
    candidate_content: str,
    existing_content: str,
    candidate_evidence: Any = None,
    candidate_confidence: float | None = None,
    model: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Compare two memory claims and return a semantic truth decision.

    Provider-backed adjudication is attempted only when explicitly configured.
    Otherwise we fail closed into deterministic correction cues and
    needs-review decisions rather than treating keyword heuristics as truth.
    """
    if _learning_policy_night_llm_adjudication_enabled() and (model or llm_adjudication_enabled()):
        try:
            decision = _call_truth_adjudicator(
                candidate_content=candidate_content,
                existing_content=existing_content,
                candidate_evidence=candidate_evidence,
                candidate_confidence=candidate_confidence,
                model=model,
                user_id=user_id,
                org_id=org_id,
            )
            if decision:
                return normalize_truth_adjudication(decision)
        except Exception:
            logger.debug("Memory truth adjudicator unavailable; falling back to conservative cues", exc_info=True)
    return normalize_truth_adjudication(
        _deterministic_truth_adjudication(
            candidate_content=candidate_content,
            existing_content=existing_content,
            candidate_confidence=candidate_confidence,
        )
    )


def _deterministic_truth_adjudication(
    *,
    candidate_content: str,
    existing_content: str,
    candidate_confidence: float | None,
) -> dict[str, Any]:
    confidence = max(0.0, min(1.0, _coerce_float(candidate_confidence, 0.5) or 0.5))
    correction = _extract_correction_terms(candidate_content)
    existing_lower = str(existing_content or "").lower()
    if correction and correction.replaced.lower() in existing_lower:
        return {
            "relation": "supersedes",
            "action": "supersede_existing",
            "confidence": max(confidence, 0.86),
            "severity": 0.9,
            "rationale": "Candidate explicitly corrects an older memory using an instead-of cue.",
            "evidence": [
                str(candidate_content)[:300],
                str(existing_content)[:300],
            ],
        }

    candidate_lower = str(candidate_content or "").lower()
    if " not " in candidate_lower and _overlap_ratio(candidate_lower, existing_lower) >= 0.45:
        return {
            "relation": "contradicts",
            "action": "needs_review",
            "confidence": min(max(confidence, 0.55), 0.72),
            "severity": 0.55,
            "rationale": "Candidate contains a negation near an overlapping claim; human review is safer than hiding either memory.",
            "evidence": [str(candidate_content)[:300], str(existing_content)[:300]],
        }

    return {
        "relation": "uncertain",
        "action": "none",
        "confidence": min(confidence, 0.4),
        "severity": 0.0,
        "rationale": "No configured LLM adjudicator and no strong deterministic correction cue.",
        "evidence": [],
    }


def _overlap_ratio(left: str, right: str) -> float:
    left_terms = {term for term in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", left.lower())}
    right_terms = {term for term in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", right.lower())}
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / max(1, min(len(left_terms), len(right_terms)))


def _call_truth_adjudicator(
    *,
    candidate_content: str,
    existing_content: str,
    candidate_evidence: Any,
    candidate_confidence: float | None,
    model: str | None,
    user_id: str | None,
    org_id: str | None,
) -> dict[str, Any] | None:
    configured_model = model or os.getenv(_TRUTH_ADJUDICATION_MODEL)
    if not configured_model:
        return None

    from brain.platform.integrations.llm import resolve_llm_client
    from brain.platform.integrations.providers import LLMRequest, get_provider
    from brain.platform.providers.model_policy import infer_provider_from_model, resolve_default_provider

    requested_provider = infer_provider_from_model(
        configured_model,
        default=resolve_default_provider(user_id=user_id, org_id=org_id),
    )
    llm = resolve_llm_client(user_id=user_id, org_id=org_id, provider=requested_provider)
    provider = get_provider(llm.provider, llm.client)
    response_format = _truth_response_format() if llm.provider == "openai" else None
    response = provider.create(
        LLMRequest(
            model=configured_model,
            max_output_tokens=700,
            messages=[
                {
                    "role": "user",
                    "content": _truth_user_prompt(
                        candidate_content=candidate_content,
                        existing_content=existing_content,
                        candidate_evidence=candidate_evidence,
                        candidate_confidence=candidate_confidence,
                    ),
                }
            ],
            system=_truth_system_prompt(),
            reasoning_effort="low",
            extra_headers=llm.build_request_headers(session_id="memory-truth-adjudication") or None,
            response_format=response_format,
            operation_type="memory_extraction",
        )
    )
    raw_text = _response_text(response)
    if not raw_text:
        return None
    return json.loads(_extract_json_text(raw_text))


def _truth_system_prompt() -> str:
    return (
        "You are Illo's memory truth-maintenance adjudicator. "
        "Compare one candidate memory against one existing memory. "
        "Decide whether the candidate supports, duplicates, contradicts, supersedes, is unrelated to, "
        "or is uncertain against the existing memory. Return strict JSON only. "
        "Never output chain-of-thought. Only recommend quarantine/supersession for direct, high-confidence conflicts."
    )


def _truth_user_prompt(
    *,
    candidate_content: str,
    existing_content: str,
    candidate_evidence: Any,
    candidate_confidence: float | None,
) -> str:
    payload = {
        "candidate": {
            "content": candidate_content,
            "confidence": candidate_confidence,
            "evidence": _jsonable_evidence(candidate_evidence),
        },
        "existing": {"content": existing_content},
    }
    return (
        "Adjudicate these memory claims.\n\n"
        "Allowed relation values: supports, duplicates, contradicts, supersedes, unrelated, uncertain.\n"
        "Allowed action values: none, needs_review, record, quarantine_candidate, quarantine_existing, supersede_existing.\n"
        "Use supersedes only when the candidate is a correction or replacement for the existing memory.\n"
        "Use needs_review for weak or ambiguous conflicts.\n\n"
        f"INPUT_JSON:\n{json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)}"
    )


def _truth_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "MemoryTruthAdjudication",
        "strict": True,
        "schema": _strict_json_schema(TruthAdjudicationPayload.model_json_schema()),
    }


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(schema)

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return
        for key in ("$defs", "definitions", "properties"):
            child = node.get(key)
            if isinstance(child, dict):
                for value in child.values():
                    _walk(value)
        if "items" in node:
            _walk(node["items"])
        for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
            if key in node:
                _walk(node[key])
        if node.get("type") == "object" or "properties" in node:
            properties = node.get("properties")
            node["required"] = list(properties.keys()) if isinstance(properties, dict) else []
            node["additionalProperties"] = False

    _walk(normalized)
    return normalized


def _response_text(response: Any) -> str | None:
    text_parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            text_parts.append(str(block.text))
    text_value = "\n".join(part.strip() for part in text_parts if part and part.strip()).strip()
    return text_value or None


def _extract_json_text(raw: str) -> str:
    text_value = str(raw or "").strip()
    if text_value.startswith("```"):
        lines = text_value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    start = text_value.find("{")
    end = text_value.rfind("}")
    if start >= 0 and end > start:
        return text_value[start:end + 1]
    return text_value


def find_truth_maintenance_candidates(
    session,
    *,
    memory_id: int,
    content: str,
    user_id: str | None = None,
    org_id: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Find a small set of memory candidates for semantic truth adjudication."""
    dialect = _dialect_name(session)
    if dialect in {None, "sqlite"}:
        return []

    from brain.platform.db.repositories.memory_visibility import MemoryVisibilityContext, memory_visibility_sql

    params: dict[str, Any] = {"memory_id": memory_id, "limit": max(1, int(limit))}
    visibility_context = MemoryVisibilityContext(
        user_id=user_id,
        org_id=org_id,
        allow_global=(user_id == "system"),
    )
    vis_clause, vis_params = memory_visibility_sql(visibility_context, alias="m")
    params.update(vis_params)

    correction = _extract_correction_terms(content)
    if correction:
        params["replaced_pattern"] = f"%{correction.replaced}%"
        candidate_filter = "AND m.content ILIKE :replaced_pattern"
        order_clause = "m.confidence DESC NULLS LAST, m.created_at DESC"
    else:
        candidate_filter = """
          AND candidate.semantic_embedding IS NOT NULL
          AND m.semantic_embedding IS NOT NULL
          AND 1 - (m.semantic_embedding <=> candidate.semantic_embedding) > 0.72
        """
        order_clause = "1 - (m.semantic_embedding <=> candidate.semantic_embedding) DESC"

    rows = session.execute(
        text(f"""
            WITH candidate AS (
                SELECT semantic_embedding
                FROM memories
                WHERE id = :memory_id
            )
            SELECT
                m.id,
                m.content,
                COALESCE(m.truth_status, 'unknown') AS truth_status,
                COALESCE(m.review_status, 'unreviewed') AS review_status,
                COALESCE(m.confidence, 0.5) AS confidence,
                COALESCE(m.memory_tier, 'episodic') AS memory_tier
            FROM memories m, candidate
            WHERE m.id != :memory_id
              AND COALESCE(m.archived, FALSE) = FALSE
              AND m.superseded_by IS NULL
              AND COALESCE(m.truth_status, 'unknown') NOT IN ('quarantined', 'expired', 'superseded')
              AND COALESCE(m.review_status, 'unreviewed') != 'rejected'
              AND m.demoted_at IS NULL
              {candidate_filter}
              {vis_clause}
            ORDER BY {order_clause}
            LIMIT :limit
        """),
        params,
    ).mappings().all()
    return [dict(row) for row in rows]


def apply_truth_adjudication(
    session,
    *,
    candidate_memory_id: int,
    existing_memory_id: int,
    adjudication: Any,
    candidate_confidence: float | None,
    candidate_evidence: Any,
    reviewer_id: str | None = None,
) -> dict[str, Any]:
    """Record an adjudication and quarantine/supersede high-confidence conflicts."""
    decision = normalize_truth_adjudication(adjudication)
    if not decision["is_conflict"]:
        return {"recorded": False, "decision": decision, "action_taken": "none"}

    confidence = max(0.0, min(1.0, _coerce_float(candidate_confidence, decision["confidence"]) or decision["confidence"]))
    evidence_payload = {
        "adjudication": decision,
        "candidate_evidence": _jsonable_evidence(candidate_evidence),
        "confidence": confidence,
    }
    status = "open" if decision["is_high_confidence_conflict"] else "needs_review"
    contradiction = record_contradiction(
        session,
        left_memory_id=candidate_memory_id,
        right_memory_id=existing_memory_id,
        contradiction_type="semantic_supersession" if decision["relation"] == "supersedes" else "semantic_conflict",
        detected_by="truth_maintenance.adjudication",
        evidence=evidence_payload,
        severity=max(decision["severity"], confidence if decision["is_high_confidence_conflict"] else min(confidence, 0.6)),
        confidence=confidence,
        status=status,
    )

    if not decision["is_high_confidence_conflict"]:
        return {
            "recorded": True,
            "decision": decision,
            "contradiction": contradiction,
            "action_taken": "recorded_for_review",
        }

    action = decision["action"]
    if decision["relation"] == "contradicts" and action in {"none", "record", "needs_review"}:
        action = "quarantine_candidate"
    if action == "supersede_existing":
        _supersede_memory(
            session,
            superseded_memory_id=existing_memory_id,
            superseding_memory_id=candidate_memory_id,
            reason=decision["rationale"] or "high-confidence semantic supersession",
            confidence=confidence,
            evidence=evidence_payload,
            reviewer_id=reviewer_id,
        )
        _mark_candidate_reviewed(
            session,
            candidate_memory_id=candidate_memory_id,
            confidence=confidence,
            reviewer_id=reviewer_id,
        )
        action_taken = "superseded_existing"
    elif action == "quarantine_existing":
        _quarantine_memory(
            session,
            memory_id=existing_memory_id,
            reason=decision["rationale"] or "high-confidence semantic contradiction",
            confidence=confidence,
            evidence=evidence_payload,
            reviewer_id=reviewer_id,
        )
        action_taken = "quarantined_existing"
    elif action == "quarantine_candidate":
        _quarantine_memory(
            session,
            memory_id=candidate_memory_id,
            reason=decision["rationale"] or "high-confidence semantic contradiction",
            confidence=confidence,
            evidence=evidence_payload,
            reviewer_id=reviewer_id,
        )
        action_taken = "quarantined_candidate"
    else:
        _mark_memory_dependents_stale(
            session,
            [candidate_memory_id, existing_memory_id],
            decision["rationale"] or "source memory contradicted",
        )
        action_taken = "recorded_open"

    return {
        "recorded": True,
        "decision": decision,
        "contradiction": contradiction,
        "action_taken": action_taken,
    }


def apply_active_truth_maintenance(
    session,
    *,
    memory_id: int,
    content: str,
    evidence: Any,
    confidence: float | None,
    user_id: str | None = None,
    org_id: str | None = None,
    limit: int = 5,
    model: str | None = None,
) -> dict[str, Any]:
    """Actively adjudicate a new memory against likely conflicts."""
    candidates = find_truth_maintenance_candidates(
        session,
        memory_id=memory_id,
        content=content,
        user_id=user_id,
        org_id=org_id,
        limit=limit,
    )
    stats: dict[str, Any] = {
        "candidate_count": len(candidates),
        "records": 0,
        "actions": [],
        "contradiction_record_ids": [],
    }
    for candidate in candidates:
        decision = adjudicate_memory_pair(
            candidate_content=content,
            existing_content=str(candidate.get("content") or ""),
            candidate_evidence=evidence,
            candidate_confidence=confidence,
            model=model,
            user_id=user_id,
            org_id=org_id,
        )
        result = apply_truth_adjudication(
            session,
            candidate_memory_id=memory_id,
            existing_memory_id=int(candidate["id"]),
            adjudication=decision,
            candidate_confidence=confidence,
            candidate_evidence=evidence,
            reviewer_id=user_id,
        )
        if result.get("recorded"):
            stats["records"] += 1
            stats["actions"].append(result.get("action_taken"))
            record_id = (result.get("contradiction") or {}).get("id")
            if record_id is not None:
                stats["contradiction_record_ids"].append(record_id)
    return stats


def _supersede_memory(
    session,
    *,
    superseded_memory_id: int,
    superseding_memory_id: int,
    reason: str,
    confidence: float,
    evidence: Any,
    reviewer_id: str | None,
) -> None:
    now = datetime.now(timezone.utc)
    session.execute(
        text("""
            UPDATE memories
            SET truth_status = 'superseded',
                review_status = 'rejected',
                superseded_by = :superseding_memory_id,
                valid_until = COALESCE(valid_until, :now),
                demotion_reason = :reason
            WHERE id = :superseded_memory_id
        """),
        {
            "superseded_memory_id": superseded_memory_id,
            "superseding_memory_id": superseding_memory_id,
            "now": now,
            "reason": reason,
        },
    )
    record_memory_review(
        session,
        memory_id=superseded_memory_id,
        action="quarantine",
        from_tier="unknown",
        to_tier="unknown",
        reviewer_id=reviewer_id,
        rationale=reason,
        evidence=evidence,
        confidence=confidence,
    )
    _mark_memory_dependents_stale(
        session,
        [superseded_memory_id],
        reason or "source memory superseded",
    )


def _quarantine_memory(
    session,
    *,
    memory_id: int,
    reason: str,
    confidence: float,
    evidence: Any,
    reviewer_id: str | None,
) -> None:
    fields = build_demotion_truth_fields(
        reason=reason,
        confidence=confidence,
        evidence=evidence,
        reviewed_by=reviewer_id,
        quarantine=True,
    )
    session.execute(
        text("""
            UPDATE memories
            SET truth_status = :truth_status,
                review_status = :review_status,
                confidence = :confidence,
                freshness_score = :freshness_score,
                source_type = :source_type,
                source_ref = :source_ref,
                valid_until = :valid_until,
                reviewed_at = :reviewed_at,
                reviewed_by = :reviewed_by,
                demoted_at = :demoted_at,
                demotion_reason = :demotion_reason
            WHERE id = :memory_id
        """),
        {"memory_id": memory_id, **fields},
    )
    record_memory_review(
        session,
        memory_id=memory_id,
        action="quarantine",
        from_tier="unknown",
        to_tier="unknown",
        reviewer_id=reviewer_id,
        rationale=reason,
        evidence=evidence,
        confidence=confidence,
    )
    _mark_memory_dependents_stale(
        session,
        [memory_id],
        reason or "source memory quarantined",
    )


def _mark_memory_dependents_stale(
    session,
    memory_ids: Sequence[int],
    reason: str,
) -> dict[str, int]:
    """Invalidate summaries/narratives that cite changed source memories."""
    try:
        from brain.platform.db.repositories.memory_dag import MemorySummaryRepository
        from brain.platform.db.repositories.narratives import NarrativeRepository

        return {
            "summaries": MemorySummaryRepository(session).mark_stale_for_memories(memory_ids, reason),
            "narratives": NarrativeRepository(session).mark_stale_for_memories(memory_ids, reason),
        }
    except Exception:
        logger.warning("Failed to mark memory dependents stale", exc_info=True)
        return {"summaries": 0, "narratives": 0}


def _mark_candidate_reviewed(
    session,
    *,
    candidate_memory_id: int,
    confidence: float,
    reviewer_id: str | None,
) -> None:
    session.execute(
        text("""
            UPDATE memories
            SET truth_status = CASE
                    WHEN COALESCE(truth_status, 'unknown') IN ('unknown', 'tentative') THEN 'reviewed'
                    ELSE truth_status
                END,
                review_status = 'reviewed',
                confidence = GREATEST(COALESCE(confidence, 0.5), :confidence),
                reviewed_at = COALESCE(reviewed_at, :reviewed_at),
                reviewed_by = COALESCE(reviewed_by, :reviewed_by),
                valid_from = COALESCE(valid_from, :reviewed_at)
            WHERE id = :candidate_memory_id
        """),
        {
            "candidate_memory_id": candidate_memory_id,
            "confidence": confidence,
            "reviewed_at": datetime.now(timezone.utc),
            "reviewed_by": reviewer_id,
        },
    )


def _dialect_name(session) -> str | None:
    try:
        bind = session.get_bind()
        return bind.dialect.name
    except Exception:
        bind = getattr(session, "bind", None)
        return getattr(getattr(bind, "dialect", None), "name", None)


def resolve_contradiction(
    session,
    *,
    contradiction_id: int,
    resolution_memory_id: int | None = None,
    resolved_by: str | None = None,
    status: str = "resolved",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark a contradiction as resolved without losing the original record."""
    payload = {
        "contradiction_id": contradiction_id,
        "status": status,
        "resolution_memory_id": resolution_memory_id,
        "resolved_by": resolved_by,
        "resolved_at": datetime.now(timezone.utc),
        "evidence": json.dumps(evidence or {}, default=str),
    }
    result = session.execute(
        text(
            """
            UPDATE memory_contradictions
            SET status = :status,
                resolution_memory_id = COALESCE(:resolution_memory_id, resolution_memory_id),
                resolved_by = COALESCE(:resolved_by, resolved_by),
                resolved_at = COALESCE(resolved_at, :resolved_at),
                evidence = COALESCE(CAST(:evidence AS jsonb), evidence)
            WHERE id = :contradiction_id
            RETURNING id, left_memory_id, right_memory_id, detected_by, contradiction_type,
                      evidence, severity, status, resolution_memory_id, created_at, resolved_at, resolved_by
            """
        ),
        payload,
    )
    row = result.mappings().first()
    if row is None:
        return {"id": contradiction_id, **payload}
    return dict(row)


def normalize_memory_truth_data(value: Any) -> dict[str, Any]:
    """Return a conservative truth payload for a memory-like object."""
    data = _object_to_dict(value)
    if not data:
        return {
            "memory_tier": "episodic",
            "truth_status": "unknown",
            "review_status": "unreviewed",
            "confidence": 0.5,
            "freshness_score": 0.5,
            "staleness_score": 0.5,
            "source_type": "direct",
            "source_kind": "direct",
            "source_ref": None,
            "source_digest": None,
            "subject_type": None,
            "subject_ref": None,
            "observed_at": None,
            "valid_from": None,
            "valid_until": None,
            "policy_kind": None,
            "policy_scope": None,
            "reviewed_at": None,
            "reviewed_by": None,
            "demoted_at": None,
            "demotion_reason": None,
            "open_contradiction_count": 0,
            "resolved_contradiction_count": 0,
            "contradiction_status": "none",
        }

    data["memory_tier"] = str(data.get("memory_tier") or "episodic")
    data["truth_status"] = _infer_truth_status(data)
    data["review_status"] = _infer_review_status(data)
    data["confidence"] = _infer_confidence(data)
    data["freshness_score"] = _infer_freshness_score(data)
    data["staleness_score"] = _infer_staleness_score(data, freshness_score=data["freshness_score"])
    data["source_type"] = _infer_source_type(data)
    data.update(normalize_memory_claim_metadata(data))
    data["reviewed_at"] = _coerce_datetime(data.get("reviewed_at"))
    data["demoted_at"] = _coerce_datetime(data.get("demoted_at"))
    data["policy_kind"] = _infer_policy_kind(data)
    data["policy_scope"] = _infer_policy_scope(data)
    data["demotion_reason"] = data.get("demotion_reason")
    data["open_contradiction_count"] = _coerce_int(data.get("open_contradiction_count"), 0)
    data["resolved_contradiction_count"] = _coerce_int(data.get("resolved_contradiction_count"), 0)
    data["contradiction_status"] = str(
        data.get("contradiction_status")
        or ("open" if data["open_contradiction_count"] > 0 else "resolved" if data["resolved_contradiction_count"] > 0 else "none")
    )
    return data


def build_truth_state(value: Any) -> dict[str, Any]:
    """Build a normalized, read-only truth state for debug surfaces."""
    data = normalize_memory_truth_data(value)
    now = datetime.now(timezone.utc)
    valid_until = data.get("valid_until")
    expired = bool(valid_until and valid_until < now)
    archived = bool(data.get("archived"))
    truth_status = str(data.get("truth_status") or "").strip().lower()
    review_status = str(data.get("review_status") or "").strip().lower()
    quarantined = truth_status == "quarantined" or review_status == "rejected" or data.get("demoted_at") is not None
    superseded = truth_status == "superseded" or data.get("superseded_by") is not None
    active = not (expired or quarantined or superseded or archived)
    open_contradiction_count = _coerce_int(data.get("open_contradiction_count"), 0)
    resolved_contradiction_count = _coerce_int(data.get("resolved_contradiction_count"), 0)
    contradiction_status = str(
        data.get("contradiction_status")
        or ("open" if open_contradiction_count > 0 else "resolved" if resolved_contradiction_count > 0 else "none")
    )
    reviewed_active = bool(active and review_status == "reviewed" and contradiction_status != "open")
    policy_effective = bool(
        reviewed_active
        and str(data.get("memory_tier") or "").strip().lower() == "policy"
        and data.get("policy_kind")
        and data.get("policy_scope")
    )
    return {
        **data,
        "is_active": active,
        "is_quarantined": quarantined,
        "is_expired": expired,
        "is_superseded": superseded,
        "is_archived": archived,
        "has_open_contradiction": contradiction_status == "open",
        "is_reviewed_active": reviewed_active,
        "is_policy_effective": policy_effective,
        "open_contradiction_count": open_contradiction_count,
        "resolved_contradiction_count": resolved_contradiction_count,
        "contradiction_status": contradiction_status,
    }


def normalize_contradiction_data(value: Any) -> dict[str, Any]:
    """Normalize a contradiction row for API responses."""
    data = _object_to_dict(value)
    data["evidence"] = _coerce_jsonish(data.get("evidence")) or {}
    severity = _coerce_float(data.get("severity"), 0.5)
    data["severity"] = 0.5 if severity is None else severity
    data["status"] = str(data.get("status") or "open").strip().lower()
    data["detected_by"] = data.get("detected_by")
    data["contradiction_type"] = str(data.get("contradiction_type") or "semantic_conflict")
    data["resolved_at"] = _coerce_datetime(data.get("resolved_at"))
    data["is_open"] = data["status"] not in _RESOLVED_CONTRADICTION_STATUSES
    data["is_resolved"] = not data["is_open"]
    return data


def normalize_review_data(value: Any) -> dict[str, Any]:
    """Normalize a review row for API responses."""
    data = _object_to_dict(value)
    data["evidence"] = _coerce_jsonish(data.get("evidence")) or {}
    data["action"] = str(data.get("action") or "review")
    data["from_tier"] = str(data.get("from_tier") or "episodic")
    data["to_tier"] = str(data.get("to_tier") or "episodic")
    data["reviewer_id"] = data.get("reviewer_id")
    data["rationale"] = data.get("rationale")
    return data


def memory_is_truth_safe(value: Any, *, quarantine_filter: bool | None = None) -> bool:
    """Return whether a memory should remain retrievable in conservative mode."""
    if quarantine_filter is None:
        quarantine_filter = quarantine_filter_enabled()
    if not quarantine_filter:
        return True
    state = build_truth_state(value)
    return bool(state["is_active"])


def filter_truth_safe_memories(memories: Sequence[Any], *, quarantine_filter: bool | None = None) -> list[Any]:
    """Filter a sequence of memory-like objects using conservative truth rules."""
    if quarantine_filter is None:
        quarantine_filter = quarantine_filter_enabled()
    if not quarantine_filter:
        return list(memories)
    return [memory for memory in memories if memory_is_truth_safe(memory, quarantine_filter=True)]


def build_contradiction_evidence(
    *,
    similarity: float | None,
    left_valence: float | None,
    right_valence: float | None,
    detector: str,
    note: str | None = None,
    confidence: float | None = None,
    adjudication: Any = None,
) -> dict[str, Any]:
    payload = {
        "similarity": similarity,
        "left_valence": left_valence,
        "right_valence": right_valence,
        "detector": detector,
        "note": note,
    }
    if confidence is not None:
        payload["confidence"] = max(0.0, min(1.0, float(confidence)))
    if adjudication is not None:
        payload["adjudication"] = normalize_truth_adjudication(adjudication)
    return payload


def record_contradiction(
    session,
    *,
    left_memory_id: int,
    right_memory_id: int,
    contradiction_type: str,
    detected_by: str | None = None,
    evidence: dict[str, Any] | None = None,
    severity: float = 0.5,
    confidence: float | None = None,
    status: str = "open",
    resolution_memory_id: int | None = None,
    resolved_at: datetime | None = None,
    resolved_by: str | None = None,
) -> dict[str, Any]:
    """Upsert a structured contradiction row and return the stored record."""
    left_id = min(left_memory_id, right_memory_id)
    right_id = max(left_memory_id, right_memory_id)
    normalized_status = str(status or "open").strip().lower()
    evidence_payload = dict(evidence or {})
    if confidence is not None:
        evidence_payload["confidence"] = max(0.0, min(1.0, float(confidence)))
    payload = {
        "left_memory_id": left_id,
        "right_memory_id": right_id,
        "contradiction_type": contradiction_type,
        "detected_by": detected_by,
        "evidence": json.dumps(evidence_payload, default=str),
        "severity": max(0.0, min(1.0, float(severity))),
        "status": normalized_status,
        "resolution_memory_id": resolution_memory_id,
        "resolved_at": resolved_at,
        "resolved_by": resolved_by,
    }

    result = session.execute(
        text(
            """
            INSERT INTO memory_contradictions (
                left_memory_id, right_memory_id, detected_by, contradiction_type,
                evidence, severity, status, resolution_memory_id, resolved_at, resolved_by
            ) VALUES (
                :left_memory_id, :right_memory_id, :detected_by, :contradiction_type,
                :evidence, :severity, :status, :resolution_memory_id, :resolved_at, :resolved_by
            )
            ON CONFLICT (left_memory_id, right_memory_id, contradiction_type)
            DO UPDATE SET
                detected_by = COALESCE(EXCLUDED.detected_by, memory_contradictions.detected_by),
                evidence = EXCLUDED.evidence,
                severity = CASE
                    WHEN memory_contradictions.severity >= EXCLUDED.severity THEN memory_contradictions.severity
                    ELSE EXCLUDED.severity
                END,
                status = CASE
                    WHEN memory_contradictions.status IN ('resolved', 'closed', 'dismissed', 'accepted')
                         AND EXCLUDED.status IN ('open', 'reviewed', 'needs_review') THEN memory_contradictions.status
                    WHEN EXCLUDED.status IN ('resolved', 'closed', 'dismissed', 'accepted') THEN EXCLUDED.status
                    ELSE EXCLUDED.status
                END,
                resolution_memory_id = CASE
                    WHEN EXCLUDED.status IN ('resolved', 'closed', 'dismissed', 'accepted')
                         THEN COALESCE(EXCLUDED.resolution_memory_id, memory_contradictions.resolution_memory_id)
                    ELSE memory_contradictions.resolution_memory_id
                END,
                resolved_at = CASE
                    WHEN EXCLUDED.status IN ('resolved', 'closed', 'dismissed', 'accepted')
                         THEN COALESCE(EXCLUDED.resolved_at, memory_contradictions.resolved_at)
                    ELSE memory_contradictions.resolved_at
                END,
                resolved_by = CASE
                    WHEN EXCLUDED.status IN ('resolved', 'closed', 'dismissed', 'accepted')
                         THEN COALESCE(EXCLUDED.resolved_by, memory_contradictions.resolved_by)
                    ELSE memory_contradictions.resolved_by
                END
            RETURNING id, left_memory_id, right_memory_id, detected_by, contradiction_type,
                      evidence, severity, status, resolution_memory_id, created_at, resolved_at, resolved_by
            """
        ),
        payload,
    )
    row = result.mappings().first()
    if row is None:
        row_id = getattr(result, "lastrowid", None)
        if row_id is None:
            return {"id": None, **payload}
        row = {"id": row_id, **payload}
    return dict(row)


def record_memory_review(
    session,
    *,
    memory_id: int,
    action: str,
    from_tier: str,
    to_tier: str,
    reviewer_id: str | None = None,
    rationale: str | None = None,
    evidence: dict[str, Any] | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    """Insert a structured memory review row and return the stored record."""
    action_context = validate_truth_action_context(
        action=action,
        evidence=evidence,
        confidence=confidence,
    )
    payload = {
        "memory_id": memory_id,
        "action": action_context["action"],
        "from_tier": from_tier,
        "to_tier": to_tier,
        "reviewer_id": reviewer_id,
        "rationale": rationale,
        "evidence": json.dumps(action_context["evidence"], default=str),
    }
    result = session.execute(
        text(
            """
            INSERT INTO memory_reviews (
                memory_id, action, from_tier, to_tier, reviewer_id, rationale, evidence
            ) VALUES (
                :memory_id, :action, :from_tier, :to_tier, :reviewer_id, :rationale, :evidence
            )
            RETURNING id, memory_id, action, from_tier, to_tier, reviewer_id, rationale, evidence, created_at
            """
        ),
        payload,
    )
    row = result.mappings().first()
    if row is None:
        row_id = getattr(result, "lastrowid", None)
        if row_id is None:
            return {"id": None, **payload}
        row = {"id": row_id, **payload}
    return dict(row)


def build_consolidation_truth_fields(
    *,
    source_kind: str,
    source_ref: str | None,
    confidence: float,
    evidence: Any = None,
    target_tier: str = "semantic",
    support_count: int = 1,
    reviewed_by: str | None = None,
    adjudication: Any = None,
    policy_kind: str | None = None,
    policy_scope: str | None = None,
    open_contradiction_count: int = 0,
) -> dict[str, Any]:
    """Return conservative truth fields for consolidation-created memories."""
    target_tier = str(target_tier or "semantic").strip().lower()
    support_count = max(0, int(support_count))
    confidence = max(0.0, min(1.0, confidence))
    created_at = datetime.now(timezone.utc)
    promotion_evidence = evidence or {
        "source_kind": source_kind,
        "source_ref": source_ref,
        "support_count": support_count,
    }
    human_reviewed = bool(isinstance(promotion_evidence, Mapping) and promotion_evidence.get("human_reviewed"))
    reviewed = bool(reviewed_by is not None and human_reviewed)

    if target_tier == "policy":
        return build_policy_truth_fields(
            source_kind=source_kind,
            source_ref=source_ref,
            confidence=confidence,
            evidence=promotion_evidence,
            support_count=support_count,
            policy_kind=policy_kind or "runtime",
            policy_scope=policy_scope or source_ref or source_kind,
            reviewed_by=reviewed_by if reviewed else None,
            adjudication=adjudication,
            open_contradiction_count=open_contradiction_count,
        )

    # Episodic -> semantic and semantic -> procedural both stay conservative:
    # the synthesis is review-backed, but the synthesized claim remains tentative
    # until a later human or policy review hardens it.
    safe_to_promote, _reason = can_promote_memory(
        from_tier={"semantic": "episodic", "procedural": "semantic"}.get(target_tier, "episodic"),
        to_tier=target_tier,
        confidence=confidence,
        evidence=promotion_evidence,
        support_count=support_count,
        reviewed=reviewed,
        adjudication=adjudication,
        open_contradiction_count=open_contradiction_count,
    )
    return {
        "truth_status": "tentative" if safe_to_promote else "unknown",
        "review_status": "reviewed" if safe_to_promote and reviewed else "adjudicated" if safe_to_promote else "unreviewed",
        "confidence": confidence if safe_to_promote else min(confidence, 0.6),
        "freshness_score": 1.0 if safe_to_promote else 0.9,
        "staleness_score": 0.0 if safe_to_promote else 0.1,
        "source_type": source_kind,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "observed_at": created_at,
        "valid_from": created_at,
        "valid_until": None,
        "policy_kind": None,
        "policy_scope": None,
        "reviewed_at": created_at if reviewed else None,
        "reviewed_by": reviewed_by if reviewed else None,
        "demoted_at": None,
        "demotion_reason": None,
    }
