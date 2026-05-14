"""Run event builders."""

from __future__ import annotations

from typing import Any

from brain.systems.runs.domain import AgentRunEvent, EventVisibility


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
) -> dict[str, Any]:
    return {
        "idea_id": idea_id,
        "tool_name": tool_name,
        "args": args or {},
        "result": redact_tool_call_result(tool_name, result),
        "source": source,
    }


async def async_record_tool_call(
    run_id: int,
    idea_id: str | None,
    tool_name: str,
    args: dict[str, Any] | None,
    result: Any,
    *,
    source: str = "runner",
    **_: Any,
) -> None:
    from brain.systems.runs.event_log import async_record_run_event

    await async_record_run_event(
        int(run_id),
        "run.tool_completed",
        tool_call_completed_payload(idea_id, tool_name, args, result, source=source),
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
    await async_record_tool_call(run_id, kwargs.get("idea_id"), tool_name, args or {}, result, source=source)


__all__ = [
    "activity_event",
    "async_record_tool_activity",
    "async_record_tool_call",
    "redact_tool_call_result",
    "run_event",
    "status_changed_event",
    "text_delta_event",
    "tool_call_completed_payload",
]
