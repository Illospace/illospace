"""Recurring scheduler task contracts.

Contracts make recurrent business work auditable before it is materialized as a
run or linked to a run. The scheduler still executes deterministic code,
but the contract records the owner, scope, permissions, and success criteria
that the LLM/runtime must respect.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from brain.platform.db.models.scheduler import SchedulerJob, SchedulerRun
from brain.app.scheduler.runtime import normalize_retry_policy

VALID_MEMORY_SCOPES = {"private", "team", "org", "system"}
DEFAULT_OUTPUT_CHANNEL = "scheduler"


@dataclass(frozen=True)
class RecurringTaskContract:
    """Normalized contract persisted on scheduler jobs and materialized runs."""

    owner_user_id: str | None
    org_id: str | None
    schedule: dict[str, Any]
    allowed_actions: tuple[str, ...] = ()
    memory_scope: dict[str, Any] = field(default_factory=dict)
    required_approvals: tuple[str, ...] = ()
    output_channel: str = DEFAULT_OUTPUT_CHANNEL
    timeout_seconds: int | None = None
    retry_policy: dict[str, int] = field(default_factory=dict)
    success_criteria: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_user_id": self.owner_user_id,
            "org_id": self.org_id,
            "schedule": dict(self.schedule),
            "allowed_actions": list(self.allowed_actions),
            "memory_scope": dict(self.memory_scope),
            "required_approvals": list(self.required_approvals),
            "output_channel": self.output_channel,
            "timeout_seconds": self.timeout_seconds,
            "retry_policy": dict(self.retry_policy),
            "success_criteria": list(self.success_criteria),
        }


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_text_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, Iterable):
        return ()
    normalized = []
    for item in value:
        if isinstance(item, dict):
            candidate = item.get("action") or item.get("tool_name") or item.get("name")
        else:
            candidate = item
        text = _as_text(candidate)
        if text:
            normalized.append(text)
    return tuple(dict.fromkeys(normalized))


def _timeout(value: Any, fallback: int | None) -> int | None:
    raw = value if value is not None else fallback
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def normalize_recurring_task_contract(
    job: SchedulerJob,
    *,
    run_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical contract for a job/run pair."""
    payload = run_payload or job.default_payload or {}
    raw = dict(job.task_contract or {})
    owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
    schedule = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else {}
    memory_scope = raw.get("memory_scope") if isinstance(raw.get("memory_scope"), dict) else {}

    contract = RecurringTaskContract(
        owner_user_id=(
            _as_text(raw.get("owner_user_id"))
            or _as_text(owner.get("user_id"))
            or _as_text(payload.get("user_id"))
            or _as_text(payload.get("owner_user_id"))
        ),
        org_id=(
            _as_text(raw.get("org_id"))
            or _as_text(owner.get("org_id"))
            or _as_text(payload.get("org_id"))
        ),
        schedule={
            "cron_expr": _as_text(schedule.get("cron_expr")) or job.cron_expr,
            "timezone": _as_text(schedule.get("timezone")) or job.timezone,
            "job_key": job.job_key,
            "owner_mode": job.owner_mode,
        },
        allowed_actions=_as_text_list(raw.get("allowed_actions") or payload.get("allowed_actions")),
        memory_scope={
            "visibility": _as_text(memory_scope.get("visibility") or payload.get("memory_visibility")) or "private",
            "user_id": _as_text(memory_scope.get("user_id") or payload.get("user_id")),
            "org_id": _as_text(memory_scope.get("org_id") or payload.get("org_id")),
        },
        required_approvals=_as_text_list(raw.get("required_approvals") or payload.get("required_approvals")),
        output_channel=_as_text(raw.get("output_channel") or payload.get("output_channel")) or DEFAULT_OUTPUT_CHANNEL,
        timeout_seconds=_timeout(raw.get("timeout_seconds"), job.timeout_seconds),
        retry_policy=normalize_retry_policy(raw.get("retry_policy") or job.retry_policy),
        success_criteria=_as_text_list(
            raw.get("success_criteria")
            or payload.get("success_criteria")
            or ("Run settles successfully",)
        ),
    )
    return contract.to_dict()


def validate_recurring_task_contract(contract: dict[str, Any]) -> list[str]:
    """Return validation errors for a normalized recurring task contract."""
    errors: list[str] = []
    schedule = contract.get("schedule") if isinstance(contract.get("schedule"), dict) else {}
    if not _as_text(schedule.get("cron_expr")):
        errors.append("schedule.cron_expr is required")
    if not _as_text(schedule.get("timezone")):
        errors.append("schedule.timezone is required")

    memory_scope = contract.get("memory_scope") if isinstance(contract.get("memory_scope"), dict) else {}
    visibility = _as_text(memory_scope.get("visibility")) or "private"
    if visibility not in VALID_MEMORY_SCOPES:
        errors.append(f"memory_scope.visibility must be one of {sorted(VALID_MEMORY_SCOPES)}")
    if visibility == "private" and not _as_text(memory_scope.get("user_id")):
        errors.append("memory_scope.user_id is required for private recurring tasks")
    if visibility in {"team", "org"} and not (
        _as_text(memory_scope.get("org_id")) or _as_text(contract.get("org_id"))
    ):
        errors.append("org_id is required for team/org recurring tasks")

    if not _as_text(contract.get("output_channel")):
        errors.append("output_channel is required")
    if not _as_text_list(contract.get("success_criteria")):
        errors.append("success_criteria is required")
    return errors


def extract_declared_actions(payload: dict[str, Any] | None) -> tuple[str, ...]:
    """Extract requested action/tool names from scheduler payload manifests."""
    if not isinstance(payload, dict):
        return ()
    candidates: list[Any] = []
    for key in ("action_manifest", "action_manifests", "required_actions", "actions"):
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            candidates.extend(value)
        else:
            candidates.append(value)
    return _as_text_list(candidates)


def validate_declared_actions(contract: dict[str, Any], payload: dict[str, Any] | None) -> list[str]:
    """Ensure payload-declared actions are inside the contract action scope."""
    declared = extract_declared_actions(payload)
    if not declared:
        return []
    allowed = set(_as_text_list(contract.get("allowed_actions")))
    if "*" in allowed:
        return []
    missing = [action for action in declared if action not in allowed]
    if not missing:
        return []
    return [f"Action(s) outside recurring task contract: {', '.join(sorted(missing))}"]


def validate_scheduler_run_contract(
    job: SchedulerJob,
    run: SchedulerRun | None = None,
    *,
    payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return the effective contract and validation errors for a run."""
    run_payload = payload if payload is not None else ((run.payload or {}) if run is not None else None)
    contract = dict((run.task_contract if run is not None else None) or normalize_recurring_task_contract(job, run_payload=run_payload))
    errors = validate_recurring_task_contract(contract)
    errors.extend(validate_declared_actions(contract, run_payload))
    return contract, errors
