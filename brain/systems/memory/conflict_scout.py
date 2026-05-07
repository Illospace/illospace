"""Cheap context-memory conflict scout.

This module only compares memories that the caller already selected or nearly
selected for context. It does not query storage, scan global memory, or call an
LLM. Ambiguous conflicts stay advisory so the hot path can avoid obvious stale
claims while deferring real adjudication to night-mode truth maintenance.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from brain.systems.memory.truth_maintenance import normalize_memory_claim_metadata

FreshnessStatus = Literal["fresh", "possibly_stale", "stale", "unknown"]
ConflictSeverity = Literal["low", "medium", "high"]
ConflictAction = Literal["include_neither", "include_one", "include_both_with_warning", "defer"]

_CORRECTION_RE = re.compile(
    r"(?i)\b(?:actually\s+)?use\s+(?P<new>.+?)\s+instead\s+of\s+(?P<old>.+?)(?:[.!?]|$)"
)
_NOT_CORRECTION_RE = re.compile(
    r"(?i)\buse\s+(?P<new>.+?),?\s+not\s+(?P<old>.+?)(?:[.!?]|$)"
)


@dataclass(frozen=True)
class MemoryConflictCandidate:
    """Normalized shape for one memory considered by the scout pass."""

    memory_id: str
    content: str
    claim_digest: str
    subject_key: str | None = None
    source_digest: str | None = None
    superseded_by: str | None = None
    supersedes: tuple[str, ...] = ()
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    truth_status: str = "unknown"
    freshness_status: FreshnessStatus = "unknown"
    raw: Any = None

    @property
    def active(self) -> bool:
        return self.truth_status not in {"archived", "expired", "quarantined", "superseded"}


@dataclass(frozen=True)
class ContextConflictNotice:
    """Advisory hot-path notice for memories that appear mutually unsafe."""

    conflict_ids: tuple[str, str]
    severity: ConflictSeverity
    recommended_action: ConflictAction
    reasons: tuple[str, ...]
    confidence: float
    preferred_memory_id: str | None = None
    preferred_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_ids": list(self.conflict_ids),
            "severity": self.severity,
            "recommended_action": self.recommended_action,
            "reasons": list(self.reasons),
            "confidence": self.confidence,
            "preferred_memory_id": self.preferred_memory_id,
            "preferred_reason": self.preferred_reason,
        }


FreshnessLookup = Mapping[str, FreshnessStatus] | Callable[[Any], FreshnessStatus | Mapping[str, Any] | None]


def scout_memory_conflicts(
    memories: Sequence[Any] | None,
    *,
    freshness: FreshnessLookup | None = None,
    max_candidates: int = 12,
    now: datetime | None = None,
) -> list[ContextConflictNotice]:
    """Return deterministic conflict notices for already-selected memories.

    ``max_candidates`` is deliberately small because this runs on prompt-context
    candidates, not the full memory corpus.
    """
    candidates = [
        _normalize_candidate(memory, freshness=freshness)
        for memory in list(memories or [])[: max(0, max_candidates)]
    ]
    candidates = [candidate for candidate in candidates if candidate.memory_id]
    reference_time = _coerce_datetime(now) or datetime.now(timezone.utc)

    notices: list[ContextConflictNotice] = []
    for left_index, left in enumerate(candidates):
        for right in candidates[left_index + 1 :]:
            notice = _compare_candidates(left, right, now=reference_time)
            if notice is not None:
                notices.append(notice)

    return sorted(
        notices,
        key=lambda notice: (
            {"high": 0, "medium": 1, "low": 2}.get(notice.severity, 3),
            -notice.confidence,
            notice.conflict_ids,
        ),
    )


def has_blocking_context_conflict(notices: Sequence[ContextConflictNotice | Mapping[str, Any]]) -> bool:
    """Return whether any notice recommends suppressing or selecting one memory."""
    for notice in notices:
        action = notice.recommended_action if isinstance(notice, ContextConflictNotice) else notice.get("recommended_action")
        if action in {"include_neither", "include_one"}:
            return True
    return False


def _compare_candidates(
    left: MemoryConflictCandidate,
    right: MemoryConflictCandidate,
    *,
    now: datetime,
) -> ContextConflictNotice | None:
    explicit = _explicit_supersession_notice(left, right)
    if explicit:
        return explicit

    if not _same_subject(left, right):
        return None

    if left.claim_digest == right.claim_digest:
        return None

    if not _validity_overlaps(left, right, now=now):
        return None

    correction = _correction_notice(left, right)
    if correction:
        return correction

    freshness_notice = _freshness_notice(left, right)
    if freshness_notice:
        return freshness_notice

    stale_pair = {left.freshness_status, right.freshness_status}
    reasons = ["same_subject_different_claim_digest", "validity_windows_overlap"]
    if "possibly_stale" in stale_pair:
        reasons.append("one_or_more_candidates_possibly_stale")

    return ContextConflictNotice(
        conflict_ids=_ordered_ids(left, right),
        severity="medium",
        recommended_action="include_both_with_warning",
        reasons=tuple(reasons),
        confidence=0.62 if "possibly_stale" not in stale_pair else 0.68,
    )


def _normalize_candidate(memory: Any, *, freshness: FreshnessLookup | None) -> MemoryConflictCandidate:
    data = _object_to_dict(memory)
    claim_metadata = normalize_memory_claim_metadata(data)
    memory_id = _stringish(data.get("id") or data.get("memory_id"))
    content = _stringish(data.get("content") or data.get("text") or data.get("claim") or "")
    source_digest = _stringish(claim_metadata.get("source_digest") or data.get("source_digest"))
    subject_key = _subject_key(data, claim_metadata)
    claim_digest = _stringish(data.get("claim_digest") or source_digest) or _digest(content)
    superseded_by = _stringish(data.get("superseded_by"))
    supersedes = _coerce_id_tuple(data.get("supersedes") or data.get("supersedes_ids"))

    return MemoryConflictCandidate(
        memory_id=memory_id,
        content=content,
        claim_digest=claim_digest,
        subject_key=subject_key,
        source_digest=source_digest,
        superseded_by=superseded_by,
        supersedes=supersedes,
        valid_from=claim_metadata.get("valid_from"),
        valid_until=claim_metadata.get("valid_until"),
        truth_status=_stringish(data.get("truth_status") or "unknown").strip().lower() or "unknown",
        freshness_status=_lookup_freshness(memory, data=data, memory_id=memory_id, freshness=freshness),
        raw=memory,
    )


def _explicit_supersession_notice(
    left: MemoryConflictCandidate,
    right: MemoryConflictCandidate,
) -> ContextConflictNotice | None:
    if left.superseded_by == right.memory_id or right.memory_id in left.supersedes:
        return _preferred_notice(left, right, preferred=right, reason="explicit_supersession")
    if right.superseded_by == left.memory_id or left.memory_id in right.supersedes:
        return _preferred_notice(left, right, preferred=left, reason="explicit_supersession")
    if not left.active and right.active:
        return _preferred_notice(left, right, preferred=right, reason="inactive_or_superseded_candidate")
    if not right.active and left.active:
        return _preferred_notice(left, right, preferred=left, reason="inactive_or_superseded_candidate")
    return None


def _correction_notice(
    left: MemoryConflictCandidate,
    right: MemoryConflictCandidate,
) -> ContextConflictNotice | None:
    left_terms = _extract_correction_terms(left.content)
    if left_terms and _term_in_content(left_terms[1], right.content):
        return _preferred_notice(left, right, preferred=left, reason="explicit_correction_cue")
    right_terms = _extract_correction_terms(right.content)
    if right_terms and _term_in_content(right_terms[1], left.content):
        return _preferred_notice(left, right, preferred=right, reason="explicit_correction_cue")
    return None


def _freshness_notice(
    left: MemoryConflictCandidate,
    right: MemoryConflictCandidate,
) -> ContextConflictNotice | None:
    left_rank = _freshness_rank(left.freshness_status)
    right_rank = _freshness_rank(right.freshness_status)
    if abs(left_rank - right_rank) < 2:
        return None
    preferred = left if left_rank > right_rank else right
    return _preferred_notice(left, right, preferred=preferred, reason="source_freshness_mismatch", confidence=0.72)


def _preferred_notice(
    left: MemoryConflictCandidate,
    right: MemoryConflictCandidate,
    *,
    preferred: MemoryConflictCandidate,
    reason: str,
    confidence: float = 0.86,
) -> ContextConflictNotice:
    return ContextConflictNotice(
        conflict_ids=_ordered_ids(left, right),
        severity="high",
        recommended_action="include_one",
        reasons=(reason,),
        confidence=confidence,
        preferred_memory_id=preferred.memory_id,
        preferred_reason=reason,
    )


def _same_subject(left: MemoryConflictCandidate, right: MemoryConflictCandidate) -> bool:
    if left.subject_key and right.subject_key:
        return left.subject_key == right.subject_key
    return _overlap_ratio(left.content, right.content) >= 0.55


def _validity_overlaps(
    left: MemoryConflictCandidate,
    right: MemoryConflictCandidate,
    *,
    now: datetime,
) -> bool:
    left_start = left.valid_from or datetime.min.replace(tzinfo=timezone.utc)
    left_end = left.valid_until or now
    right_start = right.valid_from or datetime.min.replace(tzinfo=timezone.utc)
    right_end = right.valid_until or now
    return left_start <= right_end and right_start <= left_end


def _lookup_freshness(
    memory: Any,
    *,
    data: Mapping[str, Any],
    memory_id: str,
    freshness: FreshnessLookup | None,
) -> FreshnessStatus:
    explicit = data.get("source_freshness") or data.get("freshness_status")
    if explicit:
        return _coerce_freshness_status(explicit)
    if freshness is None:
        return "unknown"
    value: Any = None
    if isinstance(freshness, Mapping):
        value = freshness.get(memory_id) or freshness.get(data.get("id"))
    else:
        value = freshness(memory)
    if isinstance(value, Mapping):
        value = value.get("status") or value.get("freshness_status")
    return _coerce_freshness_status(value)


def _coerce_freshness_status(value: Any) -> FreshnessStatus:
    status = _stringish(value).strip().lower()
    if status in {"fresh", "possibly_stale", "stale", "unknown"}:
        return status  # type: ignore[return-value]
    return "unknown"


def _freshness_rank(status: FreshnessStatus) -> int:
    return {"stale": 0, "possibly_stale": 1, "unknown": 2, "fresh": 3}.get(status, 2)


def _subject_key(data: Mapping[str, Any], claim_metadata: Mapping[str, Any]) -> str | None:
    subject_ref = claim_metadata.get("subject_ref") or data.get("subject_ref")
    if subject_ref:
        subject_type = claim_metadata.get("subject_type") or data.get("subject_type") or "subject"
        return f"{_stringish(subject_type).strip().lower()}:{_stringish(subject_ref).strip().lower()}"
    policy_scope = data.get("policy_scope")
    if policy_scope:
        return f"policy:{_stringish(policy_scope).strip().lower()}"
    return None


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


def _term_in_content(term: str, content: str) -> bool:
    normalized_term = _clean_term(term).lower()
    normalized_content = _clean_term(content).lower()
    return bool(normalized_term and normalized_term in normalized_content)


def _clean_term(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t\n\r\"'`.,;:!?")[:160]


def _overlap_ratio(left: str, right: str) -> float:
    left_terms = {term for term in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", left.lower())}
    right_terms = {term for term in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", right.lower())}
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / max(1, min(len(left_terms), len(right_terms)))


def _ordered_ids(left: MemoryConflictCandidate, right: MemoryConflictCandidate) -> tuple[str, str]:
    return tuple(sorted((left.memory_id, right.memory_id)))


def _coerce_id_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(_stringish(item) for item in value if _stringish(item))
    return (_stringish(value),) if _stringish(value) else ()


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _object_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return {}


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _stringish(value: Any) -> str:
    if value is None:
        return ""
    return str(value)
