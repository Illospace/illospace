"""Habit compiler primitives, shadow matching, and execution records."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from brain.kernel.common.coercion import int_or_none as _shared_int_or_none
from brain.kernel.common.serialization import jsonable as _shared_jsonable
from sqlalchemy import select

from brain.platform.db.models.habit import RunHabit, HabitExecution, HabitVersion
from brain.systems.feedback.heuristics import task_family_from_text

logger = logging.getLogger("agent_runtime.habits")

HABIT_SCHEMA_VERSION = 1
HABIT_MATCHER_SCHEMA_VERSION = 1
HABIT_EXECUTION_SCHEMA_VERSION = 1


def _jsonable(value: Any) -> Any:
    return _shared_jsonable(value)


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(_jsonable(dict(payload)), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    return _shared_int_or_none(value)


def _unique_preserve_order(values: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    ordered: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _freeze_payload(value: Any) -> Any:
    return json.loads(json.dumps(_jsonable(value), sort_keys=True))


@dataclass(frozen=True)
class HabitSourceRun:
    """Passive source-run bundle used to mine future habits."""

    source_run_id: int | None
    task: str
    task_family: str
    task_hash: str
    signature_hash: str
    source_skill: str | None
    contract_type: str | None
    target_status: str | None
    workspace_fingerprint: str | None
    runtime_fingerprint: str | None
    context_shape: tuple[str, ...] = field(default_factory=tuple)
    success: bool | None = None
    duration_sec: int | None = None
    tokens_used: int | None = None
    cost: float | None = None
    source_bundle: dict[str, Any] = field(default_factory=dict)
    signature_features: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    completed_at: datetime | None = None


@dataclass(frozen=True)
class HabitSourceAggregate:
    """Aggregated compiler input derived from repeated source runs."""

    task_family: str
    source_skill: str | None
    signature_hash: str
    source_run_ids: list[int] = field(default_factory=list)
    source_runs: list[HabitSourceRun] = field(default_factory=list)
    shared_context_shape: tuple[str, ...] = field(default_factory=tuple)
    matcher: dict[str, Any] = field(default_factory=dict)
    preconditions: dict[str, Any] = field(default_factory=dict)
    step_graph: list[dict[str, Any]] = field(default_factory=list)
    expected_artifacts: dict[str, Any] = field(default_factory=dict)
    fallback_policy: dict[str, Any] = field(default_factory=dict)
    eligibility_metrics: dict[str, Any] = field(default_factory=dict)
    verifier_profile: dict[str, Any] = field(default_factory=dict)
    shadow_stats: dict[str, Any] = field(default_factory=dict)
    activation_signals: dict[str, Any] = field(default_factory=dict)
    demotion_signals: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HabitCompilerProposal:
    """Candidate compiled habit version."""

    habit_key: str
    task_family: str
    source_skill: str | None
    signature_hash: str
    version: int
    source_run_ids: list[int] = field(default_factory=list)
    matcher: dict[str, Any] = field(default_factory=dict)
    preconditions: dict[str, Any] = field(default_factory=dict)
    step_graph: list[dict[str, Any]] = field(default_factory=list)
    expected_artifacts: dict[str, Any] = field(default_factory=dict)
    fallback_policy: dict[str, Any] = field(default_factory=dict)
    eligibility_metrics: dict[str, Any] = field(default_factory=dict)
    verifier_profile: dict[str, Any] = field(default_factory=dict)
    shadow_stats: dict[str, Any] = field(default_factory=dict)
    activation_signals: dict[str, Any] = field(default_factory=dict)
    demotion_signals: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HabitMatchResult:
    """Shadow matcher result for a habit version."""

    matched: bool
    confidence: float
    guard_result: dict[str, Any]
    fallback_reason: str | None = None
    matched_version_id: int | None = None
    signal_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HabitExecutionRecord:
    """Structured execution record for a habit evaluation or fallback."""

    run_id: int
    habit_id: int
    habit_version_id: int
    match_confidence: float
    guard_result: dict[str, Any]
    status: str
    fallback_reason: str | None
    executed_steps: list[dict[str, Any]] = field(default_factory=list)
    verifier_result: dict[str, Any] = field(default_factory=dict)
    signal_summary: dict[str, Any] = field(default_factory=dict)
    duration_ms: int | None = None
    tokens: int | None = None
    cost: float | None = None


def source_run_from_signature(
    signature: Mapping[str, Any],
    *,
    task: str | None = None,
    success: bool | None = None,
    duration_sec: int | None = None,
    tokens_used: int | None = None,
    cost: float | None = None,
    metadata: dict[str, Any] | None = None,
    completed_at: datetime | None = None,
) -> HabitSourceRun:
    """Convert a passive signature bundle into a typed source-run record."""
    task_value = task or str(signature.get("task") or "")
    task_family = str(signature.get("task_family") or task_family_from_text(task_value))
    context_shape = tuple(str(item) for item in signature.get("context_shape") or [])
    source_bundle_raw = signature.get("source_bundle") or {}
    source_bundle = dict(source_bundle_raw) if isinstance(source_bundle_raw, Mapping) else {}
    shadow_feedback_raw = signature.get("shadow_feedback") or {}
    shadow_feedback = dict(shadow_feedback_raw) if isinstance(shadow_feedback_raw, Mapping) else {}
    if shadow_feedback:
        source_bundle.setdefault("shadow_feedback", shadow_feedback)
    signature_features = {
        "task_family": task_family,
        "task_markers": list(signature.get("task_markers") or []),
        "source_strategy": signature.get("source_strategy"),
        "source_run_id": signature.get("source_run_id"),
        "context_shape": list(context_shape),
        "runtime_fingerprint": signature.get("runtime_fingerprint"),
        "workspace_fingerprint": signature.get("workspace_fingerprint"),
        "contract_type": signature.get("contract_type"),
        "target_status": signature.get("target_status"),
        "shadow_feedback": shadow_feedback,
    }
    return HabitSourceRun(
        source_run_id=_int_or_none(signature.get("source_run_id")),
        task=task_value,
        task_family=task_family,
        task_hash=str(signature.get("task_hash") or ""),
        signature_hash=str(signature.get("signature_hash") or _canonical_hash(signature)),
        source_skill=signature.get("source_skill") or None,
        contract_type=signature.get("contract_type") or None,
        target_status=signature.get("target_status") or None,
        workspace_fingerprint=signature.get("workspace_fingerprint") or None,
        runtime_fingerprint=signature.get("runtime_fingerprint") or None,
        context_shape=context_shape,
        success=success if success is not None else signature.get("success"),
        duration_sec=_int_or_none(duration_sec if duration_sec is not None else signature.get("duration_sec")),
        tokens_used=_int_or_none(tokens_used if tokens_used is not None else signature.get("tokens_used")),
        cost=_float_or_none(cost if cost is not None else signature.get("cost")),
        source_bundle=source_bundle,
        signature_features=signature_features,
        metadata=dict(metadata or signature.get("metadata") or {}),
        completed_at=completed_at or signature.get("completed_at"),
    )


def source_run_from_recordings(
    recordings: Mapping[str, Any],
    *,
    task: str | None = None,
    success: bool | None = None,
    duration_sec: int | None = None,
    tokens_used: int | None = None,
    cost: float | None = None,
) -> HabitSourceRun:
    """Build a source run directly from persisted run recordings."""
    signature = {
        "task": task or str((recordings.get("flight_recorder") or {}).get("input_envelope", {}).get("message") or ""),
        "task_family": None,
        "task_hash": None,
        "source_skill": (recordings.get("run_summary") or {}).get("skill_used"),
        "contract_type": (recordings.get("flight_recorder") or {}).get("target", {}).get("contract_type"),
        "target_status": (recordings.get("flight_recorder") or {}).get("target", {}).get("status"),
        "workspace_fingerprint": (recordings.get("flight_recorder") or {}).get("context", {}).get("workspace_mode"),
        "runtime_fingerprint": None,
        "context_shape": [],
        "source_run_id": recordings.get("run_id"),
        "source_strategy": "pipeline",
        "source_bundle": recordings,
        "success": success,
        "duration_sec": duration_sec,
        "tokens_used": tokens_used,
        "cost": cost,
    }
    return source_run_from_signature(
        signature,
        task=task,
        success=success,
        duration_sec=duration_sec,
        tokens_used=tokens_used,
        cost=cost,
        metadata={
            "recorded_run_summary": recordings.get("run_summary", {}),
            "recorded_flight_recorder": recordings.get("flight_recorder", {}),
        },
    )


def _group_key(source_run: HabitSourceRun) -> tuple[Any, ...]:
    return (
        source_run.task_family,
        source_run.source_skill or "",
        source_run.contract_type or "",
        source_run.target_status or "",
        source_run.workspace_fingerprint or "",
        source_run.runtime_fingerprint or "",
    )


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _spread_within(values: list[float], *, max_fraction: float = 0.35, min_abs: float = 30.0) -> bool:
    if len(values) <= 1:
        return True
    avg = _mean(values)
    if avg is None:
        return True
    spread = max(values) - min(values)
    return spread <= max(min_abs, abs(avg) * max_fraction)


def _shared_value(group_runs: list[HabitSourceRun], attr: str) -> Any | None:
    values = [getattr(run, attr) for run in group_runs if getattr(run, attr) is not None]
    if not values:
        return None
    first = values[0]
    if all(value == first for value in values):
        return first
    return None


def _build_step_graph(seed: HabitSourceRun, source_run_ids: list[int], common_context: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        {
            "step_id": "guard_compile",
            "kind": "guard",
            "depends_on": [],
            "checks": [
                "task_family",
                "source_skill",
                "contract_type",
                "target_status",
                "workspace_fingerprint",
                "runtime_fingerprint",
                "context_shape",
            ],
            "fallback": "full_pipeline",
        },
        {
            "step_id": "execute_primary",
            "kind": "execution",
            "depends_on": ["guard_compile"],
            "skill_name": seed.source_skill,
            "task_template": seed.task[:500],
            "source_run_ids": list(source_run_ids),
            "expected_context": list(common_context),
        },
        {
            "step_id": "verify_output",
            "kind": "verifier",
            "depends_on": ["execute_primary"],
            "expected_contract_type": seed.contract_type,
            "expected_target_status": seed.target_status,
            "expected_runtime_fingerprint": seed.runtime_fingerprint,
            "expected_workspace_fingerprint": seed.workspace_fingerprint,
            "fallback": "full_pipeline",
        },
    ]


def aggregate_habit_source_runs(
    source_runs: Iterable[HabitSourceRun],
    *,
    min_source_runs: int = 3,
) -> list[HabitSourceAggregate]:
    """Aggregate source runs into narrow compiler inputs."""
    grouped: dict[tuple[Any, ...], list[HabitSourceRun]] = {}
    for source_run in source_runs:
        grouped.setdefault(_group_key(source_run), []).append(source_run)

    aggregates: list[HabitSourceAggregate] = []
    for group_runs in grouped.values():
        if len(group_runs) < min_source_runs:
            continue
        if any(run.success is False for run in group_runs):
            continue
        if any(run.success is None for run in group_runs):
            continue

        durations = [float(run.duration_sec or 0) for run in group_runs if run.duration_sec is not None]
        tokens = [float(run.tokens_used or 0) for run in group_runs if run.tokens_used is not None]
        if durations and not _spread_within(durations):
            continue
        if tokens and not _spread_within(tokens, max_fraction=0.45, min_abs=500.0):
            continue

        seed = group_runs[0]
        source_run_ids = sorted(
            {
                run.source_run_id
                for run in group_runs
                if run.source_run_id is not None
            }
        )
        if not source_run_ids:
            continue
        common_context = tuple(
            item for item in seed.context_shape
            if all(item in run.context_shape for run in group_runs)
        )
        source_run_count = len(group_runs)
        success_rate = sum(1 for run in group_runs if run.success) / source_run_count
        shared_workspace = _shared_value(group_runs, "workspace_fingerprint")
        shared_runtime = _shared_value(group_runs, "runtime_fingerprint")
        shared_contract = _shared_value(group_runs, "contract_type")
        shared_target = _shared_value(group_runs, "target_status")

        matcher = {
            "schema_version": HABIT_MATCHER_SCHEMA_VERSION,
            "task_family": seed.task_family,
            "source_skill": seed.source_skill,
            "contract_type": shared_contract,
            "target_status": shared_target,
            "workspace_fingerprint": shared_workspace,
            "runtime_fingerprint": shared_runtime,
            "context_shape": list(common_context),
            "signature_hash": seed.signature_hash,
            "source": {
                "skill_name": seed.source_skill,
                "contract_type": shared_contract,
                "target_status": shared_target,
                "source_run_count": source_run_count,
            },
            "runtime": {
                "workspace_fingerprint": shared_workspace,
                "runtime_fingerprint": shared_runtime,
            },
            "context": {
                "required": list(common_context),
                "source_markers": list(seed.signature_features.get("task_markers") or []),
                "source_bundle": _freeze_payload(seed.source_bundle),
            },
        }
        preconditions = {
            "schema_version": HABIT_MATCHER_SCHEMA_VERSION,
            "source_skill": seed.source_skill,
            "contract_type": shared_contract,
            "target_status": shared_target,
            "workspace_fingerprint": shared_workspace,
            "runtime_fingerprint": shared_runtime,
            "context_shape": list(common_context),
            "fallback_to_full_pipeline": True,
            "fallback": {
                "allow_full_pipeline": True,
                "primary_path": "full_pipeline",
                "reasons": [
                    "guard_failed",
                    "verifier_mismatch",
                    "runtime_drift",
                ],
            },
            "verification": {
                "expected_contract_type": shared_contract,
                "expected_target_status": shared_target,
                "expected_runtime_fingerprint": shared_runtime,
                "expected_workspace_fingerprint": shared_workspace,
                "match_confidence_floor": 0.85,
            },
        }
        step_graph = _build_step_graph(seed, source_run_ids, common_context)
        expected_artifacts = {
            "task_family": seed.task_family,
            "task_hash": seed.task_hash,
            "source_run_ids": source_run_ids,
            "step_count": len(step_graph),
            "expected_verifier": {
                "contract_type": shared_contract,
                "target_status": shared_target,
                "runtime_fingerprint": shared_runtime,
                "workspace_fingerprint": shared_workspace,
            },
        }
        fallback_policy = {
            "fallback_to_full_pipeline": True,
            "primary_path": "full_pipeline",
            "fallback_reasons": [
                "guard_failed",
                "verifier_mismatch",
                "runtime_drift",
            ],
            "demotion_signals": [
                "workspace_drift",
                "runtime_drift",
                "verifier_regression",
            ],
        }
        eligibility_metrics = {
            "sample_count": source_run_count,
            "success_rate": round(success_rate, 3),
            "avg_duration_sec": _mean(durations),
            "avg_tokens": _mean(tokens),
            "unique_workspaces": len({run.workspace_fingerprint for run in group_runs if run.workspace_fingerprint}),
            "unique_runtimes": len({run.runtime_fingerprint for run in group_runs if run.runtime_fingerprint}),
        }
        verifier_profile = {
            "expected_contract_type": shared_contract,
            "expected_target_status": shared_target,
            "expected_runtime_fingerprint": shared_runtime,
            "expected_workspace_fingerprint": shared_workspace,
            "match_confidence_floor": 0.85,
            "accepted_variance": {
                "duration_spread_ok": _spread_within(durations) if durations else True,
                "token_spread_ok": _spread_within(tokens, max_fraction=0.45, min_abs=500.0) if tokens else True,
            },
        }
        shadow_stats = {
            "observations": source_run_count,
            "source_run_ids": source_run_ids,
            "duration_spread_ok": _spread_within(durations) if durations else True,
            "token_spread_ok": _spread_within(tokens, max_fraction=0.45, min_abs=500.0) if tokens else True,
            "success_rate": round(success_rate, 3),
            "context_shape": list(common_context),
        }
        activation_signals = {
            "promotion_ready": success_rate >= 1.0 and shadow_stats["duration_spread_ok"] and shadow_stats["token_spread_ok"],
            "shadow_observations": source_run_count,
            "stable_workspace": shared_workspace is not None,
            "stable_runtime": shared_runtime is not None,
        }
        demotion_signals = {
            "workspace_drift": shared_workspace is None,
            "runtime_drift": shared_runtime is None,
            "variance_spike": not shadow_stats["duration_spread_ok"] or not shadow_stats["token_spread_ok"],
        }
        aggregates.append(
            HabitSourceAggregate(
                task_family=seed.task_family,
                source_skill=seed.source_skill,
                signature_hash=seed.signature_hash,
                source_run_ids=source_run_ids,
                source_runs=list(group_runs),
                shared_context_shape=common_context,
                matcher=matcher,
                preconditions=preconditions,
                step_graph=step_graph,
                expected_artifacts=expected_artifacts,
                fallback_policy=fallback_policy,
                eligibility_metrics=eligibility_metrics,
                verifier_profile=verifier_profile,
                shadow_stats=shadow_stats,
                activation_signals=activation_signals,
                demotion_signals=demotion_signals,
            )
        )

    return aggregates


def compile_habit_proposals(
    source_runs: Iterable[HabitSourceRun],
    *,
    min_source_runs: int = 3,
) -> list[HabitCompilerProposal]:
    """Cluster repeated successful source runs into narrow habit proposals."""
    proposals: list[HabitCompilerProposal] = []
    for aggregate in aggregate_habit_source_runs(source_runs, min_source_runs=min_source_runs):
        habit_key = f"{aggregate.task_family}:{aggregate.signature_hash[:8]}"
        proposals.append(
            HabitCompilerProposal(
                habit_key=habit_key,
                task_family=aggregate.task_family,
                source_skill=aggregate.source_skill,
                signature_hash=aggregate.signature_hash,
                version=1,
                source_run_ids=list(aggregate.source_run_ids),
                matcher=_freeze_payload(aggregate.matcher),
                preconditions=_freeze_payload(aggregate.preconditions),
                step_graph=_freeze_payload(aggregate.step_graph),
                expected_artifacts=_freeze_payload(aggregate.expected_artifacts),
                fallback_policy=_freeze_payload(aggregate.fallback_policy),
                eligibility_metrics=_freeze_payload(aggregate.eligibility_metrics),
                verifier_profile=_freeze_payload(aggregate.verifier_profile),
                shadow_stats=_freeze_payload(aggregate.shadow_stats),
                activation_signals=_freeze_payload(aggregate.activation_signals),
                demotion_signals=_freeze_payload(aggregate.demotion_signals),
            )
        )

    return proposals


def next_habit_version_number(session, habit_id: int) -> int:
    """Return the next immutable version number for a habit family."""
    current = session.scalar(
        select(HabitVersion.version)
        .where(HabitVersion.habit_id == habit_id)
        .order_by(HabitVersion.version.desc())
    )
    return int(current or 0) + 1


def activate_habit_version(session, habit_id: int, habit_version_id: int) -> RunHabit:
    """Pin a habit family to an immutable version without mutating history."""
    habit = session.get(RunHabit, habit_id)
    version = session.get(HabitVersion, habit_version_id)
    if not habit:
        raise ValueError(f"Habit #{habit_id} not found")
    if not version:
        raise ValueError(f"Habit version #{habit_version_id} not found")
    if version.habit_id != habit.id:
        raise ValueError(f"Habit version #{habit_version_id} does not belong to habit #{habit_id}")

    habit.active_version_id = version.id
    habit.status = "active"
    return habit


def _extract_check(
    *,
    name: str,
    observed: Any,
    required: Any,
    strict: bool = True,
) -> tuple[bool, dict[str, Any]]:
    if required is None:
        return True, {
            "name": name,
            "observed": observed,
            "required": required,
            "passed": True,
            "strict": strict,
        }
    passed = observed == required if strict else bool(observed)
    return passed, {
        "name": name,
        "observed": observed,
        "required": required,
        "passed": passed,
        "strict": strict,
    }


def _compiled_value(bundle: Mapping[str, Any], key: str, *sections: str) -> Any:
    if key in bundle:
        return bundle.get(key)
    for section_name in sections:
        section = bundle.get(section_name)
        if isinstance(section, Mapping) and key in section:
            return section.get(key)
    return None


def _fallback_allowed(preconditions: Mapping[str, Any]) -> bool:
    if "fallback_to_full_pipeline" in preconditions:
        return bool(preconditions.get("fallback_to_full_pipeline"))
    fallback = preconditions.get("fallback")
    if isinstance(fallback, Mapping) and "allow_full_pipeline" in fallback:
        return bool(fallback.get("allow_full_pipeline"))
    return True


def evaluate_habit_match(
    signature: Mapping[str, Any],
    matcher: Mapping[str, Any],
    preconditions: Mapping[str, Any] | None = None,
) -> HabitMatchResult:
    """Evaluate a shadow matcher against a passive signature bundle."""
    preconditions = preconditions or {}
    checks: list[dict[str, Any]] = []
    match_score = 0.0
    signal_summary: dict[str, Any] = {
        "promotion_ready": False,
        "activation_signals": [],
        "demotion_signals": [],
        "fallback_path": "full_pipeline",
    }

    def _check(name: str, observed: Any, required: Any, *, strict: bool = True, weight: float = 0.0) -> bool:
        nonlocal match_score
        passed, payload = _extract_check(name=name, observed=observed, required=required, strict=strict)
        checks.append(payload)
        if passed:
            match_score += weight
        return passed

    task_family_required = _compiled_value(matcher, "task_family", "source", "routing")
    if not _check("task_family", signature.get("task_family"), task_family_required, weight=0.25):
        reason = "task family mismatch"
        signal_summary["demotion_signals"].append("task_family_mismatch")
        return HabitMatchResult(False, 0.0, {"matched": False, "checks": checks, "reason": reason, "signals": signal_summary}, reason, signal_summary=signal_summary)

    source_skill_required = _compiled_value(matcher, "source_skill", "source")
    if source_skill_required is None:
        source_section = matcher.get("source")
        if isinstance(source_section, Mapping):
            source_skill_required = source_section.get("skill_name")
    if not _check("source_skill", signature.get("source_skill"), source_skill_required, weight=0.15):
        reason = "source skill mismatch"
        signal_summary["demotion_signals"].append("source_skill_mismatch")
        return HabitMatchResult(False, 0.0, {"matched": False, "checks": checks, "reason": reason, "signals": signal_summary}, reason, signal_summary=signal_summary)

    contract_type_required = _compiled_value(matcher, "contract_type", "source", "verification")
    if not _check("contract_type", signature.get("contract_type"), contract_type_required, weight=0.15):
        reason = "contract type mismatch"
        signal_summary["demotion_signals"].append("contract_type_mismatch")
        return HabitMatchResult(False, 0.0, {"matched": False, "checks": checks, "reason": reason, "signals": signal_summary}, reason, signal_summary=signal_summary)

    target_status_required = _compiled_value(matcher, "target_status", "source", "verification")
    if not _check("target_status", signature.get("target_status"), target_status_required, weight=0.10):
        reason = "target status mismatch"
        signal_summary["demotion_signals"].append("target_status_mismatch")
        return HabitMatchResult(False, 0.0, {"matched": False, "checks": checks, "reason": reason, "signals": signal_summary}, reason, signal_summary=signal_summary)

    workspace_required = _compiled_value(matcher, "workspace_fingerprint", "runtime")
    if not _check("workspace_fingerprint", signature.get("workspace_fingerprint"), workspace_required, weight=0.10):
        reason = "workspace fingerprint mismatch"
        signal_summary["demotion_signals"].append("workspace_drift")
        return HabitMatchResult(False, 0.0, {"matched": False, "checks": checks, "reason": reason, "signals": signal_summary}, reason, signal_summary=signal_summary)

    runtime_required = _compiled_value(matcher, "runtime_fingerprint", "runtime")
    if not _check("runtime_fingerprint", signature.get("runtime_fingerprint"), runtime_required, weight=0.10):
        reason = "runtime fingerprint mismatch"
        signal_summary["demotion_signals"].append("runtime_drift")
        return HabitMatchResult(False, 0.0, {"matched": False, "checks": checks, "reason": reason, "signals": signal_summary}, reason, signal_summary=signal_summary)

    required_context = list(_compiled_value(matcher, "context_shape", "context") or [])
    if not required_context:
        context_section = matcher.get("context")
        if isinstance(context_section, Mapping):
            required_context = list(context_section.get("required") or context_section.get("context_shape") or [])
    observed_context = list(signature.get("context_shape") or [])
    missing_context = [item for item in required_context if item not in observed_context]
    checks.append({
        "name": "context_shape",
        "observed": observed_context,
        "required": required_context,
        "missing": missing_context,
        "passed": not missing_context,
        "strict": True,
    })
    if missing_context:
        reason = f"missing context markers: {', '.join(missing_context)}"
        signal_summary["demotion_signals"].append("context_drift")
        return HabitMatchResult(False, 0.0, {"matched": False, "checks": checks, "reason": reason, "signals": signal_summary}, reason, signal_summary=signal_summary)
    match_score += 0.15 if required_context else 0.05

    precondition_aliases = {
        "source_skill": ("expected_source_skill", "skill_name"),
        "contract_type": ("expected_contract_type",),
        "target_status": ("expected_target_status",),
        "workspace_fingerprint": ("expected_workspace_fingerprint",),
        "runtime_fingerprint": ("expected_runtime_fingerprint",),
    }
    verification_section = preconditions.get("verification")
    for name in ("source_skill", "contract_type", "target_status", "workspace_fingerprint", "runtime_fingerprint"):
        required = _compiled_value(preconditions, name, "verification", "fallback")
        if required is None and isinstance(verification_section, Mapping):
            for alias in precondition_aliases.get(name, ()):
                if alias in verification_section:
                    required = verification_section.get(alias)
                    break
        _check(f"precondition:{name}", signature.get(name), required, weight=0.0)

    if not _fallback_allowed(preconditions):
        checks.append({
            "name": "fallback_policy",
            "observed": signature.get("fallback_policy"),
            "required": True,
            "passed": False,
            "strict": True,
        })
        reason = "fallback policy does not allow full pipeline"
        signal_summary["demotion_signals"].append("fallback_disabled")
        return HabitMatchResult(False, 0.0, {"matched": False, "checks": checks, "reason": reason, "signals": signal_summary}, reason, signal_summary=signal_summary)

    confidence = min(0.99, 0.65 + match_score)
    match_confidence_floor = float(
        _compiled_value(preconditions, "match_confidence_floor", "verification") or 0.85
    )
    if confidence >= match_confidence_floor:
        signal_summary["activation_signals"].append("confidence_floor_met")
    else:
        signal_summary["demotion_signals"].append("confidence_below_floor")
    signal_summary["promotion_ready"] = confidence >= match_confidence_floor
    signal_summary["match_confidence_floor"] = match_confidence_floor
    signal_summary["required_context"] = required_context
    signal_summary["observed_context"] = observed_context
    guard_result = {
        "matched": True,
        "checks": checks,
        "required_context": required_context,
        "preconditions": dict(preconditions),
        "signals": signal_summary,
    }
    return HabitMatchResult(True, confidence, guard_result, None, signal_summary=signal_summary)


def _run_verifier_result(run: Any | None) -> dict[str, Any]:
    if not run:
        return {}
    return {
        "contract_status": getattr(run, "contract_status", None),
        "contract_type": getattr(run, "contract_type", None),
        "target_status": getattr(run, "target_status", None),
        "scout_class": getattr(run, "scout_class", None),
        "verification_attempts": getattr(run, "verification_attempts", None),
        "verification_warnings": list(getattr(run, "verification_warnings", []) or []),
    }


def _as_habit_execution_record(
    *,
    run_id: int,
    habit: HabitVersion,
    match: HabitMatchResult,
    duration_ms: int | None,
    tokens: int | None,
    cost: float | None,
) -> HabitExecutionRecord:
    fallback_reason = match.fallback_reason or (
        "full pipeline retained during shadow evaluation"
        if match.matched
        else "full pipeline retained after shadow rejection"
    )
    status = "shadow_match" if match.matched else "shadow_rejected"
    executed_steps = list(habit.step_graph or []) if match.matched else []
    verifier_result = {
        "shadow_only": True,
        "planned_steps": list(habit.step_graph or []),
        "habit_version_id": habit.id,
        "fallback_mode": "full_pipeline",
        "signal_summary": match.signal_summary,
    }
    return HabitExecutionRecord(
        run_id=run_id,
        habit_id=habit.habit_id,
        habit_version_id=habit.id,
        match_confidence=match.confidence,
        guard_result=match.guard_result,
        status=status,
        fallback_reason=fallback_reason,
        executed_steps=executed_steps,
        verifier_result=verifier_result,
        signal_summary=match.signal_summary,
        duration_ms=duration_ms,
        tokens=tokens,
        cost=cost,
    )


def record_habit_shadow_executions(
    session,
    *,
    run,
    signature: Mapping[str, Any],
    duration_sec: int | None = None,
    tokens_used: int | None = None,
    cost: float | None = None,
) -> list[HabitExecutionRecord]:
    """Persist shadow evaluations for active habit versions.

    Passive only: the full pipeline already ran. This records what would have
    happened if a compiled habit had been active, and keeps the fallback path
    explicit.
    """
    run_id = getattr(run, "id", None)
    if not run_id:
        return []

    rows = session.execute(
        select(HabitVersion)
        .join(RunHabit, RunHabit.id == HabitVersion.habit_id)
        .where(
            RunHabit.status == "active",
            RunHabit.active_version_id == HabitVersion.id,
        )
        .order_by(RunHabit.id.asc(), HabitVersion.version.desc())
    ).scalars().all()

    records: list[HabitExecutionRecord] = []
    for habit_version in rows:
        match = evaluate_habit_match(signature, habit_version.matcher or {}, habit_version.preconditions or {})
        record = _as_habit_execution_record(
            run_id=run_id,
            habit=habit_version,
            match=match,
            duration_ms=(duration_sec * 1000) if duration_sec is not None else None,
            tokens=tokens_used,
            cost=cost,
        )
        session.add(
            HabitExecution(
                run_id=record.run_id,
                habit_id=record.habit_id,
                habit_version_id=record.habit_version_id,
                match_confidence=record.match_confidence,
                guard_result=record.guard_result,
                status=record.status,
                fallback_reason=record.fallback_reason,
                executed_steps=record.executed_steps,
                verifier_result={
                    **record.verifier_result,
                    **_run_verifier_result(run),
                },
                duration_ms=record.duration_ms,
                tokens=record.tokens,
                cost=record.cost,
            )
        )
        records.append(record)

    if records:
        logger.info(
            "Recorded %s habit shadow evaluations for run %s",
            len(records),
            run_id,
        )
    return records


def summarize_habit_shadow_feedback(records: Iterable[HabitExecutionRecord]) -> dict[str, Any]:
    """Summarize shadow records into activation and demotion signals."""
    records_list = list(records)
    matched = [record for record in records_list if record.status == "shadow_match"]
    rejected = [record for record in records_list if record.status == "shadow_rejected"]
    promotion_ready = [record for record in matched if record.signal_summary.get("promotion_ready")]
    demotion_signals: list[str] = []
    for record in records_list:
        demotion_signals.extend(str(signal) for signal in record.signal_summary.get("demotion_signals", []))

    return {
        "observations": len(records_list),
        "matched": len(matched),
        "rejected": len(rejected),
        "promotion_ready": len(promotion_ready),
        "promotion_ready_ids": [record.habit_version_id for record in promotion_ready],
        "demotion_signals": _unique_preserve_order(demotion_signals),
        "fallback_path": "full_pipeline",
    }
