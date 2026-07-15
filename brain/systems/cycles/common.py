"""Shared Cycle constants and small value helpers."""
from __future__ import annotations

from datetime import datetime, timezone

REUSABLE_THREAD_EXECUTION_MODE = "reuse_same_idea"
VALID_THINKING_OVERRIDES = {"none", "low", "medium", "high", "xhigh"}
CYCLE_LEDGER_OUTPUT_TARGET_TYPE = "cycle_ledger"
THREAD_OUTPUT_TARGET_TYPE = "thread"
SCHEDULED_CYCLE_ORIGIN = "scheduled_cycle"
MANUAL_CYCLE_ORIGIN = "manual_cycle"
AGENT_TRIGGERED_CYCLE_ORIGIN = "agent_triggered_cycle"
EXTERNAL_AGENT_TRIGGERED_CYCLE_ORIGIN = "external_agent_triggered_cycle"


def validate_nonempty_trimmed(value: str, field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def canonical_execution_mode(_mode: str | None = None) -> str:
    return REUSABLE_THREAD_EXECUTION_MODE


def validate_thinking_override(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized not in VALID_THINKING_OVERRIDES:
        raise ValueError(
            f"thinking_override must be one of: {', '.join(sorted(VALID_THINKING_OVERRIDES))}"
        )
    return normalized


def string_or_none(value) -> str | None:
    if value is None:
        return None
    return str(value)


def short_identifier(value, *, length: int = 8) -> str:
    text = string_or_none(value) or ""
    return text[:length]


def required_datetime(*values) -> datetime:
    for value in values:
        if value is not None:
            return value
    return datetime.now(timezone.utc)


def json_list(value) -> list:
    return value if isinstance(value, list) else []


def json_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def cycle_run_launch_context(run) -> dict:
    """Return persisted launch provenance, defaulting legacy rows to scheduler runs."""
    context = json_dict(getattr(run, "context_snapshot", None))
    launch_context = json_dict(context.get("launch_context"))
    if launch_context:
        return launch_context
    return {
        "origin": SCHEDULED_CYCLE_ORIGIN,
        "source": "cycle_scheduler",
    }


def actor_type(value: str | None) -> str:
    return (value or "system").strip() or "system"


def actor_id(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def creator_payload(cycle) -> dict:
    creator_type = getattr(cycle, "creator_type", None) or "user"
    creator_id = getattr(cycle, "creator_id", None) or string_or_none(cycle.user_id)
    maintainer_type = getattr(cycle, "maintainer_type", None) or creator_type
    maintainer_id = getattr(cycle, "maintainer_id", None) or creator_id
    return {
        "creator_type": creator_type,
        "creator_id": string_or_none(creator_id),
        "maintainer_type": maintainer_type,
        "maintainer_id": string_or_none(maintainer_id),
    }
