"""Auditable rollback plans and learning safety monitors.

The helpers in this module do not reach into the database. They produce
explicit operations that an admin surface, night worker, or repository layer can
inspect and apply through a caller-supplied executor.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any


ROLLBACK_SCHEMA_VERSION = 1


class RollbackTargetType(StrEnum):
    POLICY_CANDIDATE_APPLICATION = "policy_candidate_application"
    SKILL_VERSION_AUTO_UPDATE = "skill_version_auto_update"
    SKILL_GRADUATION = "skill_graduation"
    MEMORY_SUPERSESSION_BATCH = "memory_supersession_batch"


class RollbackOperationAction(StrEnum):
    MARK_ROLLED_BACK = "mark_rolled_back"
    RESTORE_FIELDS = "restore_fields"
    UPDATE_FIELDS = "update_fields"
    APPEND_AUDIT_EVENT = "append_audit_event"


class SafetyMonitorKind(StrEnum):
    VERIFIER_FAILURE_INCREASE = "verifier_failure_increase"
    USER_CORRECTION_RATE_INCREASE = "user_correction_rate_increase"
    FALLBACK_RATE_INCREASE = "fallback_rate_increase"
    BUDGET_OVERRUN = "budget_overrun"


RollbackExecutor = Callable[["RollbackOperation"], Mapping[str, Any] | None]


@dataclass(frozen=True)
class RollbackOperation:
    """One explicit non-destructive operation in a rollback plan."""

    action: RollbackOperationAction | str
    target_type: str
    target_ref: str
    set_fields: Mapping[str, Any] = field(default_factory=dict)
    restore_fields: Mapping[str, Any] = field(default_factory=dict)
    expected_current_fields: Mapping[str, Any] = field(default_factory=dict)
    audit: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    destructive: bool = False

    def __post_init__(self) -> None:
        if self.destructive:
            raise ValueError("rollback operations must be non-destructive")
        action = self.action.value if isinstance(self.action, RollbackOperationAction) else str(self.action)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "target_type", str(self.target_type or "").strip())
        object.__setattr__(self, "target_ref", str(self.target_ref or "").strip())
        object.__setattr__(self, "set_fields", _mapping(self.set_fields))
        object.__setattr__(self, "restore_fields", _mapping(self.restore_fields))
        object.__setattr__(self, "expected_current_fields", _mapping(self.expected_current_fields))
        object.__setattr__(self, "audit", _mapping(self.audit))
        object.__setattr__(self, "metadata", _mapping(self.metadata))
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        if not self.target_type or not self.target_ref:
            raise ValueError("rollback operation requires target_type and target_ref")

    @property
    def operation_id(self) -> str:
        return _stable_digest(
            {
                "action": self.action,
                "target_type": self.target_type,
                "target_ref": self.target_ref,
                "set_fields": self.set_fields,
                "restore_fields": self.restore_fields,
                "expected_current_fields": self.expected_current_fields,
            }
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "action": self.action,
            "target_type": self.target_type,
            "target_ref": self.target_ref,
            "set_fields": _jsonable(self.set_fields),
            "restore_fields": _jsonable(self.restore_fields),
            "expected_current_fields": _jsonable(self.expected_current_fields),
            "audit": _jsonable(self.audit),
            "reason": self.reason,
            "metadata": _jsonable(self.metadata),
            "destructive": False,
        }


@dataclass(frozen=True)
class SafetyMonitorThresholds:
    verifier_failure_rate_delta: float = 0.12
    verifier_failure_rate_ratio: float = 1.5
    user_correction_rate_delta: float = 0.08
    user_correction_rate_ratio: float = 1.5
    fallback_rate_delta: float = 0.10
    fallback_rate_ratio: float = 1.5
    budget_overrun_ratio: float = 1.0


@dataclass(frozen=True)
class SafetyMonitorFinding:
    kind: SafetyMonitorKind | str
    triggered: bool
    severity: str
    reason: str
    baseline_rate: float | None = None
    current_rate: float | None = None
    threshold: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        kind = self.kind.value if isinstance(self.kind, SafetyMonitorKind) else str(self.kind)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "severity", str(self.severity or "ok"))
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(self, "metadata", _mapping(self.metadata))

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "triggered": self.triggered,
            "severity": self.severity,
            "reason": self.reason,
            "baseline_rate": self.baseline_rate,
            "current_rate": self.current_rate,
            "threshold": self.threshold,
            "metadata": _jsonable(self.metadata),
        }


@dataclass(frozen=True)
class RollbackPlan:
    """A stable, inspectable plan for rolling back one active learning change."""

    target_type: RollbackTargetType | str
    target_ref: str
    reason: str
    operations: tuple[RollbackOperation, ...]
    safety_findings: tuple[SafetyMonitorFinding, ...] = field(default_factory=tuple)
    requested_by: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = ROLLBACK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        target_type = self.target_type.value if isinstance(self.target_type, RollbackTargetType) else str(self.target_type)
        object.__setattr__(self, "target_type", target_type)
        object.__setattr__(self, "target_ref", str(self.target_ref or "").strip())
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(self, "operations", tuple(self.operations or ()))
        object.__setattr__(self, "safety_findings", tuple(self.safety_findings or ()))
        object.__setattr__(self, "metadata", _mapping(self.metadata))
        created_at = self.created_at if self.created_at.tzinfo else self.created_at.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "created_at", created_at)
        if not self.target_ref:
            raise ValueError("rollback plan requires target_ref")
        if not self.reason:
            raise ValueError("rollback plan requires reason")
        if not self.operations:
            raise ValueError("rollback plan requires at least one operation")

    @property
    def plan_id(self) -> str:
        return _stable_digest(
            {
                "target_type": self.target_type,
                "target_ref": self.target_ref,
                "reason": self.reason,
                "operation_ids": [operation.operation_id for operation in self.operations],
            }
        )

    @property
    def triggered_safety_findings(self) -> tuple[SafetyMonitorFinding, ...]:
        return tuple(finding for finding in self.safety_findings if finding.triggered)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "target_type": self.target_type,
            "target_ref": self.target_ref,
            "reason": self.reason,
            "requested_by": self.requested_by,
            "created_at": self.created_at.isoformat(),
            "operations": [operation.to_payload() for operation in self.operations],
            "safety_findings": [finding.to_payload() for finding in self.safety_findings],
            "metadata": _jsonable(self.metadata),
        }


@dataclass(frozen=True)
class RollbackApplyResult:
    plan: RollbackPlan
    receipts: tuple[Mapping[str, Any], ...]
    dry_run: bool = True

    @property
    def applied_count(self) -> int:
        return sum(1 for receipt in self.receipts if receipt.get("status") == "applied")

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan.plan_id,
            "dry_run": self.dry_run,
            "applied_count": self.applied_count,
            "receipts": [_jsonable(dict(receipt)) for receipt in self.receipts],
        }


def build_policy_candidate_application_rollback(
    candidate: Any,
    *,
    reason: str,
    previous_active_candidate: Any | None = None,
    requested_by: str | None = None,
    safety_findings: Sequence[SafetyMonitorFinding] | None = None,
    created_at: datetime | None = None,
) -> RollbackPlan:
    """Plan rollback of an applied policy update candidate."""

    clock = _clock(created_at)
    candidate_data = _object_to_dict(candidate)
    candidate_ref = _target_ref(candidate_data, fallback_prefix="policy_candidate")
    operations = [
        RollbackOperation(
            action=RollbackOperationAction.MARK_ROLLED_BACK,
            target_type="policy_update_candidate",
            target_ref=candidate_ref,
            set_fields={
                "status": "rolled_back",
                "rolled_back_at": clock.isoformat(),
            },
            expected_current_fields=_field_subset(candidate_data, ("status", "review_status", "applied_at")),
            audit={
                "previous_fields": _field_subset(
                    candidate_data,
                    ("status", "review_status", "applied_at", "rolled_back_at"),
                ),
                "candidate_digest": candidate_data.get("candidate_digest"),
            },
            reason=reason,
        )
    ]
    previous_data = _object_to_dict(previous_active_candidate)
    if previous_data:
        previous_ref = _target_ref(previous_data, fallback_prefix="policy_candidate")
        restore_fields = _field_subset(previous_data, ("status", "review_status", "applied_at", "rolled_back_at"))
        if "status" not in restore_fields:
            restore_fields["status"] = "active"
        operations.append(
            RollbackOperation(
                action=RollbackOperationAction.RESTORE_FIELDS,
                target_type="policy_update_candidate",
                target_ref=previous_ref,
                set_fields=restore_fields,
                restore_fields=restore_fields,
                audit={"restores_previous_active_candidate": True},
                reason=reason,
            )
        )
    return RollbackPlan(
        target_type=RollbackTargetType.POLICY_CANDIDATE_APPLICATION,
        target_ref=candidate_ref,
        reason=reason,
        operations=tuple(operations),
        safety_findings=tuple(safety_findings or ()),
        requested_by=requested_by,
        created_at=clock,
        metadata={"preserves_policy_candidate": True},
    )


def build_skill_version_auto_update_rollback(
    skill: Any,
    *,
    previous_version: Any,
    applied_version: Any | None = None,
    reason: str,
    requested_by: str | None = None,
    safety_findings: Sequence[SafetyMonitorFinding] | None = None,
    created_at: datetime | None = None,
) -> RollbackPlan:
    """Plan rollback of an automatic skill version or bundle update."""

    clock = _clock(created_at)
    skill_data = _object_to_dict(skill)
    previous_data = _object_to_dict(previous_version)
    applied_data = _object_to_dict(applied_version) or skill_data
    skill_ref = _target_ref(skill_data, fallback_prefix="skill")
    restore_fields = _field_subset(
        previous_data,
        (
            "version",
            "bundle_version_id",
            "bundle_digest",
            "effective_digest",
            "skill_effective_digest",
            "semver",
            "content_digest",
        ),
    ) or previous_data
    operation = RollbackOperation(
        action=RollbackOperationAction.RESTORE_FIELDS,
        target_type="skill",
        target_ref=skill_ref,
        set_fields=restore_fields,
        restore_fields=restore_fields,
        expected_current_fields=_field_subset(
            applied_data,
            (
                "version",
                "bundle_version_id",
                "bundle_digest",
                "effective_digest",
                "skill_effective_digest",
                "semver",
                "content_digest",
            ),
        ),
        audit={
            "auto_update_rolled_back_at": clock.isoformat(),
            "applied_version": applied_data,
            "previous_version": previous_data,
        },
        reason=reason,
        metadata={"preserves_skill_history": True},
    )
    return RollbackPlan(
        target_type=RollbackTargetType.SKILL_VERSION_AUTO_UPDATE,
        target_ref=skill_ref,
        reason=reason,
        operations=(operation,),
        safety_findings=tuple(safety_findings or ()),
        requested_by=requested_by,
        created_at=clock,
        metadata={"preserves_skill_version_rows": True},
    )


def build_skill_graduation_rollback(
    skill: Any,
    *,
    previous_fields: Mapping[str, Any],
    graduation_update: Mapping[str, Any] | None = None,
    reason: str,
    requested_by: str | None = None,
    safety_findings: Sequence[SafetyMonitorFinding] | None = None,
    created_at: datetime | None = None,
) -> RollbackPlan:
    """Plan rollback of a skill graduation without deleting evidence."""

    clock = _clock(created_at)
    skill_data = _object_to_dict(skill)
    skill_ref = _target_ref(skill_data, fallback_prefix="skill")
    restore_fields = _mapping(previous_fields)
    if not restore_fields:
        raise ValueError("skill graduation rollback requires previous_fields")
    operation = RollbackOperation(
        action=RollbackOperationAction.RESTORE_FIELDS,
        target_type="skill",
        target_ref=skill_ref,
        set_fields=restore_fields,
        restore_fields=restore_fields,
        expected_current_fields=_mapping(graduation_update),
        audit={
            "graduation_update": _mapping(graduation_update),
            "graduation_rolled_back_at": clock.isoformat(),
        },
        reason=reason,
        metadata={"preserves_graduation_evidence": True},
    )
    return RollbackPlan(
        target_type=RollbackTargetType.SKILL_GRADUATION,
        target_ref=skill_ref,
        reason=reason,
        operations=(operation,),
        safety_findings=tuple(safety_findings or ()),
        requested_by=requested_by,
        created_at=clock,
        metadata={"preserves_skill_graduation_evidence": True},
    )


def build_memory_supersession_batch_rollback(
    supersession_actions: Sequence[Any],
    *,
    reason: str,
    requested_by: str | None = None,
    safety_findings: Sequence[SafetyMonitorFinding] | None = None,
    created_at: datetime | None = None,
) -> RollbackPlan:
    """Plan rollback of a memory supersession batch from action metadata."""

    clock = _clock(created_at)
    operations: list[RollbackOperation] = []
    source_keys: list[str] = []
    for index, action in enumerate(supersession_actions or (), start=1):
        action_data = _object_to_dict(action)
        rollback_metadata = _mapping(action_data.get("rollback_metadata"))
        if not rollback_metadata:
            continue
        source_key = str(rollback_metadata.get("idempotency_key") or action_data.get("idempotency_key") or index)
        source_keys.append(source_key)
        restore_fields_by_id = _mapping(rollback_metadata.get("restore_fields"))
        affected_ids = _as_sequence(rollback_metadata.get("affected_memory_ids"))
        for memory_id in affected_ids:
            memory_ref = str(memory_id)
            restore_fields = _mapping(restore_fields_by_id.get(memory_ref))
            operations.append(
                RollbackOperation(
                    action=RollbackOperationAction.RESTORE_FIELDS,
                    target_type="memory",
                    target_ref=memory_ref,
                    set_fields=restore_fields,
                    restore_fields=restore_fields,
                    audit={
                        "source_action": action_data.get("action"),
                        "source_idempotency_key": source_key,
                        "rollback_metadata": rollback_metadata,
                    },
                    reason=reason,
                    metadata={"preserves_memory_content": True},
                )
            )
    batch_ref = _stable_digest({"memory_supersession_batch": source_keys})
    return RollbackPlan(
        target_type=RollbackTargetType.MEMORY_SUPERSESSION_BATCH,
        target_ref=batch_ref,
        reason=reason,
        operations=tuple(operations),
        safety_findings=tuple(safety_findings or ()),
        requested_by=requested_by,
        created_at=clock,
        metadata={
            "source_action_count": len(supersession_actions or ()),
            "preserves_memory_content": True,
        },
    )


def apply_rollback_plan(
    plan: RollbackPlan,
    *,
    executor: RollbackExecutor | None = None,
    dry_run: bool | None = None,
) -> RollbackApplyResult:
    """Apply a plan through an explicit executor or return dry-run receipts."""

    resolved_dry_run = executor is None if dry_run is None else bool(dry_run)
    receipts: list[Mapping[str, Any]] = []
    for operation in plan.operations:
        if resolved_dry_run:
            receipts.append(
                {
                    "operation_id": operation.operation_id,
                    "status": "planned",
                    "target_type": operation.target_type,
                    "target_ref": operation.target_ref,
                    "operation": operation.to_payload(),
                }
            )
            continue
        if executor is None:
            raise ValueError("executor is required when dry_run is false")
        result = executor(operation) or {}
        receipts.append(
            {
                "operation_id": operation.operation_id,
                "status": "applied",
                "target_type": operation.target_type,
                "target_ref": operation.target_ref,
                "executor_result": _jsonable(result),
            }
        )
    return RollbackApplyResult(plan=plan, receipts=tuple(receipts), dry_run=resolved_dry_run)


def apply_policy_candidate_application_rollback(
    plan: RollbackPlan,
    *,
    executor: RollbackExecutor | None = None,
    dry_run: bool | None = None,
) -> RollbackApplyResult:
    _require_plan_type(plan, RollbackTargetType.POLICY_CANDIDATE_APPLICATION)
    return apply_rollback_plan(plan, executor=executor, dry_run=dry_run)


def apply_skill_version_auto_update_rollback(
    plan: RollbackPlan,
    *,
    executor: RollbackExecutor | None = None,
    dry_run: bool | None = None,
) -> RollbackApplyResult:
    _require_plan_type(plan, RollbackTargetType.SKILL_VERSION_AUTO_UPDATE)
    return apply_rollback_plan(plan, executor=executor, dry_run=dry_run)


def apply_skill_graduation_rollback(
    plan: RollbackPlan,
    *,
    executor: RollbackExecutor | None = None,
    dry_run: bool | None = None,
) -> RollbackApplyResult:
    _require_plan_type(plan, RollbackTargetType.SKILL_GRADUATION)
    return apply_rollback_plan(plan, executor=executor, dry_run=dry_run)


def apply_memory_supersession_batch_rollback(
    plan: RollbackPlan,
    *,
    executor: RollbackExecutor | None = None,
    dry_run: bool | None = None,
) -> RollbackApplyResult:
    _require_plan_type(plan, RollbackTargetType.MEMORY_SUPERSESSION_BATCH)
    return apply_rollback_plan(plan, executor=executor, dry_run=dry_run)


def evaluate_safety_monitors(
    metrics: Mapping[str, Any],
    *,
    thresholds: SafetyMonitorThresholds | None = None,
) -> tuple[SafetyMonitorFinding, ...]:
    """Evaluate learning rollback safety monitors from aggregate metrics."""

    thresholds = thresholds or SafetyMonitorThresholds()
    findings = [
        _rate_finding(
            metrics,
            kind=SafetyMonitorKind.VERIFIER_FAILURE_INCREASE,
            reason="sudden verifier failure increase",
            delta_threshold=thresholds.verifier_failure_rate_delta,
            ratio_threshold=thresholds.verifier_failure_rate_ratio,
            direct_baseline=("verifier_failure_rate_baseline", "baseline_verifier_failure_rate"),
            direct_current=("verifier_failure_rate_current", "current_verifier_failure_rate"),
            baseline_count=("verifier_failures_baseline", "baseline_verifier_failures"),
            baseline_total=("verifier_checks_baseline", "baseline_verifier_checks"),
            current_count=("verifier_failures_current", "current_verifier_failures"),
            current_total=("verifier_checks_current", "current_verifier_checks"),
        ),
        _rate_finding(
            metrics,
            kind=SafetyMonitorKind.USER_CORRECTION_RATE_INCREASE,
            reason="rising user correction rate",
            delta_threshold=thresholds.user_correction_rate_delta,
            ratio_threshold=thresholds.user_correction_rate_ratio,
            direct_baseline=("user_correction_rate_baseline", "baseline_user_correction_rate"),
            direct_current=("user_correction_rate_current", "current_user_correction_rate"),
            baseline_count=("user_corrections_baseline", "baseline_user_corrections"),
            baseline_total=("user_interactions_baseline", "baseline_user_interactions", "runs_baseline"),
            current_count=("user_corrections_current", "current_user_corrections"),
            current_total=("user_interactions_current", "current_user_interactions", "runs_current"),
        ),
        _rate_finding(
            metrics,
            kind=SafetyMonitorKind.FALLBACK_RATE_INCREASE,
            reason="rising fallback rate",
            delta_threshold=thresholds.fallback_rate_delta,
            ratio_threshold=thresholds.fallback_rate_ratio,
            direct_baseline=("fallback_rate_baseline", "baseline_fallback_rate"),
            direct_current=("fallback_rate_current", "current_fallback_rate"),
            baseline_count=("fallbacks_baseline", "baseline_fallbacks"),
            baseline_total=("fallback_opportunities_baseline", "baseline_fallback_opportunities", "runs_baseline"),
            current_count=("fallbacks_current", "current_fallbacks"),
            current_total=("fallback_opportunities_current", "current_fallback_opportunities", "runs_current"),
        ),
        _budget_finding(metrics, thresholds),
    ]
    return tuple(findings)


def triggered_safety_findings(findings: Sequence[SafetyMonitorFinding]) -> tuple[SafetyMonitorFinding, ...]:
    return tuple(finding for finding in findings if finding.triggered)


def _rate_finding(
    metrics: Mapping[str, Any],
    *,
    kind: SafetyMonitorKind,
    reason: str,
    delta_threshold: float,
    ratio_threshold: float,
    direct_baseline: Sequence[str],
    direct_current: Sequence[str],
    baseline_count: Sequence[str],
    baseline_total: Sequence[str],
    current_count: Sequence[str],
    current_total: Sequence[str],
) -> SafetyMonitorFinding:
    baseline = _rate(metrics, direct_baseline, baseline_count, baseline_total)
    current = _rate(metrics, direct_current, current_count, current_total)
    if baseline is None or current is None:
        return SafetyMonitorFinding(
            kind=kind,
            triggered=False,
            severity="unknown",
            reason=f"{reason}: insufficient data",
            threshold=delta_threshold,
        )
    delta = current - baseline
    ratio = current / baseline if baseline > 0 else float("inf") if current > 0 else 1.0
    triggered = delta >= delta_threshold or (current >= delta_threshold and ratio >= ratio_threshold)
    severity = "critical" if triggered and (delta >= delta_threshold * 2 or current >= 0.5) else "warning" if triggered else "ok"
    return SafetyMonitorFinding(
        kind=kind,
        triggered=triggered,
        severity=severity,
        reason=reason if triggered else f"{reason}: within threshold",
        baseline_rate=round(baseline, 4),
        current_rate=round(current, 4),
        threshold=delta_threshold,
        metadata={"delta": round(delta, 4), "ratio": round(ratio, 4) if ratio != float("inf") else "inf"},
    )


def _budget_finding(metrics: Mapping[str, Any], thresholds: SafetyMonitorThresholds) -> SafetyMonitorFinding:
    used = _float(_first_metric(metrics, "budget_used_units", "night_budget_used_units", "learning_budget_used_units"))
    limit = _float(_first_metric(metrics, "budget_limit_units", "night_budget_limit_units", "learning_budget_limit_units"))
    if used is None or limit is None or limit <= 0:
        return SafetyMonitorFinding(
            kind=SafetyMonitorKind.BUDGET_OVERRUN,
            triggered=False,
            severity="unknown",
            reason="budget overrun: insufficient data",
            threshold=thresholds.budget_overrun_ratio,
        )
    ratio = used / limit
    triggered = ratio > thresholds.budget_overrun_ratio
    return SafetyMonitorFinding(
        kind=SafetyMonitorKind.BUDGET_OVERRUN,
        triggered=triggered,
        severity="critical" if ratio >= thresholds.budget_overrun_ratio * 1.2 else "warning" if triggered else "ok",
        reason="budget overrun" if triggered else "budget usage within limit",
        baseline_rate=1.0,
        current_rate=round(ratio, 4),
        threshold=thresholds.budget_overrun_ratio,
        metadata={"used_units": used, "limit_units": limit},
    )


def _require_plan_type(plan: RollbackPlan, expected: RollbackTargetType) -> None:
    if str(plan.target_type) != expected.value:
        raise ValueError(f"expected {expected.value} rollback plan, got {plan.target_type}")


def _clock(value: datetime | None) -> datetime:
    clock = value or datetime.now(timezone.utc)
    return clock if clock.tzinfo else clock.replace(tzinfo=timezone.utc)


def _object_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        result = value.to_dict()
        return dict(result) if isinstance(result, Mapping) else {}
    if hasattr(value, "to_payload") and callable(value.to_payload):
        result = value.to_payload()
        return dict(result) if isinstance(result, Mapping) else {}
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return {}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)):
        return (value,)
    try:
        return tuple(value)
    except TypeError:
        return (value,)


def _target_ref(data: Mapping[str, Any], *, fallback_prefix: str) -> str:
    for key in (
        "id",
        "candidate_digest",
        "digest",
        "skill_id",
        "skill_name",
        "name",
        "effective_digest",
        "bundle_digest",
    ):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return f"{fallback_prefix}:{_stable_digest(data)}"


def _field_subset(data: Mapping[str, Any], names: Sequence[str]) -> dict[str, Any]:
    return {name: data[name] for name in names if name in data}


def _rate(
    metrics: Mapping[str, Any],
    direct_names: Sequence[str],
    count_names: Sequence[str],
    total_names: Sequence[str],
) -> float | None:
    direct = _float(_first_metric(metrics, *direct_names))
    if direct is not None:
        return _clamp_rate(direct)
    count = _float(_first_metric(metrics, *count_names))
    total = _float(_first_metric(metrics, *total_names))
    if count is None or total is None or total <= 0:
        return None
    return _clamp_rate(count / total)


def _first_metric(metrics: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in metrics and metrics[name] not in (None, ""):
            return metrics[name]
    return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp_rate(value: float) -> float:
    return max(0.0, min(1.0, value))


def _stable_digest(value: Any) -> str:
    raw = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "ROLLBACK_SCHEMA_VERSION",
    "RollbackApplyResult",
    "RollbackExecutor",
    "RollbackOperation",
    "RollbackOperationAction",
    "RollbackPlan",
    "RollbackTargetType",
    "SafetyMonitorFinding",
    "SafetyMonitorKind",
    "SafetyMonitorThresholds",
    "apply_memory_supersession_batch_rollback",
    "apply_policy_candidate_application_rollback",
    "apply_rollback_plan",
    "apply_skill_graduation_rollback",
    "apply_skill_version_auto_update_rollback",
    "build_memory_supersession_batch_rollback",
    "build_policy_candidate_application_rollback",
    "build_skill_graduation_rollback",
    "build_skill_version_auto_update_rollback",
    "evaluate_safety_monitors",
    "triggered_safety_findings",
]
