"""Shared Cycle constants and small value helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_
from sqlalchemy.sql.elements import ColumnElement

from brain.platform.db.models.cycle import Cycle
from brain.platform.providers.model_policy import (
    EFFORT_TIER_SET,
    PROVIDER_MODEL_OPTIONS,
    normalize_model_name,
)

REUSABLE_THREAD_EXECUTION_MODE = "reuse_same_idea"
VALID_THINKING_OVERRIDES = EFFORT_TIER_SET
CYCLE_LEDGER_OUTPUT_TARGET_TYPE = "cycle_ledger"
THREAD_OUTPUT_TARGET_TYPE = "thread"
SCHEDULED_CYCLE_ORIGIN = "scheduled_cycle"
MANUAL_CYCLE_ORIGIN = "manual_cycle"
AGENT_TRIGGERED_CYCLE_ORIGIN = "agent_triggered_cycle"
EXTERNAL_AGENT_TRIGGERED_CYCLE_ORIGIN = "external_agent_triggered_cycle"
SCHEDULED_DIGEST_RUN_KIND = "scheduled_digest"
OFF_SLOT_MATERIAL_ALERT_RUN_KIND = "off_slot_material_alert"
MIN_CYCLE_TIMEOUT_SECONDS = 60
MAX_CYCLE_TIMEOUT_SECONDS = 14_400
ILLO_LANE_EXECUTOR_BINDING = "illo-lane"
PERSONAL_AGENT_EXECUTOR_BINDING = "personal-agent"
VALID_CYCLE_EXECUTOR_BINDINGS = frozenset(
    {ILLO_LANE_EXECUTOR_BINDING, PERSONAL_AGENT_EXECUTOR_BINDING}
)

_VALID_MODEL_OVERRIDES = frozenset(
    f"{provider}/{model}"
    for provider, models in PROVIDER_MODEL_OPTIONS.items()
    for model in models
)


def validate_nonempty_trimmed(value: str, field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def validate_cycle_timeout_seconds(value: int | None) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_CYCLE_TIMEOUT_SECONDS <= value <= MAX_CYCLE_TIMEOUT_SECONDS
    ):
        raise ValueError(
            "timeout_seconds must be an integer between "
            f"{MIN_CYCLE_TIMEOUT_SECONDS} and {MAX_CYCLE_TIMEOUT_SECONDS}, or null"
        )
    return value


def validate_executor_binding(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in VALID_CYCLE_EXECUTOR_BINDINGS:
        raise ValueError(
            "executor_binding must be one of: "
            + ", ".join(sorted(VALID_CYCLE_EXECUTOR_BINDINGS))
        )
    return normalized


def cycle_executor_binding(cycle) -> str:
    return validate_executor_binding(
        getattr(cycle, "executor_binding", None) or ILLO_LANE_EXECUTOR_BINDING
    )


def due_illo_lane_cycle_clause(
    cutoff: datetime, *, inclusive: bool
) -> ColumnElement[bool]:
    """Select enabled, scheduled illo-lane cycles due by ``cutoff``."""
    next_run_at_clause = (
        Cycle.next_run_at <= cutoff if inclusive else Cycle.next_run_at < cutoff
    )
    return and_(
        Cycle.deleted_at.is_(None),
        Cycle.enabled.is_(True),
        Cycle.executor_binding == ILLO_LANE_EXECUTOR_BINDING,
        Cycle.next_run_at.is_not(None),
        next_run_at_clause,
    )


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


def validate_model_override(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw or raw.lower() == "default":
        return None

    candidate = raw.replace(":", "/", 1)
    if "/" not in candidate:
        matches = [
            f"{provider}/{candidate}"
            for provider, models in PROVIDER_MODEL_OPTIONS.items()
            if candidate in models
        ]
        candidate = matches[0] if len(matches) == 1 else candidate

    if candidate not in _VALID_MODEL_OVERRIDES:
        raise ValueError(
            f"Unknown model_override {raw!r}. Valid options: "
            f"{', '.join(sorted(_VALID_MODEL_OVERRIDES))}"
        )
    return normalize_model_name(candidate)


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
        "run_kind": SCHEDULED_DIGEST_RUN_KIND,
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
