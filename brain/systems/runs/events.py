"""Run event builders."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from brain.systems.runs.domain import AgentRunEvent, EventVisibility
from brain.systems.runs.tool_catalog.metadata import (
    ToolSideEffectClass,
    coerce_tool_side_effect_class,
    is_write_side_effect_class,
)
from brain.systems.runs.tool_catalog.registry import side_effect_class_for_tool


SECRET_TOOL_NAMES = {"brain_vault", "vault", "secrets"}


def run_event(
    run_id: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    root_run_id: int | None = None,
    producer: str = "agent_runtime",
    visibility: EventVisibility = EventVisibility.PUBLIC,
) -> AgentRunEvent:
    return AgentRunEvent(
        run_id=run_id,
        root_run_id=root_run_id,
        event_type=event_type,
        payload=payload or {},
        producer=producer,
        visibility=visibility,
    )


def activity_event(run_id: int, label: str, *, root_run_id: int | None = None, **payload: Any) -> AgentRunEvent:
    return run_event(run_id, "run.activity", {"label": label, **payload}, root_run_id=root_run_id)


def text_delta_event(run_id: int, delta: str, *, root_run_id: int | None = None) -> AgentRunEvent:
    return run_event(run_id, "run.text_delta", {"delta": delta}, root_run_id=root_run_id)


def status_changed_event(
    run_id: int,
    *,
    from_status: str,
    to_status: str,
    root_run_id: int | None = None,
    reason: str | None = None,
) -> AgentRunEvent:
    payload = {"from_status": from_status, "to_status": to_status}
    if reason:
        payload["reason"] = reason
    return run_event(run_id, "run.status_changed", payload, root_run_id=root_run_id)


def redact_tool_call_result(tool_name: str, result: Any) -> str:
    if str(tool_name or "").lower() in SECRET_TOOL_NAMES:
        return "[secret redacted]"
    return str(result or "")


def tool_call_completed_payload(
    idea_id: str | None,
    tool_name: str,
    args: dict[str, Any] | None,
    result: Any,
    *,
    source: str = "runner",
    side_effect: ToolSideEffectClass | str | None = None,
) -> dict[str, Any]:
    side_effect_class = (
        side_effect_class_for_tool(tool_name)
        if side_effect is None
        else coerce_tool_side_effect_class(side_effect)
    )
    return {
        "idea_id": idea_id,
        "tool_name": tool_name,
        "args": args or {},
        "result": redact_tool_call_result(tool_name, result),
        "source": source,
        "side_effect": side_effect_class.value,
        "is_write": is_write_side_effect_class(side_effect_class),
    }


async def async_record_tool_call(
    run_id: int,
    idea_id: str | None,
    tool_name: str,
    args: dict[str, Any] | None,
    result: Any,
    *,
    source: str = "runner",
    side_effect: ToolSideEffectClass | str | None = None,
    **_: Any,
) -> None:
    from brain.systems.runs.event_log import async_record_run_event

    await async_record_run_event(
        int(run_id),
        "run.tool_completed",
        tool_call_completed_payload(
            idea_id,
            tool_name,
            args,
            result,
            source=source,
            side_effect=side_effect,
        ),
        producer=source or "runner",
    )


async def async_record_tool_activity(
    run_id: int,
    tool_name: str,
    args: dict[str, Any] | None = None,
    result: Any = None,
    *,
    source: str = "runner",
    **kwargs: Any,
) -> None:
    await async_record_tool_call(
        run_id,
        kwargs.get("idea_id"),
        tool_name,
        args or {},
        result,
        source=source,
        side_effect=kwargs.get("side_effect"),
    )


def tool_call_write_timing(
    events: Iterable[Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Summarize the most recent stored write-class completed tool event."""
    last_write_at: datetime | None = None
    for event in events:
        if str(getattr(event, "event_type", "") or "") not in {
            "run.tool_completed",
            "run.tool_failed",
        }:
            continue
        payload = getattr(event, "payload", None)
        payload = payload if isinstance(payload, dict) else {}
        stored_is_write = payload.get("is_write")
        is_write = (
            stored_is_write
            if isinstance(stored_is_write, bool)
            else is_write_side_effect_class(payload.get("side_effect"))
        )
        called_at = getattr(event, "created_at", None)
        if not is_write or not isinstance(called_at, datetime):
            continue
        if called_at.tzinfo is None:
            called_at = called_at.replace(tzinfo=timezone.utc)
        else:
            called_at = called_at.astimezone(timezone.utc)
        if last_write_at is None or called_at > last_write_at:
            last_write_at = called_at

    if last_write_at is None:
        return {
            "last_write_tool_call_at": None,
            "seconds_since_last_write_tool_call": None,
        }
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    else:
        reference = reference.astimezone(timezone.utc)
    return {
        "last_write_tool_call_at": last_write_at.isoformat(),
        "seconds_since_last_write_tool_call": max(
            0,
            int((reference - last_write_at).total_seconds()),
        ),
    }


__all__ = [
    "activity_event",
    "async_record_tool_activity",
    "async_record_tool_call",
    "redact_tool_call_result",
    "run_event",
    "status_changed_event",
    "text_delta_event",
    "tool_call_completed_payload",
    "tool_call_write_timing",
]
