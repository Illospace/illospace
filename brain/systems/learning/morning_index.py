"""Compact morning index and runtime hints.

The morning index is a hot-path handoff from background learning/consolidation
jobs to run-time context assembly. It deliberately consumes already-built
metadata: repo-summary hot paths, stale-memory ids, skill pins, policy
thresholds, and review ledgers. It does not scan the repository, call models, or
read long prose fields.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from brain.kernel.common.coercion import drop_none as _shared_drop_none
from brain.kernel.common.coercion import int_or_none as _shared_int_or_none
from brain.kernel.common.coercion import mapping_view as _shared_mapping_view
from brain.kernel.common.coercion import optional_text as _shared_optional_text
from brain.kernel.common.serialization import jsonable as _shared_jsonable
from brain.systems.context.policy import context_policy_runtime_hints, normalize_task_class
from brain.systems.routing.skills import (
    SkillRoutingQualityPolicy,
    skill_routing_policy_payload,
)

MORNING_INDEX_SCHEMA_VERSION = "morning-index/v1"
MORNING_INDEX_SOURCE_VERSION = "morning-index-sources/v1"

DEFAULT_MAX_REPO_SUMMARIES = 8
DEFAULT_MAX_STALE_MEMORIES = 100
DEFAULT_MAX_SKILL_PINS_PER_TASK = 3
DEFAULT_MAX_REVIEW_ITEMS = 25

_INACTIVE_TRUTH_STATUSES = {"archived", "expired", "quarantined", "superseded"}
_RESOLVED_REVIEW_STATUSES = {
    "accepted",
    "approved",
    "closed",
    "complete",
    "completed",
    "done",
    "pass",
    "passed",
    "rejected",
    "resolved",
    "reviewed",
    "suppressed",
}
_HIGH_RISK_VALUES = {"blocking", "critical", "high", "high_risk", "must_verify", "required"}


@dataclass(frozen=True, slots=True)
class MorningSourceRef:
    """One compact source digest that can invalidate a morning index."""

    key: str
    digest: str | None
    source_type: str

    def to_payload(self) -> dict[str, Any]:
        return _drop_none({
            "key": self.key,
            "digest": self.digest,
            "source_type": self.source_type,
        })


@dataclass(frozen=True, slots=True)
class RepoSummaryHint:
    """Hot-path repository summary metadata without generated prose."""

    summary_id: str | None
    scope_digest: str | None
    summary_kind: str | None
    repo_root: str | None
    branch: str | None
    commit_sha: str | None
    source_digest: str | None
    source_digest_complete: bool | None
    hot_path: Mapping[str, Any] = field(default_factory=dict)
    generated_at: str | None = None

    @classmethod
    def from_summary(cls, summary: Mapping[str, Any]) -> "RepoSummaryHint":
        data = _mapping(summary)
        identity = _summary_identity(data)
        hot_path = _compact_repo_hot_path(
            _mapping(data.get("hot_path"))
            or _mapping(data.get("architecture"))
            or data
        )
        source_digest = _text(
            data.get("source_digest")
            or identity.get("source_digest")
            or hot_path.get("source_digest")
        )
        return cls(
            summary_id=_text(data.get("summary_id") or data.get("id") or identity.get("identity_digest")),
            scope_digest=_text(
                data.get("summary_scope_digest")
                or data.get("scope_digest")
                or identity.get("scope_digest")
            ),
            summary_kind=_text(data.get("summary_kind") or identity.get("summary_kind")),
            repo_root=_text(data.get("repo_root") or identity.get("repo_root") or hot_path.get("repo_root")),
            branch=_text(data.get("branch") or identity.get("branch") or hot_path.get("branch")),
            commit_sha=_text(data.get("commit_sha") or identity.get("commit_sha") or hot_path.get("commit_sha")),
            source_digest=source_digest,
            source_digest_complete=_bool_or_none(
                data.get("source_digest_complete")
                if "source_digest_complete" in data
                else hot_path.get("source_digest_complete")
            ),
            hot_path=hot_path,
            generated_at=_text(data.get("generated_at") or data.get("observed_at")),
        )

    @property
    def source_key(self) -> str:
        return "repo_summary:" + (self.scope_digest or self.summary_id or self.source_digest or "unknown")

    def source_ref(self) -> MorningSourceRef:
        return MorningSourceRef(self.source_key, self.source_digest, "repo_summary")

    def to_payload(self) -> dict[str, Any]:
        return _drop_none({
            "summary_id": self.summary_id,
            "scope_digest": self.scope_digest,
            "summary_kind": self.summary_kind,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "source_digest": self.source_digest,
            "source_digest_complete": self.source_digest_complete,
            "generated_at": self.generated_at,
            "hot_path": _jsonable(dict(self.hot_path)),
        })


@dataclass(frozen=True, slots=True)
class StaleMemoryHint:
    """Known stale memory reference without raw memory content."""

    memory_id: str
    item_id: str
    item_digest: str
    content_digest: str | None
    source_digest: str | None
    freshness_status: str | None
    staleness_score: float | None
    truth_status: str | None
    confidence: float | None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_memory(cls, memory: Mapping[str, Any]) -> "StaleMemoryHint | None":
        data = _mapping(memory)
        memory_id = _text(data.get("id") or data.get("memory_id"))
        freshness = _freshness_map(data)
        freshness_status = _normalized_token(
            freshness.get("status")
            or freshness.get("freshness_status")
            or data.get("freshness_status")
            or data.get("source_freshness_status")
        )
        truth = _mapping(data.get("truth_state"))
        truth_status = _normalized_token(
            truth.get("truth_status") or data.get("truth_status") or data.get("lifecycle_status")
        )
        staleness_score = _first_float(
            freshness.get("staleness_score"),
            data.get("staleness_score"),
        )
        if staleness_score is None:
            freshness_score = _first_float(freshness.get("freshness_score"), data.get("freshness_score"))
            if freshness_score is not None:
                staleness_score = 1.0 - freshness_score

        reasons = _stale_memory_reasons(
            freshness_status=freshness_status,
            truth_status=truth_status,
            staleness_score=staleness_score,
        )
        if not reasons:
            return None

        content_digest = _text(
            data.get("content_digest")
            or data.get("claim_digest")
            or data.get("source_digest")
        )
        source_digest = _text(data.get("source_digest") or freshness.get("source_digest"))
        identity = {
            "memory_id": memory_id,
            "memory_type": _text(data.get("type") or data.get("memory_type")),
            "tier": _text(data.get("tier") or data.get("memory_tier")),
            "content_digest": content_digest,
            "source_digest": source_digest,
        }
        item_digest = _text(data.get("item_digest")) or _digest_payload(identity, length=24)
        item_id = _text(data.get("item_id")) or f"memory:{memory_id or item_digest}"
        return cls(
            memory_id=memory_id or item_digest,
            item_id=item_id,
            item_digest=item_digest,
            content_digest=content_digest,
            source_digest=source_digest,
            freshness_status=freshness_status,
            staleness_score=_round_or_none(staleness_score),
            truth_status=truth_status,
            confidence=_round_or_none(
                _first_float(
                    data.get("freshness_confidence"),
                    freshness.get("confidence"),
                    data.get("confidence"),
                    truth.get("confidence"),
                )
            ),
            reasons=tuple(reasons),
        )

    @property
    def source_key(self) -> str:
        return f"stale_memory:{self.memory_id}"

    def source_ref(self) -> MorningSourceRef:
        digest = self.source_digest or self.content_digest or self.item_digest
        return MorningSourceRef(self.source_key, digest, "stale_memory")

    def to_payload(self) -> dict[str, Any]:
        return _drop_none({
            "memory_id": self.memory_id,
            "item_id": self.item_id,
            "item_digest": self.item_digest,
            "content_digest": self.content_digest,
            "source_digest": self.source_digest,
            "freshness_status": self.freshness_status,
            "staleness_score": self.staleness_score,
            "truth_status": self.truth_status,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
        })


@dataclass(frozen=True, slots=True)
class SkillPinHint:
    """Preferred skill metadata for a normalized task class."""

    task_class: str
    name: str | None
    effective_digest: str | None
    rank: int
    pin_strength: float | None
    trust_level: str | None
    source: str | None
    reasons: tuple[str, ...] = field(default_factory=tuple)
    item_digest: str | None = None

    @classmethod
    def from_candidate(
        cls,
        item: Mapping[str, Any],
        *,
        task_class: str | None,
        index: int,
    ) -> "SkillPinHint | None":
        data = _mapping(item)
        resolved_task = normalize_task_class(task_class or data.get("task_class"))
        name = _text(data.get("name") or data.get("skill_name"))
        effective_digest = _text(data.get("effective_digest") or data.get("skill_effective_digest"))
        if not name and not effective_digest:
            return None
        quality = _mapping(data.get("quality"))
        routing = _mapping(data.get("quality_routing"))
        score = _first_float(
            data.get("pin_strength"),
            routing.get("final_score"),
            quality.get("score"),
            data.get("match_score"),
            data.get("score"),
        )
        rank = _int_or_none(data.get("pin_rank") or data.get("rank")) or index
        identity = {
            "task_class": resolved_task,
            "name": name,
            "effective_digest": effective_digest,
            "rank": rank,
            "pin_strength": _round_or_none(score),
        }
        return cls(
            task_class=resolved_task,
            name=name,
            effective_digest=effective_digest,
            rank=rank,
            pin_strength=_round_or_none(score),
            trust_level=_text(data.get("trust_level")),
            source=_text(data.get("source") or data.get("pin_source")),
            reasons=tuple(_compact_reason_list(
                data.get("pin_reasons"),
                data.get("reasons"),
                routing.get("gate_reasons"),
            )),
            item_digest=_text(data.get("item_digest")) or _digest_payload(identity, length=24),
        )

    @property
    def dedupe_key(self) -> tuple[str, str]:
        return (self.task_class, self.effective_digest or self.name or self.item_digest or "")

    @property
    def source_key(self) -> str:
        return f"skill_pin:{self.task_class}:{self.effective_digest or self.name or self.item_digest}"

    def source_ref(self) -> MorningSourceRef:
        digest = self.effective_digest or self.item_digest or _digest_payload(self.to_payload())
        return MorningSourceRef(self.source_key, digest, "skill_pin")

    def to_payload(self) -> dict[str, Any]:
        return _drop_none({
            "task_class": self.task_class,
            "name": self.name,
            "effective_digest": self.effective_digest,
            "rank": self.rank,
            "pin_strength": self.pin_strength,
            "trust_level": self.trust_level,
            "source": self.source,
            "reasons": list(self.reasons),
            "item_digest": self.item_digest,
        })


@dataclass(frozen=True, slots=True)
class ReviewItemHint:
    """Unresolved high-risk review metadata without long evidence prose."""

    review_id: str
    risk_level: str | None
    severity: str | None
    status: str | None
    source: str | None
    source_reference: str | None
    source_digest: str | None
    target_type: str | None
    target_id: str | None
    created_at: str | None
    updated_at: str | None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> "ReviewItemHint | None":
        data = _mapping(item)
        risk_level = _normalized_token(data.get("risk_level") or data.get("risk") or data.get("risk_class"))
        severity = _normalized_token(data.get("severity"))
        status = _normalized_token(
            data.get("review_status")
            or data.get("status")
            or data.get("outcome_status")
            or data.get("state")
        )
        if not _is_unresolved_high_risk(
            risk_level=risk_level,
            severity=severity,
            status=status,
            item=data,
        ):
            return None

        review_id = _text(
            data.get("review_id")
            or data.get("id")
            or data.get("item_id")
            or data.get("trace_id")
        )
        target = _mapping(data.get("target"))
        metadata = {
            "review_id": review_id,
            "risk_level": risk_level,
            "severity": severity,
            "status": status,
            "source": _text(data.get("source")),
            "source_ref": _text(data.get("source_ref") or data.get("trace_id")),
            "target_type": _text(data.get("target_type") or target.get("type")),
            "target_id": _text(data.get("target_id") or target.get("id") or target.get("path")),
        }
        source_digest = _text(data.get("source_digest") or data.get("item_digest")) or _digest_payload(
            metadata,
            length=24,
        )
        return cls(
            review_id=review_id or source_digest,
            risk_level=risk_level,
            severity=severity,
            status=status,
            source=metadata["source"],
            source_reference=metadata["source_ref"],
            source_digest=source_digest,
            target_type=metadata["target_type"],
            target_id=metadata["target_id"],
            created_at=_text(data.get("created_at") or data.get("started_at")),
            updated_at=_text(data.get("updated_at") or data.get("completed_at")),
            reasons=tuple(_compact_reason_list(
                data.get("reason_code"),
                data.get("reasons"),
                data.get("failure_reason"),
                data.get("type"),
            )),
        )

    @property
    def source_key(self) -> str:
        return f"review_item:{self.review_id}"

    def source_ref(self) -> MorningSourceRef:
        return MorningSourceRef(self.source_key, self.source_digest, "review_item")

    def to_payload(self) -> dict[str, Any]:
        return _drop_none({
            "review_id": self.review_id,
            "risk_level": self.risk_level,
            "severity": self.severity,
            "status": self.status,
            "source": self.source,
            "source_ref": self.source_reference,
            "source_digest": self.source_digest,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reasons": list(self.reasons),
        })


@dataclass(frozen=True, slots=True)
class MorningIndexScopeInput:
    """Already-materialized metadata for one morning-index scope."""

    scope_id: str
    scope_type: str = "workspace"
    repo_summaries: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    stale_memories: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    skill_pins: Mapping[str, Any] | Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    review_items: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    source_digests: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MorningIndexScopeInput":
        data = _mapping(value)
        return cls(
            scope_id=_text(data.get("scope_id") or data.get("id") or data.get("scope")) or "default",
            scope_type=_text(data.get("scope_type") or data.get("type")) or "workspace",
            repo_summaries=tuple(_mapping_items(
                data.get("repo_summaries") or data.get("current_repo_summaries")
            )),
            stale_memories=tuple(_mapping_items(
                data.get("stale_memories")
                or data.get("known_stale_memories")
                or data.get("memory_hints")
            )),
            skill_pins=data.get("skill_pins")
            or data.get("preferred_skill_pins")
            or data.get("skill_recommendations_by_task_class")
            or (),
            review_items=tuple(_mapping_items(
                data.get("review_items")
                or data.get("unresolved_review_items")
                or data.get("high_risk_review_items")
            )),
            source_digests=_mapping(data.get("source_digests")),
        )


@dataclass(frozen=True, slots=True)
class MorningScopeHints:
    """Compact per-scope runtime hints exported by the morning index."""

    scope_id: str
    scope_type: str
    repo_summaries: tuple[RepoSummaryHint, ...] = field(default_factory=tuple)
    stale_memories: tuple[StaleMemoryHint, ...] = field(default_factory=tuple)
    skill_pins: tuple[SkillPinHint, ...] = field(default_factory=tuple)
    unresolved_review_items: tuple[ReviewItemHint, ...] = field(default_factory=tuple)
    context_policy: Mapping[str, Any] = field(default_factory=dict)
    skill_routing_policy: Mapping[str, Any] = field(default_factory=dict)
    extra_source_refs: tuple[MorningSourceRef, ...] = field(default_factory=tuple)

    @property
    def known_stale_memory_ids(self) -> tuple[str, ...]:
        return tuple(memory.memory_id for memory in self.stale_memories)

    def source_refs(self) -> tuple[MorningSourceRef, ...]:
        refs = [
            *[hint.source_ref() for hint in self.repo_summaries],
            *[hint.source_ref() for hint in self.stale_memories],
            *[hint.source_ref() for hint in self.skill_pins],
            *[hint.source_ref() for hint in self.unresolved_review_items],
            *self.extra_source_refs,
        ]
        return tuple(_dedupe_source_refs(refs))

    def to_payload(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "scope_type": self.scope_type,
            "repo_summaries": [hint.to_payload() for hint in self.repo_summaries],
            "known_stale_memory_ids": list(self.known_stale_memory_ids),
            "stale_memories": [hint.to_payload() for hint in self.stale_memories],
            "preferred_skill_pins": _skill_pin_payload_by_task(self.skill_pins),
            "context_policy": _jsonable(dict(self.context_policy)),
            "skill_routing_policy": _jsonable(dict(self.skill_routing_policy)),
            "unresolved_high_risk_review_items": [
                hint.to_payload()
                for hint in self.unresolved_review_items
            ],
            "source_digest_count": len(self.source_refs()),
        }


@dataclass(frozen=True, slots=True)
class MorningIndex:
    """Versioned morning index payload."""

    scopes: tuple[MorningScopeHints, ...]
    source_manifest: Mapping[str, Any]
    source_fingerprint: str
    generated_at: str | None = None
    schema_version: str = MORNING_INDEX_SCHEMA_VERSION

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "source_fingerprint": self.source_fingerprint,
            "source_manifest": _jsonable(dict(self.source_manifest)),
            "scopes": [scope.to_payload() for scope in self.scopes],
            "stats": _index_stats(self.scopes),
        }
        if self.generated_at:
            payload["generated_at"] = self.generated_at
        return payload


def build_morning_scope_hints(
    scope: MorningIndexScopeInput | Mapping[str, Any],
    *,
    context_policy: Mapping[str, Any] | None = None,
    skill_routing_policy: Mapping[str, Any] | SkillRoutingQualityPolicy | None = None,
    max_repo_summaries: int = DEFAULT_MAX_REPO_SUMMARIES,
    max_stale_memories: int = DEFAULT_MAX_STALE_MEMORIES,
    max_skill_pins_per_task: int = DEFAULT_MAX_SKILL_PINS_PER_TASK,
    max_review_items: int = DEFAULT_MAX_REVIEW_ITEMS,
) -> MorningScopeHints:
    """Build compact hints for one scope from already-available metadata."""

    scope_input = scope if isinstance(scope, MorningIndexScopeInput) else MorningIndexScopeInput.from_mapping(scope)
    repo_hints = tuple(_repo_summary_hints(scope_input.repo_summaries, limit=max_repo_summaries))
    stale_hints = tuple(_stale_memory_hints(scope_input.stale_memories, limit=max_stale_memories))
    skill_hints = tuple(_skill_pin_hints(
        scope_input.skill_pins,
        max_per_task_class=max_skill_pins_per_task,
    ))
    review_hints = tuple(_review_item_hints(scope_input.review_items, limit=max_review_items))
    return MorningScopeHints(
        scope_id=scope_input.scope_id,
        scope_type=scope_input.scope_type,
        repo_summaries=repo_hints,
        stale_memories=stale_hints,
        skill_pins=skill_hints,
        unresolved_review_items=review_hints,
        context_policy=_resolve_context_policy(context_policy),
        skill_routing_policy=_resolve_skill_routing_policy(skill_routing_policy),
        extra_source_refs=tuple(_extra_source_refs(scope_input.source_digests, scope_id=scope_input.scope_id)),
    )


def build_morning_index(
    scopes: Sequence[MorningIndexScopeInput | Mapping[str, Any]] | None = None,
    *,
    scope_id: str = "default",
    scope_type: str = "workspace",
    repo_summaries: Sequence[Mapping[str, Any]] | None = None,
    stale_memories: Sequence[Mapping[str, Any]] | None = None,
    skill_pins: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    review_items: Sequence[Mapping[str, Any]] | None = None,
    source_digests: Mapping[str, Any] | None = None,
    context_policy: Mapping[str, Any] | None = None,
    skill_routing_policy: Mapping[str, Any] | SkillRoutingQualityPolicy | None = None,
    generated_at: datetime | str | None = None,
) -> MorningIndex:
    """Build a deterministic morning index from compact metadata inputs."""

    if scopes is None:
        scopes = (
            MorningIndexScopeInput(
                scope_id=scope_id,
                scope_type=scope_type,
                repo_summaries=tuple(repo_summaries or ()),
                stale_memories=tuple(stale_memories or ()),
                skill_pins=skill_pins or (),
                review_items=tuple(review_items or ()),
                source_digests=source_digests or {},
            ),
        )
    context_hints = _resolve_context_policy(context_policy)
    skill_policy_hints = _resolve_skill_routing_policy(skill_routing_policy)
    scope_hints = tuple(
        build_morning_scope_hints(
            scope,
            context_policy=context_hints,
            skill_routing_policy=skill_policy_hints,
        )
        for scope in scopes
    )
    source_manifest = build_morning_source_manifest(
        scope_hints,
        context_policy=context_hints,
        skill_routing_policy=skill_policy_hints,
    )
    return MorningIndex(
        scopes=scope_hints,
        source_manifest=source_manifest,
        source_fingerprint=morning_index_source_fingerprint(source_manifest),
        generated_at=_isoformat(generated_at),
    )


def build_morning_source_manifest(
    scopes: Sequence[MorningScopeHints | Mapping[str, Any]],
    *,
    context_policy: Mapping[str, Any] | None = None,
    skill_routing_policy: Mapping[str, Any] | SkillRoutingQualityPolicy | None = None,
) -> dict[str, Any]:
    """Return sorted source digests and policy versions for invalidation."""

    refs: list[MorningSourceRef] = []
    for scope in scopes:
        if isinstance(scope, MorningScopeHints):
            refs.extend(scope.source_refs())
        else:
            data = _mapping(scope)
            raw_refs = data.get("source_digests")
            if isinstance(raw_refs, Mapping):
                refs.extend(_extra_source_refs(
                    raw_refs,
                    scope_id=_text(data.get("scope_id")) or "scope",
                ))
                continue
            for item in _mapping_items(raw_refs):
                refs.append(MorningSourceRef(
                    key=_text(item.get("key")) or "source:unknown",
                    digest=_text(item.get("digest")),
                    source_type=_text(item.get("source_type")) or "metadata",
                ))
    context_hints = _resolve_context_policy(context_policy)
    skill_policy_hints = _resolve_skill_routing_policy(skill_routing_policy)
    return {
        "schema_version": MORNING_INDEX_SOURCE_VERSION,
        "policy_versions": {
            "morning_index": MORNING_INDEX_SCHEMA_VERSION,
            "context_policy": _text(context_hints.get("policy_version")),
            "skill_routing_policy": _text(skill_policy_hints.get("policy_version")),
        },
        "source_digests": [
            ref.to_payload()
            for ref in _dedupe_source_refs(refs)
        ],
    }


def morning_index_source_fingerprint(source_manifest: Mapping[str, Any]) -> str:
    """Return the stable invalidation fingerprint for a source manifest."""

    manifest = _mapping(source_manifest)
    normalized = {
        "schema_version": manifest.get("schema_version") or MORNING_INDEX_SOURCE_VERSION,
        "policy_versions": _mapping(manifest.get("policy_versions")),
        "source_digests": sorted(
            [_mapping(item) for item in _sequence(manifest.get("source_digests"))],
            key=lambda item: (
                str(item.get("source_type") or ""),
                str(item.get("key") or ""),
                str(item.get("digest") or ""),
            ),
        ),
    }
    return _digest_payload(normalized)


def is_morning_index_current(
    index: MorningIndex | Mapping[str, Any],
    *,
    source_fingerprint: str | None = None,
    source_manifest: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether ``index`` still matches the supplied source state."""

    payload = index.to_payload() if isinstance(index, MorningIndex) else _mapping(index)
    current = source_fingerprint
    if source_manifest is not None:
        current = morning_index_source_fingerprint(source_manifest)
    return bool(payload.get("source_fingerprint") and current and payload.get("source_fingerprint") == current)


def morning_index_invalidated(
    index: MorningIndex | Mapping[str, Any],
    *,
    source_fingerprint: str | None = None,
    source_manifest: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether ``index`` should be regenerated for the supplied source state."""

    return not is_morning_index_current(
        index,
        source_fingerprint=source_fingerprint,
        source_manifest=source_manifest,
    )


def _repo_summary_hints(
    summaries: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> list[RepoSummaryHint]:
    hints = [
        RepoSummaryHint.from_summary(summary)
        for summary in summaries
        if _is_current_summary(summary)
    ]
    hints.sort(key=lambda item: (
        item.repo_root or "",
        item.summary_kind or "",
        item.scope_digest or "",
        item.summary_id or "",
    ))
    return hints[: max(0, limit)]


def _stale_memory_hints(
    memories: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> list[StaleMemoryHint]:
    hints = [
        hint for hint in (StaleMemoryHint.from_memory(memory) for memory in memories)
        if hint is not None
    ]
    hints.sort(key=lambda item: (item.memory_id, item.item_digest))
    return hints[: max(0, limit)]


def _skill_pin_hints(
    pins: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    max_per_task_class: int,
) -> list[SkillPinHint]:
    candidates = [
        hint
        for hint in _iter_skill_pin_hints(pins)
        if hint is not None
    ]
    candidates.sort(key=lambda item: (
        item.task_class,
        item.rank,
        -1.0 * (item.pin_strength if item.pin_strength is not None else 0.0),
        item.name or item.effective_digest or "",
    ))

    deduped: dict[tuple[str, str], SkillPinHint] = {}
    for candidate in candidates:
        deduped.setdefault(candidate.dedupe_key, candidate)

    grouped: dict[str, list[SkillPinHint]] = {}
    for candidate in deduped.values():
        bucket = grouped.setdefault(candidate.task_class, [])
        if len(bucket) < max(0, max_per_task_class):
            bucket.append(candidate)
    return [
        pin
        for task_class in sorted(grouped)
        for pin in grouped[task_class]
    ]


def _iter_skill_pin_hints(
    pins: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> list[SkillPinHint | None]:
    if isinstance(pins, Mapping):
        if any(key in pins for key in ("recommended_skills", "skill_pins", "pins")):
            task_class = _text(pins.get("task_class") or pins.get("scout_class"))
            items = pins.get("skill_pins") or pins.get("pins") or pins.get("recommended_skills")
            return [
                SkillPinHint.from_candidate(item, task_class=task_class, index=index)
                for index, item in enumerate(_mapping_items(items), start=1)
            ]

        hints: list[SkillPinHint | None] = []
        for task_class, items in sorted(pins.items(), key=lambda item: str(item[0])):
            if isinstance(items, Mapping):
                nested_items = items.get("skill_pins") or items.get("pins") or items.get("recommended_skills")
                source_items = _mapping_items(nested_items) if nested_items is not None else [items]
            else:
                source_items = _mapping_items(items)
            hints.extend(
                SkillPinHint.from_candidate(item, task_class=str(task_class), index=index)
                for index, item in enumerate(source_items, start=1)
            )
        return hints

    return [
        SkillPinHint.from_candidate(item, task_class=_text(item.get("task_class")), index=index)
        for index, item in enumerate(_mapping_items(pins), start=1)
    ]


def _review_item_hints(
    items: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> list[ReviewItemHint]:
    hints = [
        hint for hint in (ReviewItemHint.from_item(item) for item in items)
        if hint is not None
    ]
    hints.sort(key=lambda item: (
        item.created_at or "",
        item.review_id,
    ))
    return hints[: max(0, limit)]


def _resolve_context_policy(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return context_policy_runtime_hints()
    payload = dict(value)
    payload.setdefault("policy_version", context_policy_runtime_hints()["policy_version"])
    return _jsonable(payload)


def _resolve_skill_routing_policy(
    value: Mapping[str, Any] | SkillRoutingQualityPolicy | None,
) -> dict[str, Any]:
    if isinstance(value, SkillRoutingQualityPolicy):
        return skill_routing_policy_payload(value)
    if isinstance(value, Mapping):
        payload = dict(value)
        payload.setdefault("policy_version", skill_routing_policy_payload()["policy_version"])
        return _jsonable(payload)
    return skill_routing_policy_payload()


def _extra_source_refs(source_digests: Mapping[str, Any], *, scope_id: str) -> list[MorningSourceRef]:
    refs: list[MorningSourceRef] = []
    for key, value in sorted(source_digests.items(), key=lambda item: str(item[0])):
        if isinstance(value, Mapping):
            digest = _text(value.get("digest") or value.get("source_digest"))
            source_type = _text(value.get("source_type") or value.get("type")) or "external"
        else:
            digest = _text(value)
            source_type = "external"
        refs.append(MorningSourceRef(
            key=f"extra:{scope_id}:{key}",
            digest=digest,
            source_type=source_type,
        ))
    return refs


def _is_current_summary(summary: Mapping[str, Any]) -> bool:
    data = _mapping(summary)
    status = _normalized_token(data.get("lifecycle_status") or data.get("status"))
    return not status or status == "current"


def _summary_identity(data: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("summary_identity", "identity", "metadata", "source_metadata"):
        nested = data.get(key)
        if isinstance(nested, Mapping):
            return nested
    return {}


def _compact_repo_hot_path(value: Mapping[str, Any]) -> dict[str, Any]:
    data = _mapping(value)
    payload = {
        "schema_version": data.get("schema_version"),
        "repo_name": data.get("repo_name"),
        "repo_root": data.get("repo_root"),
        "branch": data.get("branch"),
        "commit_sha": data.get("commit_sha"),
        "summary_kind": data.get("summary_kind"),
        "path_globs": _text_list(data.get("path_globs"))[:20],
        "source_digest": data.get("source_digest"),
        "source_digest_complete": data.get("source_digest_complete"),
        "file_count": _int_or_none(data.get("file_count")),
        "byte_count": _int_or_none(data.get("byte_count")),
        "top_directories": _compact_mapping_list(data.get("top_directories"), limit=10),
        "extensions": _compact_mapping_list(data.get("extensions"), limit=10),
        "sample_paths": _text_list(data.get("sample_paths"))[:20],
        "largest_files": _compact_mapping_list(data.get("largest_files"), limit=5),
        "skipped_file_count": _int_or_none(data.get("skipped_file_count")),
        "unmatched_globs": _text_list(data.get("unmatched_globs"))[:20],
    }
    return _drop_none(payload)


def _freshness_map(data: Mapping[str, Any]) -> Mapping[str, Any]:
    source = data.get("source_freshness") or data.get("freshness")
    if isinstance(source, Mapping):
        return source
    if isinstance(source, str):
        return {"status": source}
    return {}


def _stale_memory_reasons(
    *,
    freshness_status: str | None,
    truth_status: str | None,
    staleness_score: float | None,
) -> list[str]:
    reasons: list[str] = []
    if freshness_status == "stale":
        reasons.append("source_freshness_stale")
    if staleness_score is not None and staleness_score >= 0.85:
        reasons.append("staleness_score_high")
    if truth_status in _INACTIVE_TRUTH_STATUSES:
        reasons.append(f"truth_status_{truth_status}")
    return reasons


def _is_unresolved_high_risk(
    *,
    risk_level: str | None,
    severity: str | None,
    status: str | None,
    item: Mapping[str, Any],
) -> bool:
    if item.get("resolved_at"):
        return False
    high_risk = (
        bool(item.get("high_risk"))
        or risk_level in _HIGH_RISK_VALUES
        or severity in _HIGH_RISK_VALUES
    )
    if not high_risk:
        return False
    return status not in _RESOLVED_REVIEW_STATUSES


def _skill_pin_payload_by_task(pins: Sequence[SkillPinHint]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for pin in pins:
        grouped.setdefault(pin.task_class, []).append(pin.to_payload())
    return {task_class: grouped[task_class] for task_class in sorted(grouped)}


def _index_stats(scopes: Sequence[MorningScopeHints]) -> dict[str, int]:
    return {
        "scope_count": len(scopes),
        "repo_summary_count": sum(len(scope.repo_summaries) for scope in scopes),
        "known_stale_memory_count": sum(len(scope.stale_memories) for scope in scopes),
        "skill_pin_count": sum(len(scope.skill_pins) for scope in scopes),
        "unresolved_high_risk_review_count": sum(len(scope.unresolved_review_items) for scope in scopes),
        "source_digest_count": sum(len(scope.source_refs()) for scope in scopes),
    }


def _dedupe_source_refs(refs: Sequence[MorningSourceRef]) -> list[MorningSourceRef]:
    deduped: dict[tuple[str, str], MorningSourceRef] = {}
    for ref in refs:
        deduped[(ref.source_type, ref.key)] = ref
    return [
        deduped[key]
        for key in sorted(deduped)
    ]


def _mapping(value: Any) -> Mapping[str, Any]:
    return _shared_mapping_view(value)


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    return [item for item in _sequence(value) if isinstance(item, Mapping)]


def _compact_mapping_list(value: Any, *, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in _mapping_items(value):
        compact = {
            key: item.get(key)
            for key in ("name", "extension", "files", "path", "size_bytes", "count")
            if item.get(key) is not None
        }
        if compact:
            items.append(_jsonable(compact))
        if len(items) >= limit:
            break
    return items


def _compact_reason_list(*values: Any, limit: int = 5) -> list[str]:
    reasons: list[str] = []
    for value in values:
        raw_items = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value]
        for item in raw_items:
            text = _text(item)
            if not text:
                continue
            text = text.replace("\n", " ").strip()
            if len(text) > 96:
                text = text[:93].rstrip() + "..."
            if text not in reasons:
                reasons.append(text)
            if len(reasons) >= limit:
                return reasons
    return reasons


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = [value]
    else:
        raw_items = _sequence(value)
    return [text for text in (_text(item) for item in raw_items) if text]


def _text(value: Any) -> str | None:
    return _shared_optional_text(value)


def _normalized_token(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    return text.lower().replace("-", "_").replace(" ", "_")


def _first_float(*values: Any) -> float | None:
    for value in values:
        coerced = _float_or_none(value)
        if coerced is not None:
            return coerced
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def _round_or_none(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(max(0.0, min(1.0, float(value))), 6)


def _int_or_none(value: Any) -> int | None:
    return _shared_int_or_none(value)


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _isoformat(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _drop_none(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _shared_drop_none(payload)


def _jsonable(value: Any) -> Any:
    return _shared_jsonable(value)


def _digest_payload(payload: Mapping[str, Any], *, length: int | None = None) -> str:
    encoded = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    if length is not None:
        digest = digest[:length]
    return f"sha256:{digest}"


__all__ = [
    "MORNING_INDEX_SCHEMA_VERSION",
    "MORNING_INDEX_SOURCE_VERSION",
    "MorningIndex",
    "MorningIndexScopeInput",
    "MorningScopeHints",
    "MorningSourceRef",
    "RepoSummaryHint",
    "ReviewItemHint",
    "SkillPinHint",
    "StaleMemoryHint",
    "build_morning_index",
    "build_morning_scope_hints",
    "build_morning_source_manifest",
    "is_morning_index_current",
    "morning_index_invalidated",
    "morning_index_source_fingerprint",
]
