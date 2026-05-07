"""Offline planner for nightly skill improvement work.

The planner is deliberately persistence-free. It consumes already-loaded skill,
quality, context, bundle, and repeated-pattern evidence, then emits deterministic
advisory actions that another reviewed worker can apply later.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime
from enum import StrEnum
import hashlib
import json
import re
from typing import Any

from brain.systems.learning.budget import LearningBudgetLedger, LearningBudgetPolicy
from brain.systems.learning.night_budget import (
    NightBudgetCandidate,
    NightWorkType,
    build_night_budget_plan,
)


SKILL_IMPROVEMENT_PLAN_SCHEMA_VERSION = 1


class SkillImprovementActionType(StrEnum):
    RUN_EVAL_BUNDLE = "run_eval_bundle"
    DRAFT_OVERLAY_REFINEMENT = "draft_overlay_refinement"
    RECOMMEND_VERSION_UPDATE = "recommend_version_update"
    CREATE_PRIVATE_SKILL = "create_private_skill"
    REQUEST_ADMIN_REVIEW = "request_admin_review"


ACTION_SORT_ORDER: Mapping[str, int] = {
    SkillImprovementActionType.REQUEST_ADMIN_REVIEW.value: 0,
    SkillImprovementActionType.RUN_EVAL_BUNDLE.value: 1,
    SkillImprovementActionType.DRAFT_OVERLAY_REFINEMENT.value: 2,
    SkillImprovementActionType.RECOMMEND_VERSION_UPDATE.value: 3,
    SkillImprovementActionType.CREATE_PRIVATE_SKILL.value: 4,
}

HOSTED_PUBLIC_TRUST_LEVELS = frozenset({"public", "community", "marketplace"})
AGENT_DRAFT_VALUES = frozenset({"agent_draft", "agent-draft"})


@dataclass(frozen=True)
class SkillImprovementPolicy:
    """Thresholds and safety defaults for nightly skill improvement planning."""

    min_high_use_count: int = 10
    max_low_quality_score: float = 0.55
    min_quality_confidence_for_low: float = 0.35
    max_fallback_success_rate: float = 0.55
    min_missing_context_count: int = 3
    min_agent_draft_successful_runs: int = 3
    min_redacted_eval_cases: int = 2
    min_repeated_pattern_count: int = 3
    min_version_quality_score: float = 0.65
    min_version_quality_delta: float = 0.05
    allow_hosted_public_auto_update: bool = False
    action_token_estimates: Mapping[str, int] = field(
        default_factory=lambda: {
            SkillImprovementActionType.RUN_EVAL_BUNDLE.value: 3_500,
            SkillImprovementActionType.DRAFT_OVERLAY_REFINEMENT.value: 2_500,
            SkillImprovementActionType.RECOMMEND_VERSION_UPDATE.value: 700,
            SkillImprovementActionType.CREATE_PRIVATE_SKILL.value: 2_500,
            SkillImprovementActionType.REQUEST_ADMIN_REVIEW.value: 0,
        }
    )


@dataclass(frozen=True)
class MissingContextAggregate:
    """Collapsed repeated missing-context evidence for one skill."""

    count: int
    source_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    sections: tuple[str, ...] = ()
    requested_sections: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "source_ids": list(self.source_ids),
            "reasons": list(self.reasons),
            "sections": list(self.sections),
            "requested_sections": list(self.requested_sections),
        }


@dataclass(frozen=True)
class SkillImprovementAction:
    """One deterministic advisory action emitted by the planner."""

    action: str | SkillImprovementActionType
    target: Mapping[str, Any]
    payload: Mapping[str, Any]
    reasons: Sequence[str]
    source_ids: Sequence[str] = ()
    priority: float = 0.0
    estimated_tokens: int = 0
    safety: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    deferred: bool = False
    budget_decision: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        action = _action_value(self.action)
        target = _payload_value(dict(self.target or {}))
        payload = _payload_value(dict(self.payload or {}))
        reasons = tuple(dict.fromkeys(str(reason) for reason in self.reasons if reason))
        source_ids = tuple(sorted(dict.fromkeys(str(source) for source in self.source_ids if source)))
        safety = {
            "auto_apply": False,
            "planner_only": True,
            **dict(self.safety or {}),
        }
        metadata = {
            "llm_call": False,
            "planner": "nightly_skill_improvement",
            **dict(self.metadata or {}),
        }
        idempotency_key = self.idempotency_key or _action_idempotency_key(
            action=action,
            target=target,
            payload=payload,
            source_ids=source_ids,
        )
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "source_ids", source_ids)
        object.__setattr__(self, "priority", round(float(self.priority or 0.0), 4))
        object.__setattr__(self, "estimated_tokens", max(0, int(self.estimated_tokens or 0)))
        object.__setattr__(self, "safety", _payload_value(safety))
        object.__setattr__(self, "metadata", _payload_value(metadata))
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(
            self,
            "budget_decision",
            _payload_value(dict(self.budget_decision)) if self.budget_decision else None,
        )

    def with_budget_decision(
        self,
        *,
        deferred: bool,
        budget_decision: Mapping[str, Any],
    ) -> "SkillImprovementAction":
        return replace(
            self,
            deferred=deferred,
            budget_decision=dict(budget_decision),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SKILL_IMPROVEMENT_PLAN_SCHEMA_VERSION,
            "action": self.action,
            "idempotency_key": self.idempotency_key,
            "target": _payload_value(dict(self.target)),
            "payload": _payload_value(dict(self.payload)),
            "reasons": list(self.reasons),
            "source_ids": list(self.source_ids),
            "priority": self.priority,
            "estimated_tokens": self.estimated_tokens,
            "deferred": self.deferred,
            "budget_decision": _payload_value(self.budget_decision),
            "safety": _payload_value(dict(self.safety)),
            "metadata": _payload_value(dict(self.metadata)),
        }


@dataclass(frozen=True)
class SkillImprovementPlan:
    """Advisory plan for skill maintenance and eval work."""

    actions: Sequence[SkillImprovementAction]
    evidence_summary: Mapping[str, Any]
    budget_summary: Mapping[str, Any] | None = None
    schema_version: int = SKILL_IMPROVEMENT_PLAN_SCHEMA_VERSION
    advisory_only: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "evidence_summary", _payload_value(dict(self.evidence_summary)))
        if self.budget_summary is not None:
            object.__setattr__(self, "budget_summary", _payload_value(dict(self.budget_summary)))

    @property
    def allowed_actions(self) -> tuple[SkillImprovementAction, ...]:
        return tuple(action for action in self.actions if not action.deferred)

    @property
    def deferred_actions(self) -> tuple[SkillImprovementAction, ...]:
        return tuple(action for action in self.actions if action.deferred)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "advisory_only": self.advisory_only,
            "mode": "plan_only",
            "actions": [action.to_payload() for action in self.actions],
            "action_count": len(self.actions),
            "allowed_action_count": len(self.allowed_actions),
            "deferred_action_count": len(self.deferred_actions),
            "evidence_summary": _payload_value(dict(self.evidence_summary)),
            "budget_summary": _payload_value(self.budget_summary),
        }


def plan_skill_improvements(
    *,
    skills: Sequence[Any] = (),
    quality_scores: Sequence[Any] | Mapping[str, Any] = (),
    missing_context_signals: Sequence[Any] = (),
    agent_draft_skills: Sequence[Any] | None = None,
    bundle_update_candidates: Sequence[Any] = (),
    repeated_patterns: Sequence[Any] = (),
    eval_cases_by_skill: Mapping[str, Sequence[Any]] | None = None,
    policy: SkillImprovementPolicy | None = None,
    use_night_budget: bool = False,
    budget_policy: LearningBudgetPolicy | None = None,
    ledger: LearningBudgetLedger | None = None,
) -> SkillImprovementPlan:
    """Convert skill-quality evidence into deterministic nightly actions."""
    policy = policy or SkillImprovementPolicy()
    skill_rows = _dedupe_skill_rows(tuple(skills or ()), tuple(agent_draft_skills or ()))
    quality_payloads = _quality_index(quality_scores)
    missing_by_skill = _aggregate_missing_context(missing_context_signals)
    eval_case_index = _eval_case_index(eval_cases_by_skill or {})

    actions: list[SkillImprovementAction] = []
    for skill in sorted(skill_rows, key=_skill_sort_key):
        quality = _quality_for_skill(skill, quality_payloads)
        missing = _missing_for_skill(skill, missing_by_skill)
        low_quality = _is_high_use_low_quality(skill, quality, policy)
        has_missing_context = bool(missing and missing.count >= policy.min_missing_context_count)

        if _ready_for_eval(skill, quality, eval_case_index, policy):
            actions.append(_run_eval_bundle_action(skill, quality, eval_case_index, policy))

        if _permission_change_requested(skill):
            actions.append(_admin_review_for_skill_permission_action(skill, policy))

        if low_quality or has_missing_context:
            actions.append(
                _draft_overlay_refinement_action(
                    skill,
                    quality=quality,
                    missing_context=missing if has_missing_context else None,
                    low_quality=low_quality,
                    policy=policy,
                )
            )

    for candidate in sorted((_object_to_dict(item) for item in bundle_update_candidates or ()), key=_bundle_update_sort_key):
        if not _is_better_bundle_pin(candidate, policy):
            continue
        actions.append(_recommend_version_update_action(candidate, policy))
        if _permission_change_requested(candidate):
            actions.append(_admin_review_for_bundle_permission_action(candidate, policy))

    for pattern in sorted((_object_to_dict(item) for item in repeated_patterns or ()), key=_pattern_sort_key):
        if _pattern_count(pattern) < policy.min_repeated_pattern_count:
            continue
        if _text(pattern.get("skill_name") or pattern.get("existing_skill_name") or pattern.get("skill_effective_digest")):
            continue
        actions.append(_create_private_skill_action(pattern, policy))

    actions = tuple(sorted(actions, key=_action_sort_key))
    budget_summary: Mapping[str, Any] | None = None
    if use_night_budget and actions:
        actions, budget_summary = _apply_night_budget(
            actions,
            policy=budget_policy,
            ledger=ledger,
        )

    evidence_summary = {
        "skill_count": len(skill_rows),
        "quality_score_count": len(_iter_quality_payloads(quality_scores)),
        "missing_context_signal_count": len(tuple(missing_context_signals or ())),
        "missing_context_skill_count": len(missing_by_skill),
        "bundle_update_candidate_count": len(tuple(bundle_update_candidates or ())),
        "repeated_pattern_count": len(tuple(repeated_patterns or ())),
        "action_count": len(actions),
        "llm_calls": 0,
        "mutates_skills": False,
        "mutates_bundle_installations": False,
    }
    return SkillImprovementPlan(
        actions=actions,
        evidence_summary=evidence_summary,
        budget_summary=budget_summary,
    )


def _run_eval_bundle_action(
    skill: Mapping[str, Any],
    quality: Mapping[str, Any] | None,
    eval_case_index: Mapping[str, int],
    policy: SkillImprovementPolicy,
) -> SkillImprovementAction:
    skill_target = _skill_target(skill)
    eval_count = _eval_case_count_for_skill(skill, eval_case_index)
    success_count = _int(skill.get("success_count") or skill.get("repeated_successful_runs"), 0)
    reasons = [
        "agent draft has enough repeated successful use for redacted evals",
    ]
    if eval_count < policy.min_redacted_eval_cases:
        reasons.append("redacted eval coverage is below graduation threshold")
    return SkillImprovementAction(
        action=SkillImprovementActionType.RUN_EVAL_BUNDLE,
        target=skill_target,
        payload={
            "mode": "redacted_bundle_eval",
            "advisory_only": True,
            "bundle": _bundle_target(skill),
            "eval_requirements": {
                "min_redacted_eval_cases": policy.min_redacted_eval_cases,
                "current_redacted_eval_cases": eval_count,
                "include_raw_memory_content": False,
                "include_tenant_identifiers": False,
            },
            "quality": _quality_brief(quality),
        },
        reasons=reasons,
        source_ids=_source_ids("agent_draft", skill),
        priority=55.0 + min(40.0, success_count * 8.0),
        estimated_tokens=policy.action_token_estimates[SkillImprovementActionType.RUN_EVAL_BUNDLE.value],
        safety={
            "auto_apply": False,
            "requires_admin_review": False,
            "raw_private_context_allowed": False,
        },
    )


def _draft_overlay_refinement_action(
    skill: Mapping[str, Any],
    *,
    quality: Mapping[str, Any] | None,
    missing_context: MissingContextAggregate | None,
    low_quality: bool,
    policy: SkillImprovementPolicy,
) -> SkillImprovementAction:
    score = _quality_score(quality)
    use_count = _skill_use_count(skill)
    refinement_focus: list[str] = []
    reasons: list[str] = []
    source_ids = list(_source_ids("skill_quality", skill))
    if low_quality:
        refinement_focus.append("quality_recovery")
        reasons.append("high-use skill has low advisory quality")
    if missing_context is not None:
        refinement_focus.append("missing_context_recovery")
        reasons.append("skill has repeated missing-context evidence")
        source_ids.extend(missing_context.source_ids)
    if _failure_rate(skill) >= 0.25:
        refinement_focus.append("failure_reduction")
    if quality and _signal_value(quality, "verifier_pass_rate") is not None:
        refinement_focus.append("verifier_reliability")

    hosted_public = _is_hosted_public(skill)
    return SkillImprovementAction(
        action=SkillImprovementActionType.DRAFT_OVERLAY_REFINEMENT,
        target=_skill_target(skill),
        payload={
            "mode": "draft_only",
            "overlay": {
                "status": "draft",
                "base_effective_digest": _text(skill.get("effective_digest") or skill.get("skill_effective_digest")),
                "base_bundle_digest": _text(skill.get("bundle_digest")),
                "refinement_focus": sorted(dict.fromkeys(refinement_focus)),
                "quality": _quality_brief(quality),
                "missing_context": missing_context.to_payload() if missing_context else None,
            },
            "bundle": _bundle_target(skill),
            "hosted_public_overlay_only": hosted_public,
        },
        reasons=reasons,
        source_ids=source_ids,
        priority=_overlay_priority(use_count=use_count, quality_score=score, missing_context=missing_context),
        estimated_tokens=policy.action_token_estimates[SkillImprovementActionType.DRAFT_OVERLAY_REFINEMENT.value],
        safety={
            "auto_apply": False,
            "mutates_hosted_bundle": False,
            "requires_admin_review": hosted_public,
            "hosted_public_auto_update": False,
        },
    )


def _recommend_version_update_action(
    candidate: Mapping[str, Any],
    policy: SkillImprovementPolicy,
) -> SkillImprovementAction:
    hosted_public = _is_hosted_public(candidate)
    requires_trust_policy = hosted_public and not policy.allow_hosted_public_auto_update
    current = _version_ref(candidate, prefix="current")
    recommended = _version_ref(candidate, prefix="available")
    if not recommended["semver"]:
        recommended = _version_ref(candidate, prefix="latest")
    quality_delta = None
    current_quality = _quality_value(candidate, "current")
    available_quality = _quality_value(candidate, "available")
    if current_quality is not None and available_quality is not None:
        quality_delta = round(available_quality - current_quality, 4)

    return SkillImprovementAction(
        action=SkillImprovementActionType.RECOMMEND_VERSION_UPDATE,
        target={
            "skill_name": _text(candidate.get("skill_name")),
            "installation_id": candidate.get("installation_id") or candidate.get("skill_installation_id"),
            "bundle": _bundle_target(candidate),
            "current": current,
            "recommended": recommended,
        },
        payload={
            "mode": "recommend_pin_only",
            "recommended_pin": recommended,
            "current_pin": current,
            "quality_delta": quality_delta,
            "update_policy": _text(candidate.get("update_policy")) or "manual",
            "auto_apply": False,
            "requires_trust_policy": requires_trust_policy,
        },
        reasons=["installed bundle has a better approved pin available"],
        source_ids=_source_ids("bundle_update", candidate),
        priority=_version_update_priority(candidate),
        estimated_tokens=policy.action_token_estimates[SkillImprovementActionType.RECOMMEND_VERSION_UPDATE.value],
        safety={
            "auto_apply": False,
            "requires_admin_review": requires_trust_policy or _permission_change_requested(candidate),
            "hosted_public_auto_update": False,
            "trust_policy_required": requires_trust_policy,
        },
    )


def _create_private_skill_action(
    pattern: Mapping[str, Any],
    policy: SkillImprovementPolicy,
) -> SkillImprovementAction:
    count = _pattern_count(pattern)
    name = _private_skill_name(pattern)
    summary = _text(pattern.get("summary") or pattern.get("description") or pattern.get("pattern")) or name
    task_class = _text(pattern.get("task_class") or pattern.get("task_family"))
    return SkillImprovementAction(
        action=SkillImprovementActionType.CREATE_PRIVATE_SKILL,
        target={
            "pattern_id": _pattern_id(pattern),
            "skill_name": name,
            "task_class": task_class,
        },
        payload={
            "mode": "draft_private_skill",
            "draft": {
                "name": name,
                "description": summary,
                "source_kind": "private_local",
                "trust_level": "private_local",
                "provisional": True,
                "task_class": task_class,
            },
            "source_pattern": _payload_value(dict(pattern)),
            "minimum_occurrences": policy.min_repeated_pattern_count,
            "observed_occurrences": count,
        },
        reasons=["repeated pattern is ready for a private local skill draft"],
        source_ids=(f"repeated_pattern:{_pattern_id(pattern)}",),
        priority=45.0 + min(50.0, count * 12.0),
        estimated_tokens=policy.action_token_estimates[SkillImprovementActionType.CREATE_PRIVATE_SKILL.value],
        safety={
            "auto_apply": False,
            "requires_admin_review": False,
            "public_export_allowed": False,
        },
    )


def _admin_review_for_skill_permission_action(
    skill: Mapping[str, Any],
    policy: SkillImprovementPolicy,
) -> SkillImprovementAction:
    return SkillImprovementAction(
        action=SkillImprovementActionType.REQUEST_ADMIN_REVIEW,
        target={
            **_skill_target(skill),
            "review_subject": "skill_permission_change",
        },
        payload={
            "review_type": "skill_permission_change",
            "current_permissions": _payload_value(skill.get("current_permissions") or skill.get("permission_grants") or []),
            "requested_permissions": _payload_value(skill.get("requested_permissions") or skill.get("available_permissions") or []),
            "permission_delta": _payload_value(skill.get("permission_delta") or skill.get("permissions_delta")),
        },
        reasons=["skill permission change requires admin review"],
        source_ids=_source_ids("permission_change", skill),
        priority=92.0,
        estimated_tokens=policy.action_token_estimates[SkillImprovementActionType.REQUEST_ADMIN_REVIEW.value],
        safety={
            "auto_apply": False,
            "requires_admin_review": True,
            "permission_change": True,
        },
    )


def _admin_review_for_bundle_permission_action(
    candidate: Mapping[str, Any],
    policy: SkillImprovementPolicy,
) -> SkillImprovementAction:
    return SkillImprovementAction(
        action=SkillImprovementActionType.REQUEST_ADMIN_REVIEW,
        target={
            "skill_name": _text(candidate.get("skill_name")),
            "installation_id": candidate.get("installation_id") or candidate.get("skill_installation_id"),
            "bundle": _bundle_target(candidate),
            "review_subject": "bundle_permission_change",
        },
        payload={
            "review_type": "bundle_permission_change",
            "current_pin": _version_ref(candidate, prefix="current"),
            "recommended_pin": _version_ref(candidate, prefix="available"),
            "current_permissions": _payload_value(candidate.get("current_permissions") or candidate.get("permission_grants") or []),
            "requested_permissions": _payload_value(candidate.get("requested_permissions") or candidate.get("available_permissions") or []),
            "permission_delta": _payload_value(candidate.get("permission_delta") or candidate.get("permissions_delta")),
        },
        reasons=["recommended bundle update changes permissions"],
        source_ids=_source_ids("permission_change", candidate),
        priority=94.0,
        estimated_tokens=policy.action_token_estimates[SkillImprovementActionType.REQUEST_ADMIN_REVIEW.value],
        safety={
            "auto_apply": False,
            "requires_admin_review": True,
            "permission_change": True,
        },
    )


def _apply_night_budget(
    actions: Sequence[SkillImprovementAction],
    *,
    policy: LearningBudgetPolicy | None,
    ledger: LearningBudgetLedger | None,
) -> tuple[tuple[SkillImprovementAction, ...], Mapping[str, Any]]:
    candidates = [
        NightBudgetCandidate(
            candidate_id=action.idempotency_key or "",
            work_type=NightWorkType.SKILL_EVAL,
            estimated_tokens=action.estimated_tokens,
            org_id=_text(action.target.get("org_id")),
            user_id=_text(action.target.get("user_id")),
            subject_ref=_text(action.target.get("skill_name"))
            or _text(action.target.get("pattern_id"))
            or _text(action.target.get("installation_id")),
            description=action.action,
            impact_score=action.priority,
            signals={
                "skill_traffic_count": _int(action.payload.get("observed_occurrences"), 0),
                "skill_confidence": _quality_score(action.payload.get("quality")),
                "review_status": "needs_review" if action.action == SkillImprovementActionType.REQUEST_ADMIN_REVIEW.value else None,
            },
            metadata={"action": action.action},
        )
        for action in actions
    ]
    budget_plan = build_night_budget_plan(candidates, policy=policy, ledger=ledger)
    decisions = {
        item.candidate.candidate_id: item
        for item in budget_plan.items
    }
    budgeted_actions = []
    for action in actions:
        item = decisions.get(action.idempotency_key or "")
        if item is None:
            budgeted_actions.append(action)
            continue
        budgeted_actions.append(
            action.with_budget_decision(
                deferred=not item.allowed,
                budget_decision=item.decision.to_payload(),
            )
        )
    return tuple(budgeted_actions), budget_plan.to_payload()


def _ready_for_eval(
    skill: Mapping[str, Any],
    quality: Mapping[str, Any] | None,
    eval_case_index: Mapping[str, int],
    policy: SkillImprovementPolicy,
) -> bool:
    if not _is_agent_draft(skill):
        return False
    if _int(skill.get("success_count") or skill.get("repeated_successful_runs"), 0) < policy.min_agent_draft_successful_runs:
        return False
    if _permission_change_requested(skill):
        return False
    if _eval_case_count_for_skill(skill, eval_case_index) >= policy.min_redacted_eval_cases:
        return False
    score = _quality_score(quality)
    if score is not None and score < 0.45:
        return False
    return True


def _is_high_use_low_quality(
    skill: Mapping[str, Any],
    quality: Mapping[str, Any] | None,
    policy: SkillImprovementPolicy,
) -> bool:
    use_count = _skill_use_count(skill)
    if use_count < policy.min_high_use_count:
        return False
    score = _quality_score(quality)
    confidence = _quality_confidence(quality)
    if score is not None and score <= policy.max_low_quality_score:
        if confidence is None or confidence >= policy.min_quality_confidence_for_low:
            return True
    success_rate = _fallback_success_rate(skill)
    return success_rate is not None and success_rate <= policy.max_fallback_success_rate


def _is_better_bundle_pin(
    candidate: Mapping[str, Any],
    policy: SkillImprovementPolicy,
) -> bool:
    current = _version_ref(candidate, prefix="current")
    available = _version_ref(candidate, prefix="available")
    if not available["semver"] and not available["digest"]:
        available = _version_ref(candidate, prefix="latest")
    identity_changed = bool(
        (available["semver"] and available["semver"] != current["semver"])
        or (available["digest"] and available["digest"] != current["digest"])
        or (
            available["bundle_version_id"] is not None
            and available["bundle_version_id"] != current["bundle_version_id"]
        )
    )
    if not identity_changed:
        return False

    available_quality = _quality_value(candidate, "available")
    current_quality = _quality_value(candidate, "current")
    if available_quality is None:
        return _version_greater(available["semver"], current["semver"]) and _truthy(
            candidate.get("available_approved")
            or candidate.get("latest_approved")
            or candidate.get("status") == "approved"
        )
    if available_quality < policy.min_version_quality_score:
        return False
    if current_quality is None:
        return True
    return available_quality >= current_quality + policy.min_version_quality_delta


def _aggregate_missing_context(signals: Sequence[Any]) -> dict[str, MissingContextAggregate]:
    buckets: dict[str, dict[str, Any]] = {}
    for signal in signals or ():
        for record in _missing_context_records(signal):
            keys = _record_skill_keys(record)
            if not keys:
                continue
            canonical = sorted(keys)[0]
            bucket = buckets.setdefault(
                canonical,
                {
                    "count": 0,
                    "source_ids": set(),
                    "reasons": Counter(),
                    "sections": Counter(),
                    "requested_sections": Counter(),
                },
            )
            count = max(1, _int(record.get("count") or record.get("miss_count"), 1))
            bucket["count"] += count
            source_id = _source_id(record)
            if source_id:
                bucket["source_ids"].add(source_id)
            for reason in _text_list(record.get("reasons") or record.get("reason")):
                bucket["reasons"][reason] += count
            section = _text(record.get("section"))
            if section:
                bucket["sections"][section] += count
            evidence = _mapping(record.get("evidence"))
            requested_section = _text(
                record.get("requested_section")
                or evidence.get("requested_section")
                or evidence.get("asset_path")
            )
            if requested_section:
                bucket["requested_sections"][requested_section] += count
            for key in keys:
                buckets[key] = bucket

    result: dict[str, MissingContextAggregate] = {}
    for key, bucket in buckets.items():
        result[key] = MissingContextAggregate(
            count=int(bucket["count"]),
            source_ids=tuple(sorted(bucket["source_ids"])),
            reasons=tuple(_counter_keys(bucket["reasons"])),
            sections=tuple(_counter_keys(bucket["sections"])),
            requested_sections=tuple(_counter_keys(bucket["requested_sections"])),
        )
    return result


def _missing_context_records(signal: Any) -> list[dict[str, Any]]:
    data = _object_to_dict(signal)
    payload = _mapping(data.get("payload"))
    context_payload = _mapping(payload.get("context")) or payload
    labels = context_payload.get("labels")
    records: list[dict[str, Any]] = []
    if isinstance(labels, list):
        for label in labels:
            if not isinstance(label, Mapping):
                continue
            if _clean(label.get("label")) != "missed":
                continue
            record = {
                **data,
                **dict(label),
                "skill_effective_digest": data.get("skill_effective_digest") or label.get("skill_effective_digest"),
                "skill_name": data.get("skill_name") or label.get("skill_name"),
                "source_id": data.get("signal_digest") or data.get("id"),
            }
            records.append(record)
        return records

    label = _clean(data.get("label") or data.get("status") or data.get("kind"))
    if label in {"missed", "missing", "missing_context", "context_miss"} or data.get("missing_context"):
        records.append(data)
    return records


def _quality_index(scores: Sequence[Any] | Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for payload, explicit_key in _iter_quality_payloads_with_keys(scores):
        for key in _quality_keys(payload):
            index[key] = payload
        if explicit_key:
            index[_lookup_key("quality", explicit_key)] = payload
            index[_lookup_key("name", explicit_key)] = payload
            index[_lookup_key("digest", explicit_key)] = payload
    return index


def _iter_quality_payloads(scores: Sequence[Any] | Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(payload for payload, _key in _iter_quality_payloads_with_keys(scores))


def _iter_quality_payloads_with_keys(
    scores: Sequence[Any] | Mapping[str, Any],
) -> list[tuple[Mapping[str, Any], str | None]]:
    if not scores:
        return []
    if isinstance(scores, Mapping) and ("score" in scores or "skill" in scores):
        return [(_quality_payload(scores), None)]
    if isinstance(scores, Mapping):
        return [(_quality_payload(value), str(key)) for key, value in sorted(scores.items(), key=lambda item: str(item[0]))]
    return [(_quality_payload(value), None) for value in scores]


def _quality_for_skill(
    skill: Mapping[str, Any],
    quality_payloads: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for key in _skill_identity_keys(skill):
        quality = quality_payloads.get(key)
        if quality is not None:
            return quality
    return _fallback_quality_from_skill(skill)


def _fallback_quality_from_skill(skill: Mapping[str, Any]) -> Mapping[str, Any] | None:
    success_rate = _fallback_success_rate(skill)
    if success_rate is None:
        return None
    use_count = _skill_use_count(skill)
    return {
        "score": success_rate,
        "confidence": min(1.0, (use_count / 30.0) ** 0.5) if use_count > 0 else 0.0,
        "rating": "fallback_counts",
        "skill": {
            "name": _text(skill.get("name") or skill.get("skill_name")),
            "effective_digest": _text(skill.get("effective_digest") or skill.get("skill_effective_digest")),
        },
        "evidence": {
            "count": use_count,
            "sample_size_confidence": min(1.0, (use_count / 30.0) ** 0.5) if use_count > 0 else 0.0,
        },
    }


def _fallback_success_rate(skill: Mapping[str, Any]) -> float | None:
    use_count = _skill_use_count(skill)
    if use_count <= 0:
        return None
    success_count = _int(skill.get("success_count"), 0)
    partial_count = _int(skill.get("partial_count"), 0)
    return max(0.0, min(1.0, (success_count + 0.5 * partial_count) / use_count))


def _failure_rate(skill: Mapping[str, Any]) -> float:
    use_count = _skill_use_count(skill)
    if use_count <= 0:
        return 0.0
    return max(0.0, min(1.0, _int(skill.get("failure_count"), 0) / use_count))


def _quality_payload(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "to_payload"):
        return _object_to_dict(value.to_payload())
    return _object_to_dict(value)


def _quality_keys(payload: Mapping[str, Any]) -> set[str]:
    skill = _mapping(payload.get("skill"))
    keys = set()
    for value in (
        payload.get("skill_name"),
        skill.get("name"),
    ):
        text = _text(value)
        if text:
            keys.add(_lookup_key("name", text))
    for value in (
        payload.get("skill_effective_digest"),
        skill.get("effective_digest"),
    ):
        text = _text(value)
        if text:
            keys.add(_lookup_key("digest", text))
    return keys


def _quality_brief(quality: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not quality:
        return None
    evidence = _mapping(quality.get("evidence"))
    return {
        "score": _quality_score(quality),
        "confidence": _quality_confidence(quality),
        "rating": _text(quality.get("rating")),
        "evidence_count": evidence.get("count"),
        "sample_size_confidence": evidence.get("sample_size_confidence"),
        "reasons": list(_text_list(quality.get("reasons"))),
    }


def _quality_score(quality: Any) -> float | None:
    payload = _quality_payload(quality)
    return _float(payload.get("score"))


def _quality_confidence(quality: Any) -> float | None:
    payload = _quality_payload(quality)
    return _float(payload.get("confidence"))


def _signal_value(quality: Mapping[str, Any], signal_name: str) -> Any:
    signals = _mapping(quality.get("signals"))
    signal = _mapping(signals.get(signal_name))
    return signal.get("value")


def _quality_value(candidate: Mapping[str, Any], prefix: str) -> float | None:
    for key in (
        f"{prefix}_quality_score",
        f"{prefix}_score",
        f"{prefix}_eval_score",
    ):
        value = _float(candidate.get(key))
        if value is not None:
            return value
    for key in (
        f"{prefix}_quality",
        f"{prefix}_eval_summary",
        f"{prefix}_quality_payload",
    ):
        nested = _mapping(candidate.get(key))
        value = _float(nested.get("score") or nested.get("quality_score"))
        if value is not None:
            return value
    return None


def _missing_for_skill(
    skill: Mapping[str, Any],
    missing_by_skill: Mapping[str, MissingContextAggregate],
) -> MissingContextAggregate | None:
    for key in _skill_identity_keys(skill):
        missing = missing_by_skill.get(key)
        if missing is not None:
            return missing
    return None


def _eval_case_index(mapping: Mapping[str, Sequence[Any]]) -> dict[str, int]:
    index: dict[str, int] = {}
    for key, cases in mapping.items():
        count = len(tuple(cases or ()))
        index[_lookup_key("name", key)] = count
        index[_lookup_key("digest", key)] = count
        index[_lookup_key("quality", key)] = count
    return index


def _eval_case_count_for_skill(
    skill: Mapping[str, Any],
    eval_case_index: Mapping[str, int],
) -> int:
    explicit = _int(skill.get("redacted_eval_case_count") or skill.get("eval_case_count"), -1)
    if explicit >= 0:
        return explicit
    for key in _skill_identity_keys(skill):
        if key in eval_case_index:
            return eval_case_index[key]
    return 0


def _dedupe_skill_rows(*groups: Sequence[Any]) -> tuple[Mapping[str, Any], ...]:
    rows: dict[str, Mapping[str, Any]] = {}
    for group in groups:
        for item in group:
            data = _object_to_dict(item)
            keys = sorted(_skill_identity_keys(data))
            key = keys[0] if keys else _stable_digest(data)
            rows.setdefault(key, data)
    return tuple(rows.values())


def _skill_identity_keys(skill: Mapping[str, Any]) -> set[str]:
    keys = set()
    skill_id = _text(skill.get("id") or skill.get("skill_id"))
    if skill_id:
        keys.add(_lookup_key("id", skill_id))
    name = _text(skill.get("name") or skill.get("skill_name"))
    if name:
        keys.add(_lookup_key("name", name))
    digest = _text(skill.get("effective_digest") or skill.get("skill_effective_digest"))
    if digest:
        keys.add(_lookup_key("digest", digest))
    return keys


def _record_skill_keys(record: Mapping[str, Any]) -> set[str]:
    keys = set()
    for value in (record.get("skill_name"), record.get("name")):
        text = _text(value)
        if text:
            keys.add(_lookup_key("name", text))
    digest = _text(record.get("skill_effective_digest") or record.get("effective_digest"))
    if digest:
        keys.add(_lookup_key("digest", digest))
    target_type = _clean(record.get("target_type"))
    target_id = _text(record.get("target_id"))
    if target_type == "skill" and target_id:
        parsed_name, parsed_digest = _parse_skill_target_id(target_id)
        if parsed_name:
            keys.add(_lookup_key("name", parsed_name))
        if parsed_digest:
            keys.add(_lookup_key("digest", parsed_digest))
    return keys


def _parse_skill_target_id(value: str) -> tuple[str | None, str | None]:
    text = value.strip()
    if text.startswith("skill:"):
        text = text[len("skill:") :]
    name, separator, digest = text.partition("@")
    return (_text(name), _text(digest) if separator else None)


def _skill_target(skill: Mapping[str, Any]) -> dict[str, Any]:
    return _drop_none(
        {
            "skill_id": skill.get("id") or skill.get("skill_id"),
            "skill_name": _text(skill.get("name") or skill.get("skill_name")),
            "skill_effective_digest": _text(skill.get("effective_digest") or skill.get("skill_effective_digest")),
            "org_id": _text(skill.get("org_id")),
            "user_id": _text(skill.get("user_id")),
            "source_kind": _text(skill.get("source_kind")),
            "trust_level": _text(skill.get("trust_level")),
            "bundle": _bundle_target(skill),
        }
    )


def _bundle_target(data: Mapping[str, Any]) -> dict[str, Any]:
    namespace = _text(data.get("bundle_namespace") or data.get("namespace"))
    name = _text(data.get("bundle_name") or data.get("name"))
    return _drop_none(
        {
            "namespace": namespace,
            "name": name,
            "version": _text(
                data.get("bundle_version")
                or data.get("semver")
                or data.get("current_version")
                or data.get("installed_version")
            ),
            "digest": _text(
                data.get("bundle_digest")
                or data.get("current_digest")
                or data.get("installed_digest")
            ),
            "bundle_id": data.get("bundle_id"),
            "bundle_version_id": data.get("bundle_version_id"),
        }
    )


def _version_ref(data: Mapping[str, Any], *, prefix: str) -> dict[str, Any]:
    aliases = {
        "current": ("current", "installed", "base"),
        "available": ("available", "recommended", "latest"),
        "latest": ("latest", "available", "recommended"),
    }[prefix]
    return _drop_none(
        {
            "bundle_version_id": _first_value(data, *(f"{alias}_bundle_version_id" for alias in aliases), "bundle_version_id" if prefix == "current" else None),
            "semver": _text(_first_value(data, *(f"{alias}_version" for alias in aliases), "semver" if prefix == "current" else None)),
            "digest": _text(_first_value(data, *(f"{alias}_digest" for alias in aliases), "installed_digest" if prefix == "current" else None)),
            "quality_score": _quality_value(data, prefix),
        }
    )


def _first_value(data: Mapping[str, Any], *keys: str | None) -> Any:
    for key in keys:
        if key and data.get(key) is not None:
            return data.get(key)
    return None


def _source_ids(prefix: str, data: Mapping[str, Any]) -> tuple[str, ...]:
    explicit = _source_id(data)
    if explicit:
        return (explicit,)
    for value in (
        data.get("signal_digest"),
        data.get("candidate_digest"),
        data.get("skill_effective_digest"),
        data.get("effective_digest"),
        data.get("name"),
        data.get("skill_name"),
        data.get("installation_id"),
        data.get("skill_installation_id"),
    ):
        text = _text(value)
        if text:
            return (f"{prefix}:{text}",)
    return (f"{prefix}:{_stable_digest(data)}",)


def _source_id(data: Mapping[str, Any]) -> str | None:
    for key in ("source_id", "signal_digest", "candidate_digest", "eval_digest", "id"):
        text = _text(data.get(key))
        if text:
            return text if ":" in text else f"{key}:{text}"
    return None


def _pattern_count(pattern: Mapping[str, Any]) -> int:
    for key in ("count", "occurrence_count", "support_count", "similar_task_count"):
        value = _int(pattern.get(key), -1)
        if value >= 0:
            return value
    examples = pattern.get("examples") or pattern.get("example_digests")
    if isinstance(examples, Sequence) and not isinstance(examples, (str, bytes)):
        return len(examples)
    return 0


def _pattern_id(pattern: Mapping[str, Any]) -> str:
    return _text(pattern.get("pattern_id") or pattern.get("id") or pattern.get("digest")) or _stable_digest(pattern)


def _private_skill_name(pattern: Mapping[str, Any]) -> str:
    explicit = _text(pattern.get("proposed_skill_name") or pattern.get("name"))
    if explicit:
        return _slug(explicit)
    basis = _text(pattern.get("task_class") or pattern.get("task_family") or pattern.get("summary") or pattern.get("pattern")) or _pattern_id(pattern)
    return f"private-{_slug(basis)}"[:50].rstrip("-")


def _pattern_sort_key(pattern: Mapping[str, Any]) -> tuple[str, str]:
    return (_private_skill_name(pattern), _pattern_id(pattern))


def _bundle_update_sort_key(candidate: Mapping[str, Any]) -> tuple[str, str, str, str]:
    bundle = _bundle_target(candidate)
    return (
        str(bundle.get("namespace") or ""),
        str(bundle.get("name") or ""),
        str(candidate.get("installation_id") or candidate.get("skill_installation_id") or ""),
        str(_version_ref(candidate, prefix="available").get("semver") or ""),
    )


def _skill_sort_key(skill: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        _clean(skill.get("name") or skill.get("skill_name")) or "",
        _clean(skill.get("effective_digest") or skill.get("skill_effective_digest")) or "",
        str(skill.get("id") or skill.get("skill_id") or ""),
    )


def _action_sort_key(action: SkillImprovementAction) -> tuple[float, int, str]:
    return (
        -action.priority,
        ACTION_SORT_ORDER.get(action.action, 99),
        action.idempotency_key or "",
    )


def _overlay_priority(
    *,
    use_count: int,
    quality_score: float | None,
    missing_context: MissingContextAggregate | None,
) -> float:
    score = 40.0
    if quality_score is not None:
        score += max(0.0, 1.0 - quality_score) * 45.0
    score += min(25.0, use_count * 0.7)
    if missing_context is not None:
        score += min(35.0, missing_context.count * 8.0)
    return round(score, 4)


def _version_update_priority(candidate: Mapping[str, Any]) -> float:
    current_quality = _quality_value(candidate, "current") or 0.0
    available_quality = _quality_value(candidate, "available") or current_quality
    return round(40.0 + max(0.0, available_quality - current_quality) * 100.0, 4)


def _skill_use_count(skill: Mapping[str, Any]) -> int:
    return _int(skill.get("use_count") or skill.get("traffic_count") or skill.get("evidence_count"), 0)


def _is_agent_draft(skill: Mapping[str, Any]) -> bool:
    source_kind = _clean(skill.get("source_kind"))
    trust_level = _clean(skill.get("trust_level"))
    return source_kind in AGENT_DRAFT_VALUES or trust_level in AGENT_DRAFT_VALUES or bool(skill.get("agent_draft"))


def _is_hosted_public(data: Mapping[str, Any]) -> bool:
    hosted = _truthy(data.get("hosted") or data.get("hosted_mode"))
    trust = _clean(data.get("trust_level") or data.get("visibility") or data.get("source_kind"))
    namespace = _clean(data.get("bundle_namespace") or data.get("namespace"))
    return bool(hosted or namespace in {"public", "community", "marketplace"}) and trust in HOSTED_PUBLIC_TRUST_LEVELS


def _permission_change_requested(data: Mapping[str, Any]) -> bool:
    for key in ("permission_change", "permissions_changed", "unresolved_permission_escalation"):
        if _truthy(data.get(key)):
            return True
    delta = data.get("permission_delta") or data.get("permissions_delta")
    if isinstance(delta, Mapping) and delta:
        return True
    if isinstance(delta, Sequence) and not isinstance(delta, (str, bytes)) and len(delta) > 0:
        return True
    requested = data.get("requested_permissions") or data.get("available_permissions")
    current = data.get("current_permissions") or data.get("permission_grants")
    if requested is not None and current is not None:
        return _payload_value(requested) != _payload_value(current)
    return False


def _version_greater(left: Any, right: Any) -> bool:
    left_text = _text(left)
    right_text = _text(right)
    if not left_text or not right_text:
        return bool(left_text and left_text != right_text)
    return _version_tuple(left_text) > _version_tuple(right_text)


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = [int(part) for part in re.findall(r"\d+", value)]
    return tuple(parts or [0])


def _lookup_key(kind: str, value: Any) -> str:
    return f"{kind}:{_clean(value) or ''}"


def _counter_keys(counter: Counter) -> list[str]:
    return [key for key, _count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))]


def _action_value(value: str | SkillImprovementActionType) -> str:
    return value.value if isinstance(value, SkillImprovementActionType) else str(value)


def _action_idempotency_key(
    *,
    action: str,
    target: Mapping[str, Any],
    payload: Mapping[str, Any],
    source_ids: Sequence[str],
) -> str:
    digest = _stable_digest(
        {
            "schema_version": SKILL_IMPROVEMENT_PLAN_SCHEMA_VERSION,
            "action": action,
            "target": target,
            "payload": payload,
            "source_ids": list(source_ids),
        }
    )
    return f"skill-improvement:v{SKILL_IMPROVEMENT_PLAN_SCHEMA_VERSION}:{action}:{digest}"


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(_payload_value(payload), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _object_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "to_payload"):
        return _object_to_dict(value.to_payload())
    table = getattr(value, "__table__", None)
    if table is not None:
        return {column.name: getattr(value, column.name) for column in table.columns}
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _payload_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _payload_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            if item is not None
        }
    if isinstance(value, tuple):
        return [_payload_value(item) for item in value]
    if isinstance(value, list):
        return [_payload_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_payload_value(item) for item in value)
    return value


def _drop_none(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None and item != {}}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean(value: Any) -> str | None:
    text = _text(value)
    return text.lower().replace("-", "_") if text else None


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [text for item in value if (text := _text(item))]
    text = _text(value)
    return [text] if text else []


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "approved"}


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return re.sub(r"-+", "-", text).strip("-") or "skill"


__all__ = [
    "SKILL_IMPROVEMENT_PLAN_SCHEMA_VERSION",
    "SkillImprovementAction",
    "SkillImprovementActionType",
    "SkillImprovementPlan",
    "SkillImprovementPolicy",
    "plan_skill_improvements",
]
