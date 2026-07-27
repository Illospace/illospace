"""Read projections over persisted tool events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
from typing import Any

from sqlalchemy import case, func, or_, select

from brain.platform.db.models.agent_run import AgentRunEventRow
from brain.systems.runs.tool_catalog.metadata import ToolSideEffectClass


_TOOL_RESULT_EVENT_TYPES = ("run.tool_completed", "run.tool_failed")
_LEGACY_SIDE_EFFECT_CLASSES = frozenset(
    {"read", "write", "command", "chat_message", "external", "unknown"}
)
_KNOWN_SIDE_EFFECT_CLASSES = frozenset(
    side_effect_class.value for side_effect_class in ToolSideEffectClass
) | _LEGACY_SIDE_EFFECT_CLASSES
_NON_WRITE_SIDE_EFFECT_CLASSES = frozenset(
    {
        "read",
        ToolSideEffectClass.READ_ONLY.value,
        ToolSideEffectClass.READ_ONLY_EXTERNAL.value,
    }
)


@dataclass(frozen=True)
class PersistedToolSideEffect:
    """Compatibility projection for current and historical event metadata."""

    side_effect: str
    is_write: bool


def parse_persisted_tool_side_effect(payload: Any) -> PersistedToolSideEffect:
    """Parse untrusted stored event metadata without inventing a known class."""
    metadata = payload if isinstance(payload, Mapping) else {}
    raw_side_effect = metadata.get("side_effect")
    side_effect = (
        raw_side_effect
        if isinstance(raw_side_effect, str)
        and raw_side_effect in _KNOWN_SIDE_EFFECT_CLASSES
        else "unknown"
    )
    stored_is_write = metadata.get("is_write")
    is_write = (
        stored_is_write
        if isinstance(stored_is_write, bool)
        else side_effect not in _NON_WRITE_SIDE_EFFECT_CLASSES
    )
    return PersistedToolSideEffect(
        side_effect=side_effect,
        is_write=is_write,
    )


def _session_dialect_name(session: Any) -> str:
    try:
        return str(session.get_bind().dialect.name)
    except Exception:
        return "postgresql"


def _persisted_is_write_expression(*, dialect_name: str) -> Any:
    payload = AgentRunEventRow.payload
    stored_is_write = payload["is_write"].as_boolean()
    if dialect_name == "sqlite":
        stored_is_boolean = func.json_type(payload, "$.is_write").in_(
            ("true", "false")
        )
    else:
        stored_is_boolean = (
            func.jsonb_typeof(payload["is_write"]) == "boolean"
        )

    side_effect = payload["side_effect"].as_string()
    legacy_fallback = or_(
        side_effect.is_(None),
        side_effect.not_in(_NON_WRITE_SIDE_EFFECT_CLASSES),
    )
    return case(
        (stored_is_boolean, stored_is_write),
        else_=legacy_fallback,
    ).is_(True)


def _tool_call_summary_statement(run_id: int, *, dialect_name: str) -> Any:
    return (
        select(AgentRunEventRow)
        .where(
            AgentRunEventRow.run_id == int(run_id),
            AgentRunEventRow.event_type.in_(_TOOL_RESULT_EVENT_TYPES),
            _persisted_is_write_expression(dialect_name=dialect_name),
        )
        .order_by(
            AgentRunEventRow.created_at.desc(),
            AgentRunEventRow.id.desc(),
        )
        .limit(1)
    )


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def tool_call_summary(
    session: Any,
    run_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the latest write-class tool-call timing for one complete run."""
    statement = _tool_call_summary_statement(
        run_id,
        dialect_name=_session_dialect_name(session),
    )
    event = await _maybe_await(session.scalar(statement))

    last_write_at: datetime | None = None
    if event is not None:
        side_effect = parse_persisted_tool_side_effect(
            getattr(event, "payload", None)
        )
        created_at = getattr(event, "created_at", None)
        if side_effect.is_write and isinstance(created_at, datetime):
            last_write_at = _as_utc(created_at)

    if last_write_at is None:
        return {
            "run_id": int(run_id),
            "last_write_tool_call_at": None,
            "seconds_since_last_write_tool_call": None,
        }

    reference = _as_utc(now or datetime.now(timezone.utc))
    return {
        "run_id": int(run_id),
        "last_write_tool_call_at": last_write_at.isoformat(),
        "seconds_since_last_write_tool_call": max(
            0,
            int((reference - last_write_at).total_seconds()),
        ),
    }


__all__ = [
    "PersistedToolSideEffect",
    "parse_persisted_tool_side_effect",
    "tool_call_summary",
]
