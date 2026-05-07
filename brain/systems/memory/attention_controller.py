"""Attention controller — shadow ranking and retrieval decision logging.

This module centralizes retrieval scoring for observe-first logging without
changing the active retrieval result ordering by default.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterable, Sequence

from sqlalchemy import select

from brain.platform.db.models.memory import Memory
from brain.platform.db.models.memory_dag import MemorySummary
from brain.platform.db.repositories.memory_visibility import MemoryVisibilityContext, memory_is_visible
from brain.platform.db.models.system import RetrievalDecision, RetrievalItemFeedback
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)

_DEFAULT_MODE = os.getenv("ATTENTION_CONTROLLER_MODE", "shadow").strip().lower() or "shadow"
_DEFAULT_POLICY_VERSION = os.getenv("ATTENTION_POLICY_VERSION", "shadow-v1").strip() or "shadow-v1"
_DEFAULT_PRELOAD_ITEM_LIMIT = max(1, int(os.getenv("ATTENTION_PRELOAD_ITEM_LIMIT", "3")))
_CONTROLLER_ENABLED = os.getenv("ATTENTION_CONTROLLER_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
_DEBUG_EXPLAIN_ENABLED = os.getenv("ATTENTION_DEBUG_EXPLAIN_ENABLED", "0").strip().lower() not in {
    "0",
    "false",
    "no",
}
_USEFULNESS_WRITE_ENABLED = os.getenv("ATTENTION_USEFULNESS_WRITE_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
_SERVICE_USER_IDS = {"system", "service:internal-api"}


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_query(query_text: str | None) -> str:
    if not query_text:
        return ""
    return re.sub(r"\s+", " ", query_text).strip().lower()


def _query_fingerprint(query_text: str | None) -> str:
    normalized = _normalize_query(query_text)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _coerce_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _attention_visibility_context(
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    service_retrieval: bool = False,
) -> MemoryVisibilityContext:
    """Build the tenant context for attention decisions and feedback."""
    user_id = _coerce_text(user_id)
    org_id = _coerce_text(org_id)
    if service_retrieval or user_id in _SERVICE_USER_IDS:
        return MemoryVisibilityContext(
            user_id=user_id or "system",
            org_id=org_id,
            allow_global=True,
            principal_type="service",
        )
    if not user_id:
        raise ValueError(
            "Attention retrieval requires user_id; pass service_retrieval=True for explicit service/system retrieval"
        )
    return MemoryVisibilityContext(user_id=user_id, org_id=org_id)


def _tenant_context_payload(context: MemoryVisibilityContext) -> dict[str, Any]:
    return {
        "user_id": context.user_id,
        "org_id": context.org_id,
        "service_retrieval": bool(context.allow_global),
        "principal_type": context.principal_type,
    }


def _candidate_has_tenant_scope(candidate: dict[str, Any]) -> bool:
    return any(
        _coerce_text(candidate.get(key)) is not None
        for key in ("user_id", "org_id", "owner_user_id", "owner_org_id")
    )


def _candidate_is_visible(candidate: dict[str, Any], context: MemoryVisibilityContext) -> bool:
    if context.allow_global or not _candidate_has_tenant_scope(candidate):
        return True
    return memory_is_visible(
        SimpleNamespace(
            user_id=_coerce_text(candidate.get("user_id") or candidate.get("owner_user_id")),
            org_id=_coerce_text(candidate.get("org_id") or candidate.get("owner_org_id")),
            visibility=_coerce_text(candidate.get("visibility")) or "private",
        ),
        context,
    )


def _filter_visible_candidates(
    candidates: Sequence[dict[str, Any]],
    context: MemoryVisibilityContext,
) -> tuple[list[dict[str, Any]], int]:
    visible: list[dict[str, Any]] = []
    hidden_count = 0
    for candidate in candidates:
        if _candidate_is_visible(candidate, context):
            visible.append(candidate)
        else:
            hidden_count += 1
    return visible, hidden_count


def _candidate_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    payload = dict(candidate)
    payload.pop("_attention_score", None)
    payload.pop("_attention_tie_breaker", None)
    return payload


def _extract_score(candidate: dict[str, Any], *paths: tuple[str, ...], default: float = 0.0) -> float:
    for path in paths:
        current: Any = candidate
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
            if current is None:
                break
        if current is not None:
            return _coerce_float(current, default)
    return default


def _freshness_score(candidate: dict[str, Any]) -> float:
    timestamp = candidate.get("last_accessed") or candidate.get("created_at")
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            timestamp = None
    if not isinstance(timestamp, datetime):
        return 0.5
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds() / 86_400)
    return math.exp(-0.02 * age_days)


def _novelty_score(candidate: dict[str, Any]) -> float:
    access_count = candidate.get("access_count")
    if access_count is None:
        return 0.5
    try:
        return 1.0 / (1.0 + max(0.0, float(access_count)))
    except (TypeError, ValueError):
        return 0.5


def _candidate_identity(candidate: dict[str, Any]) -> tuple[str, int | None]:
    item_id = candidate.get("id")
    if item_id is None:
        item_id = candidate.get("memory_id") or candidate.get("summary_id")
    try:
        item_id = int(item_id) if item_id is not None else None
    except (TypeError, ValueError):
        item_id = None
    source = (
        candidate.get("candidate_source")
        or candidate.get("_pool")
        or candidate.get("source")
        or candidate.get("retrieval_mode")
        or candidate.get("type")
        or "memory"
    )
    return str(source), item_id


def _candidate_lookup_key(candidate: dict[str, Any]) -> tuple[str, int | None]:
    return _candidate_identity(candidate)


def _is_lazy_load_eligible(candidate: "AttentionCandidate", *, stage: str) -> bool:
    if candidate.selected_key is None:
        return False
    if candidate.attention_score < 0.32 and candidate.omission_risk_score < 0.42:
        return False
    if stage == "frame_assembly":
        return candidate.semantic_score >= 0.25 or candidate.omission_risk_score >= 0.45
    return candidate.semantic_score >= 0.35 or candidate.omission_risk_score >= 0.4


def _select_lazy_candidates(
    ranked: Sequence["AttentionCandidate"],
    *,
    stage: str,
    selected_count: int,
    lazy_budget_tokens: int,
) -> list["AttentionCandidate"]:
    if lazy_budget_tokens <= 0:
        return []

    lazy_cap = max(1, min(len(ranked), math.ceil(lazy_budget_tokens / 120)))
    lazy_candidates: list[AttentionCandidate] = []
    for candidate in ranked[selected_count:]:
        if _is_lazy_load_eligible(candidate, stage=stage):
            lazy_candidates.append(candidate)
        if len(lazy_candidates) >= lazy_cap:
            break
    return lazy_candidates


def _decision_debug_payload(
    *,
    decision: "AttentionDecision",
    ranked: Sequence["AttentionCandidate"],
    selected: Sequence["AttentionCandidate"],
    lazy_candidates: Sequence["AttentionCandidate"],
    fallback_used: bool,
    fallback_reason: str | None,
    tenant_context: dict[str, Any],
    visibility_suppressed_count: int,
) -> dict[str, Any]:
    selected_keys = {item.selected_key for item in selected if item.selected_key is not None}
    lazy_keys = {item.selected_key for item in lazy_candidates if item.selected_key is not None}

    return {
        "summary": {
            "stage": decision.stage,
            "policy_version": decision.policy_version,
            "mode": decision.mode,
            "tenant_context": tenant_context,
            "candidate_count": decision.candidate_count,
            "selected_count": len(selected),
            "suppressed_count": max(0, len(ranked) - len(selected)),
            "lazy_load_count": len(lazy_candidates),
            "visibility_suppressed_count": visibility_suppressed_count,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
        },
        "candidates": [
            {
                "item_id": candidate.selected_key,
                "candidate_source": candidate.candidate_source,
                "item_kind": candidate.item_kind,
                "attention_score": candidate.attention_score,
                "semantic_score": candidate.semantic_score,
                "stage_score": candidate.stage_score,
                "freshness_score": candidate.freshness_score,
                "novelty_score": candidate.novelty_score,
                "prior_usefulness_score": candidate.prior_usefulness_score,
                "contradiction_score": candidate.contradiction_score,
                "omission_risk_score": candidate.omission_risk_score,
                "decision": (
                    "preload"
                    if candidate.selected_key in selected_keys
                    else "lazy-load"
                    if candidate.selected_key in lazy_keys
                    else "suppress"
                ),
            }
            for candidate in ranked
        ],
    }


def _decision_rationale_payload(
    *,
    decision: "AttentionDecision",
    tenant_context: dict[str, Any],
    visibility_suppressed_count: int,
) -> dict[str, Any]:
    return {
        "tenant_context": tenant_context,
        "rationale": {
            "policy_version": decision.policy_version,
            "mode": decision.mode,
            "selected_item_ids": list(decision.selected_item_ids),
            "suppressed_item_ids": list(decision.suppressed_item_ids),
            "lazy_load_item_ids": list(decision.lazy_load_item_ids),
            "visibility_suppressed_count": visibility_suppressed_count,
        },
    }


@dataclass(frozen=True)
class AttentionCandidate:
    """Normalized candidate used by the attention controller."""

    candidate_source: str
    item_id: int | None
    summary_id: int | None
    item_kind: str
    semantic_score: float
    stage_score: float
    freshness_score: float
    novelty_score: float
    prior_usefulness_score: float
    contradiction_score: float
    omission_risk_score: float
    attention_score: float
    tie_breaker: str
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def selected_key(self) -> int | None:
        return self.item_id if self.item_id is not None else self.summary_id

    def to_feedback_kwargs(
        self,
        *,
        retrieval_decision_id: int,
        user_id: str | None,
        org_id: str | None,
        preload_decision: bool,
        lazy_load_eligible: bool,
    ) -> dict[str, Any]:
        return {
            "retrieval_decision_id": retrieval_decision_id,
            "user_id": user_id,
            "org_id": org_id,
            "memory_id": self.item_id if self.item_kind == "memory" else None,
            "summary_id": self.summary_id if self.item_kind == "summary" else None,
            "candidate_source": self.candidate_source,
            "semantic_score": self.semantic_score,
            "stage_score": self.stage_score,
            "freshness_score": self.freshness_score,
            "novelty_score": self.novelty_score,
            "prior_usefulness_score": self.prior_usefulness_score,
            "contradiction_score": self.contradiction_score,
            "omission_risk_score": self.omission_risk_score,
            "preload_decision": preload_decision,
            "lazy_load_eligible": lazy_load_eligible,
            "lazy_loaded": False,
            "actually_used": False,
            "cited_in_output": False,
            "correlated_with_success": False,
        }


@dataclass(frozen=True)
class AttentionSelection:
    """Materialized selection produced from a logged attention decision."""

    decision: AttentionDecision
    selected: list[dict[str, Any]]
    suppressed: list[dict[str, Any]]
    lazy_load_eligible: list[dict[str, Any]]
    fallback_used: bool
    fallback_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "selected": list(self.selected),
            "suppressed": list(self.suppressed),
            "lazy_load_eligible": list(self.lazy_load_eligible),
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
        }


@dataclass(frozen=True)
class AttentionDecision:
    """A logged retrieval decision."""

    stage: str
    query_text: str
    query_fingerprint: str
    policy_version: str
    mode: str
    user_id: str | None
    org_id: str | None
    service_retrieval: bool
    preload_budget_tokens: int
    lazy_budget_tokens: int
    selected_item_ids: list[int]
    suppressed_item_ids: list[int]
    lazy_load_item_ids: list[int]
    omission_risk_score: float
    contradiction_risk_score: float
    candidate_count: int
    fallback_used: bool = False
    fallback_reason: str | None = None
    debug: dict[str, Any] | None = None
    retrieval_decision_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "retrieval_decision_id": self.retrieval_decision_id,
            "stage": self.stage,
            "query_text": self.query_text,
            "query_fingerprint": self.query_fingerprint,
            "policy_version": self.policy_version,
            "mode": self.mode,
            "user_id": self.user_id,
            "org_id": self.org_id,
            "service_retrieval": self.service_retrieval,
            "tenant_context": {
                "user_id": self.user_id,
                "org_id": self.org_id,
                "service_retrieval": self.service_retrieval,
            },
            "preload_budget_tokens": self.preload_budget_tokens,
            "lazy_budget_tokens": self.lazy_budget_tokens,
            "selected_item_ids": list(self.selected_item_ids),
            "suppressed_item_ids": list(self.suppressed_item_ids),
            "lazy_load_item_ids": list(self.lazy_load_item_ids),
            "omission_risk_score": self.omission_risk_score,
            "contradiction_risk_score": self.contradiction_risk_score,
            "candidate_count": self.candidate_count,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "debug": self.debug,
        }


class AttentionController:
    """Shadow-first attention controller for retrieval and frame assembly."""

    def __init__(
        self,
        *,
        mode: str | None = None,
        policy_version: str | None = None,
        preload_item_limit: int | None = None,
        enabled: bool | None = None,
        usefulness_write_enabled: bool | None = None,
    ) -> None:
        self.mode = (mode or _DEFAULT_MODE).strip().lower() or _DEFAULT_MODE
        self.policy_version = (policy_version or _DEFAULT_POLICY_VERSION).strip() or _DEFAULT_POLICY_VERSION
        self.preload_item_limit = max(1, int(preload_item_limit or _DEFAULT_PRELOAD_ITEM_LIMIT))
        self.enabled = _CONTROLLER_ENABLED if enabled is None else enabled
        self.usefulness_write_enabled = (
            _USEFULNESS_WRITE_ENABLED if usefulness_write_enabled is None else usefulness_write_enabled
        )

    def normalize_candidates(self, candidates: Sequence[dict[str, Any]], *, stage: str) -> list[AttentionCandidate]:
        normalized: list[AttentionCandidate] = []
        for candidate in candidates:
            source, item_id = _candidate_identity(candidate)
            summary_id = candidate.get("summary_id")
            try:
                summary_id = int(summary_id) if summary_id is not None else None
            except (TypeError, ValueError):
                summary_id = None

            item_kind = "summary" if summary_id is not None and item_id is None else "memory"
            if candidate.get("candidate_kind") in {"summary", "memory", "narrative"}:
                item_kind = str(candidate["candidate_kind"])

            semantic_score = _extract_score(
                candidate,
                ("combined_score",),
                ("score",),
                ("similarity",),
                ("scores", "combined"),
                ("scores", "semantic"),
                default=0.0,
            )
            stage_score = _extract_score(
                candidate,
                ("stage_score",),
                ("recency_score",),
                ("scores", "recency"),
                default=_coerce_float(candidate.get("salience"), 0.0) / 10.0,
            )
            freshness_score = _extract_score(candidate, ("freshness_score",), default=_freshness_score(candidate))
            novelty_score = _extract_score(candidate, ("novelty_score",), default=_novelty_score(candidate))
            prior_usefulness_score = _extract_score(
                candidate,
                ("prior_usefulness_score",),
                ("usefulness_score",),
                default=min(1.0, max(0.0, _coerce_float(candidate.get("salience"), 0.0) / 10.0)),
            )
            contradiction_score = _extract_score(
                candidate,
                ("contradiction_score",),
                ("scores", "contradiction"),
                default=0.0,
            )
            omission_risk_score = _extract_score(
                candidate,
                ("omission_risk_score",),
                default=min(1.0, (semantic_score * 0.45) + (stage_score * 0.2) + (prior_usefulness_score * 0.35)),
            )

            stage_weight = 0.24 if stage == "frame_assembly" else 0.18
            attention_score = (
                semantic_score * 0.42
                + stage_score * stage_weight
                + freshness_score * 0.10
                + novelty_score * 0.08
                + prior_usefulness_score * 0.12
                + omission_risk_score * 0.10
                - contradiction_score * 0.10
            )
            attention_score = max(0.0, min(1.0, round(attention_score, 6)))

            tie_breaker = json.dumps(
                {
                    "source": source,
                    "item_id": item_id,
                    "summary_id": summary_id,
                    "content": candidate.get("content") or candidate.get("arc_summary") or "",
                },
                sort_keys=True,
                default=_json_default,
            )

            normalized.append(
                AttentionCandidate(
                    candidate_source=source,
                    item_id=item_id,
                    summary_id=summary_id,
                    item_kind=item_kind,
                    semantic_score=round(semantic_score, 6),
                    stage_score=round(stage_score, 6),
                    freshness_score=round(freshness_score, 6),
                    novelty_score=round(novelty_score, 6),
                    prior_usefulness_score=round(prior_usefulness_score, 6),
                    contradiction_score=round(contradiction_score, 6),
                    omission_risk_score=round(omission_risk_score, 6),
                    attention_score=attention_score,
                    tie_breaker=tie_breaker,
                    raw=_candidate_payload(candidate),
                )
            )

        return normalized

    def rank_candidates(self, candidates: Sequence[dict[str, Any]], *, stage: str) -> list[AttentionCandidate]:
        normalized = self.normalize_candidates(candidates, stage=stage)
        return sorted(
            normalized,
            key=lambda item: (
                -item.attention_score,
                -item.omission_risk_score,
                -item.semantic_score,
                -item.stage_score,
                -item.freshness_score,
                item.candidate_source,
                item.item_kind,
                item.selected_key if item.selected_key is not None else math.inf,
                item.tie_breaker,
            ),
        )

    def evaluate(
        self,
        *,
        stage: str,
        query_text: str,
        candidates: Sequence[dict[str, Any]],
        user_id: str | None = None,
        org_id: str | None = None,
        service_retrieval: bool = False,
        preload_budget_tokens: int = 0,
        lazy_budget_tokens: int = 0,
        selected_limit: int | None = None,
    ) -> tuple[AttentionDecision, list[AttentionCandidate], list[AttentionCandidate], list[AttentionCandidate]]:
        visibility_context = _attention_visibility_context(
            user_id=user_id,
            org_id=org_id,
            service_retrieval=service_retrieval,
        )
        scoped_candidates, visibility_suppressed_count = _filter_visible_candidates(
            candidates,
            visibility_context,
        )
        ranked = self.rank_candidates(scoped_candidates, stage=stage)
        selection_limit = selected_limit if selected_limit is not None else self.preload_item_limit
        selection_limit = max(0, min(len(ranked), selection_limit))

        selected = ranked[:selection_limit]
        lazy_candidates = _select_lazy_candidates(
            ranked,
            stage=stage,
            selected_count=selection_limit,
            lazy_budget_tokens=lazy_budget_tokens,
        )
        suppressed = ranked[selection_limit:]
        fallback_used = bool(ranked) and not selected and selection_limit == 0
        fallback_reason = "empty_selection" if fallback_used else None

        selected_item_ids = [item.selected_key for item in selected if item.selected_key is not None]
        suppressed_item_ids = [item.selected_key for item in suppressed if item.selected_key is not None]
        lazy_load_item_ids = [item.selected_key for item in lazy_candidates if item.selected_key is not None]
        omission_risk_score = max((item.omission_risk_score for item in ranked), default=0.0)
        contradiction_risk_score = max((item.contradiction_score for item in ranked), default=0.0)

        decision = AttentionDecision(
            stage=stage,
            query_text=query_text,
            query_fingerprint=_query_fingerprint(query_text),
            policy_version=self.policy_version,
            mode=self.mode,
            user_id=visibility_context.user_id,
            org_id=visibility_context.org_id,
            service_retrieval=bool(visibility_context.allow_global),
            preload_budget_tokens=max(0, int(preload_budget_tokens)),
            lazy_budget_tokens=max(0, int(lazy_budget_tokens)),
            selected_item_ids=[int(item_id) for item_id in selected_item_ids],
            suppressed_item_ids=[int(item_id) for item_id in suppressed_item_ids],
            lazy_load_item_ids=[int(item_id) for item_id in lazy_load_item_ids],
            omission_risk_score=round(omission_risk_score, 6),
            contradiction_risk_score=round(contradiction_risk_score, 6),
            candidate_count=len(ranked),
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )
        decision = AttentionDecision(**{
            **asdict(decision),
            "debug": _decision_rationale_payload(
                decision=decision,
                tenant_context=_tenant_context_payload(visibility_context),
                visibility_suppressed_count=visibility_suppressed_count,
            ),
        })
        return decision, ranked, selected, lazy_candidates

    def materialize_selection(
        self,
        candidates: Sequence[dict[str, Any]],
        decision: AttentionDecision | dict[str, Any],
    ) -> AttentionSelection:
        if isinstance(decision, AttentionDecision):
            decision_dict = decision.to_dict()
        else:
            decision_dict = dict(decision)

        tenant_context = decision_dict.get("tenant_context") or {}
        tenant_user_id = decision_dict.get("user_id") or tenant_context.get("user_id")
        tenant_org_id = decision_dict.get("org_id") or tenant_context.get("org_id")
        service_retrieval = bool(
            decision_dict.get("service_retrieval")
            or tenant_context.get("service_retrieval", False)
        )
        if tenant_user_id or service_retrieval:
            visibility_context = _attention_visibility_context(
                user_id=tenant_user_id,
                org_id=tenant_org_id,
                service_retrieval=service_retrieval,
            )
            candidates, _ = _filter_visible_candidates(candidates, visibility_context)

        selected_ids = [int(item_id) for item_id in decision_dict.get("selected_item_ids", []) if item_id is not None]
        suppressed_ids = [int(item_id) for item_id in decision_dict.get("suppressed_item_ids", []) if item_id is not None]
        lazy_ids = [int(item_id) for item_id in decision_dict.get("lazy_load_item_ids", []) if item_id is not None]

        id_map: dict[int, dict[str, Any]] = {}
        for candidate in candidates:
            _, candidate_id = _candidate_identity(candidate)
            if candidate_id is not None and candidate_id not in id_map:
                id_map[candidate_id] = candidate

        selected = [id_map[item_id] for item_id in selected_ids if item_id in id_map]
        if not selected and candidates:
            selected = list(candidates)
            selected_ids = [candidate_id for _, candidate_id in (_candidate_identity(c) for c in candidates) if candidate_id is not None]
            suppressed_ids = []
            lazy_ids = []

        lazy_load_eligible = [id_map[item_id] for item_id in lazy_ids if item_id in id_map and id_map[item_id] not in selected]
        if not lazy_load_eligible:
            lazy_load_eligible = [candidate for candidate in candidates if _candidate_identity(candidate)[1] in lazy_ids and candidate not in selected]

        suppressed = [candidate for candidate in candidates if _candidate_identity(candidate)[1] in suppressed_ids]
        if not suppressed:
            suppressed = [candidate for candidate in candidates if candidate not in selected and candidate not in lazy_load_eligible]

        fallback_used = bool(decision_dict.get("fallback_used", False)) or (bool(candidates) and not decision_dict.get("selected_item_ids"))
        fallback_reason = decision_dict.get("fallback_reason")
        if not selected and candidates:
            fallback_used = True
            fallback_reason = fallback_reason or "selection_missing"

        decision_kwargs = {
            "stage": decision_dict.get("stage", "memory_query"),
            "query_text": decision_dict.get("query_text", ""),
            "query_fingerprint": decision_dict.get("query_fingerprint", _query_fingerprint(decision_dict.get("query_text", ""))),
            "policy_version": decision_dict.get("policy_version", self.policy_version),
            "mode": decision_dict.get("mode", self.mode),
            "user_id": tenant_user_id,
            "org_id": tenant_org_id,
            "service_retrieval": service_retrieval,
            "preload_budget_tokens": int(decision_dict.get("preload_budget_tokens", 0) or 0),
            "lazy_budget_tokens": int(decision_dict.get("lazy_budget_tokens", 0) or 0),
            "selected_item_ids": selected_ids,
            "suppressed_item_ids": suppressed_ids,
            "lazy_load_item_ids": lazy_ids,
            "omission_risk_score": float(decision_dict.get("omission_risk_score", 0.0) or 0.0),
            "contradiction_risk_score": float(decision_dict.get("contradiction_risk_score", 0.0) or 0.0),
            "candidate_count": int(decision_dict.get("candidate_count", len(candidates)) or len(candidates)),
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            "debug": decision_dict.get("debug"),
            "retrieval_decision_id": decision_dict.get("retrieval_decision_id"),
        }

        return AttentionSelection(
            decision=AttentionDecision(**decision_kwargs),
            selected=selected,
            suppressed=suppressed,
            lazy_load_eligible=lazy_load_eligible,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )

    def observe(
        self,
        *,
        stage: str,
        query_text: str,
        candidates: Sequence[dict[str, Any]],
        user_id: str | None = None,
        org_id: str | None = None,
        service_retrieval: bool = False,
        run_id: int | None = None,
        preload_budget_tokens: int = 0,
        lazy_budget_tokens: int = 0,
        selected_limit: int | None = None,
    ) -> AttentionDecision:
        decision, ranked, selected, lazy_candidates = self.evaluate(
            stage=stage,
            query_text=query_text,
            candidates=candidates,
            user_id=user_id,
            org_id=org_id,
            service_retrieval=service_retrieval,
            preload_budget_tokens=preload_budget_tokens,
            lazy_budget_tokens=lazy_budget_tokens,
            selected_limit=selected_limit,
        )
        if _DEBUG_EXPLAIN_ENABLED:
            visibility_suppressed_count = int(
                ((decision.debug or {}).get("rationale") or {}).get("visibility_suppressed_count", 0)
            )
            decision = AttentionDecision(**{**asdict(decision), "debug": _decision_debug_payload(
                decision=decision,
                ranked=ranked,
                selected=selected,
                lazy_candidates=lazy_candidates,
                fallback_used=decision.fallback_used,
                fallback_reason=decision.fallback_reason,
                tenant_context=(decision.debug or {}).get("tenant_context", {}),
                visibility_suppressed_count=visibility_suppressed_count,
            )})

        if self.enabled:
            try:
                decision = self._persist_decision(decision, ranked, run_id=run_id)
            except Exception as exc:  # pragma: no cover - defensive shadow logging
                logger.debug("Attention decision logging failed: %s", exc)

        return decision

    def _persist_decision(
        self,
        decision: AttentionDecision,
        ranked: Sequence[AttentionCandidate],
        *,
        run_id: int | None = None,
    ) -> AttentionDecision:
        with UnitOfWork() as uow:
            decision_row = RetrievalDecision(
                run_id=run_id,
                stage=decision.stage,
                query_text=decision.query_text,
                query_fingerprint=decision.query_fingerprint,
                policy_version=decision.policy_version,
                mode=decision.mode,
                user_id=decision.user_id,
                org_id=decision.org_id,
                preload_budget_tokens=decision.preload_budget_tokens,
                lazy_budget_tokens=decision.lazy_budget_tokens,
                selected_item_ids=decision.selected_item_ids,
                suppressed_item_ids=decision.suppressed_item_ids,
                omission_risk_score=decision.omission_risk_score,
                contradiction_risk_score=decision.contradiction_risk_score,
                candidate_count=decision.candidate_count,
                decision_debug=decision.debug or {},
            )
            uow.session.add(decision_row)
            uow.session.flush()

            if self.usefulness_write_enabled:
                selected_keys = set(decision.selected_item_ids)
                lazy_keys = set(decision.lazy_load_item_ids)
                for candidate in ranked:
                    payload = candidate.to_feedback_kwargs(
                        retrieval_decision_id=decision_row.id,
                        user_id=decision.user_id,
                        org_id=decision.org_id,
                        preload_decision=candidate.selected_key in selected_keys,
                        lazy_load_eligible=candidate.selected_key in lazy_keys,
                    )
                    row = RetrievalItemFeedback(**payload)
                    uow.session.add(row)
                uow.session.flush()

            return AttentionDecision(
                **{
                    **asdict(decision),
                    "retrieval_decision_id": decision_row.id,
                }
            )

    def record_usefulness(
        self,
        *,
        retrieval_decision_id: int,
        user_id: str | None = None,
        org_id: str | None = None,
        service_retrieval: bool = False,
        item_id: int | None = None,
        summary_id: int | None = None,
        actually_used: bool | None = None,
        cited_in_output: bool | None = None,
        correlated_with_success: bool | None = None,
        lazy_loaded: bool | None = None,
        retry_delta: int | None = None,
        verifier_helped: bool | None = None,
        user_feedback_signal: str | None = None,
    ) -> bool:
        if not self.usefulness_write_enabled or not self.enabled:
            return False
        visibility_context = _attention_visibility_context(
            user_id=user_id,
            org_id=org_id,
            service_retrieval=service_retrieval,
        )

        with UnitOfWork() as uow:
            stmt = select(RetrievalItemFeedback).where(
                RetrievalItemFeedback.retrieval_decision_id == retrieval_decision_id,
            )
            if not visibility_context.allow_global:
                stmt = stmt.where(RetrievalItemFeedback.user_id == visibility_context.user_id)
                if visibility_context.org_id is not None:
                    stmt = stmt.where(RetrievalItemFeedback.org_id == visibility_context.org_id)
                else:
                    stmt = stmt.where(RetrievalItemFeedback.org_id.is_(None))
            if item_id is not None:
                stmt = stmt.where(RetrievalItemFeedback.memory_id == item_id)
            if summary_id is not None:
                stmt = stmt.where(RetrievalItemFeedback.summary_id == summary_id)

            row = uow.session.scalars(stmt.order_by(RetrievalItemFeedback.id.asc())).first()
            if row is None:
                return False

            if actually_used is not None:
                row.actually_used = actually_used
            if cited_in_output is not None:
                row.cited_in_output = cited_in_output
            if correlated_with_success is not None:
                row.correlated_with_success = correlated_with_success
            if lazy_loaded is not None:
                row.lazy_loaded = lazy_loaded
            if retry_delta is not None:
                row.retry_delta = retry_delta
            if verifier_helped is not None:
                row.verifier_helped = verifier_helped
            if user_feedback_signal is not None:
                row.user_feedback_signal = user_feedback_signal
            row.feedback_at = datetime.now(timezone.utc)
            return True

    def record_lazy_load(
        self,
        *,
        retrieval_decision_id: int,
        user_id: str | None = None,
        org_id: str | None = None,
        service_retrieval: bool = False,
        item_id: int | None = None,
        summary_id: int | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        visibility_context = _attention_visibility_context(
            user_id=user_id,
            org_id=org_id,
            service_retrieval=service_retrieval,
        )

        with UnitOfWork() as uow:
            stmt = select(RetrievalItemFeedback).where(
                RetrievalItemFeedback.retrieval_decision_id == retrieval_decision_id,
            )
            if not visibility_context.allow_global:
                stmt = stmt.where(RetrievalItemFeedback.user_id == visibility_context.user_id)
                if visibility_context.org_id is not None:
                    stmt = stmt.where(RetrievalItemFeedback.org_id == visibility_context.org_id)
                else:
                    stmt = stmt.where(RetrievalItemFeedback.org_id.is_(None))
            if item_id is not None:
                stmt = stmt.where(RetrievalItemFeedback.memory_id == item_id)
            if summary_id is not None:
                stmt = stmt.where(RetrievalItemFeedback.summary_id == summary_id)
            row = uow.session.scalars(stmt.order_by(RetrievalItemFeedback.id.asc())).first()
            if row is None:
                return False
            row.lazy_loaded = True
            row.feedback_at = datetime.now(timezone.utc)
            return True

    def load_lazy_candidates(
        self,
        *,
        retrieval_decision_id: int,
        user_id: str | None = None,
        org_id: str | None = None,
        service_retrieval: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch deferred candidates for a prior decision and mark them loaded."""
        if not self.enabled:
            return []
        visibility_context = _attention_visibility_context(
            user_id=user_id,
            org_id=org_id,
            service_retrieval=service_retrieval,
        )

        with UnitOfWork() as uow:
            stmt = select(RetrievalItemFeedback).where(
                RetrievalItemFeedback.retrieval_decision_id == retrieval_decision_id,
                RetrievalItemFeedback.lazy_load_eligible.is_(True),
                RetrievalItemFeedback.lazy_loaded.is_(False),
            ).order_by(RetrievalItemFeedback.id.asc())
            if not visibility_context.allow_global:
                stmt = stmt.where(RetrievalItemFeedback.user_id == visibility_context.user_id)
                if visibility_context.org_id is not None:
                    stmt = stmt.where(RetrievalItemFeedback.org_id == visibility_context.org_id)
                else:
                    stmt = stmt.where(RetrievalItemFeedback.org_id.is_(None))
            feedback_rows = list(uow.session.scalars(stmt).all())
            if limit is not None:
                feedback_rows = feedback_rows[: max(0, int(limit))]

            loaded: list[dict[str, Any]] = []
            for row in feedback_rows:
                payload: dict[str, Any] | None = None
                if row.memory_id is not None:
                    mem = uow.session.get(Memory, row.memory_id)
                    if mem is not None and memory_is_visible(mem, visibility_context):
                        payload = {
                            "id": mem.id,
                            "content": mem.content[:300],
                            "type": mem.memory_type,
                            "tier": getattr(mem, "memory_tier", "episodic") or "episodic",
                            "salience": float(mem.salience) if mem.salience is not None else 0.0,
                            "visibility": getattr(mem, "visibility", "private") or "private",
                            "lazy_loaded": True,
                            "retrieval_decision_id": retrieval_decision_id,
                            "tenant_context": _tenant_context_payload(visibility_context),
                        }
                elif row.summary_id is not None:
                    summary = uow.session.get(MemorySummary, row.summary_id)
                    if summary is not None and memory_is_visible(summary, visibility_context):
                        payload = {
                            "id": summary.id,
                            "content": summary.content[:300],
                            "type": "summary",
                            "tier": "semantic" if summary.depth == 0 else "summary",
                            "salience": float(summary.descendant_count or 0),
                            "visibility": summary.visibility,
                            "lazy_loaded": True,
                            "retrieval_decision_id": retrieval_decision_id,
                            "tenant_context": _tenant_context_payload(visibility_context),
                        }

                if payload is None:
                    continue

                row.lazy_loaded = True
                row.feedback_at = datetime.now(timezone.utc)
                loaded.append(payload)

            return loaded

    def explain(
        self,
        decision: AttentionDecision | dict[str, Any],
        candidates: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        selection = self.materialize_selection(candidates, decision)
        return selection.to_dict()


def observe_retrieval(
    *,
    stage: str,
    query_text: str,
    candidates: Sequence[dict[str, Any]],
    user_id: str | None = None,
    org_id: str | None = None,
    service_retrieval: bool = False,
    run_id: int | None = None,
    preload_budget_tokens: int = 0,
    lazy_budget_tokens: int = 0,
    selected_limit: int | None = None,
    mode: str | None = None,
    policy_version: str | None = None,
    preload_item_limit: int | None = None,
    usefulness_write_enabled: bool | None = None,
) -> dict[str, Any]:
    """Observe a retrieval event and persist a shadow decision if enabled."""
    controller = AttentionController(
        mode=mode,
        policy_version=policy_version,
        preload_item_limit=preload_item_limit,
        usefulness_write_enabled=usefulness_write_enabled,
    )
    decision = controller.observe(
        stage=stage,
        query_text=query_text,
        candidates=candidates,
        user_id=user_id,
        org_id=org_id,
        service_retrieval=service_retrieval,
        run_id=run_id,
        preload_budget_tokens=preload_budget_tokens,
        lazy_budget_tokens=lazy_budget_tokens,
        selected_limit=selected_limit,
    )
    return decision.to_dict()
