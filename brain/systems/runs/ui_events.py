"""Stable websocket projection for AgentRun events."""

from __future__ import annotations

from typing import Any

from brain.systems.runs.failures import public_run_failure
from brain.systems.runs.presentation import public_tool_event_payload
from brain.systems.runs.visibility import run_is_headless

STABLE_RUN_EVENT_TYPES = frozenset(
    {
        "run_started",
        "step_started",
        "tool_started",
        "tool_finished",
        "text_delta",
        "run_completed",
    }
)

_INTERNAL_TO_UI_TYPE = {
    "run.started": "run_started",
    "run.activity": "step_started",
    "run.step_started": "step_started",
    "run.tool_started": "tool_started",
    "run.tool_completed": "tool_finished",
    "run.tool_failed": "tool_finished",
    "run.text_delta": "text_delta",
    "run.text_completed": "text_delta",
    "run.completed": "run_completed",
    "run.failed": "run_completed",
    "run.canceled": "run_completed",
    "run.expired": "run_completed",
    "run.status_changed": "run_completed",
}

_PUBLIC_FAILURE_STATUSES = frozenset({"failed", "canceled", "cancelled", "expired"})


def _public_failure_for_event(
    event: Any,
    *,
    run: Any | None,
    source_type: str,
    payload: dict[str, Any],
) -> dict[str, str] | None:
    event_status = {
        "run.failed": "failed",
        "run.tool_failed": "failed",
        "run.canceled": "canceled",
        "run.expired": "expired",
    }.get(source_type)
    status = str(
        (payload.get("to_status") if source_type == "run.status_changed" else None)
        or event_status
        or getattr(run, "status", None)
        or getattr(event, "_agent_run_status", None)
        or ""
    ).strip().lower()
    if status == "cancelled":
        status = "canceled"
    if status not in _PUBLIC_FAILURE_STATUSES:
        return None
    metadata = getattr(run, "metadata_", None) or getattr(event, "_agent_run_metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    metadata_failure = metadata.get("failure") if isinstance(metadata.get("failure"), dict) else {}
    payload_failure = payload.get("failure") if isinstance(payload.get("failure"), dict) else {}
    category = (
        metadata_failure.get("category")
        or payload_failure.get("category")
        or payload.get("failure_category")
        or payload.get("category")
    )
    return public_run_failure(status, category)


def public_run_event_payload(
    payload: Any,
    source_type: str,
    *,
    failure: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Project one durable event payload into its safe public representation."""

    projected = dict(payload or {})
    if source_type in {"run.tool_started", "run.tool_completed", "run.tool_failed"}:
        projected = public_tool_event_payload(projected, source_type)
    if source_type in {"run.failed", "run.canceled", "run.expired"} and failure is None:
        failure = public_run_failure(
            {
                "run.failed": "failed",
                "run.canceled": "canceled",
                "run.expired": "expired",
            }[source_type],
            projected.get("failure_category") or projected.get("category")
        )
    if failure is not None and source_type == "run.tool_failed":
        projected.pop("result", None)
        projected.pop("result_preview", None)
        projected["failure"] = failure
        projected["error"] = failure["message"]
    elif failure is not None and source_type in {"run.text_delta", "run.text_completed"}:
        for key in ("delta", "error", "final_answer", "message", "text"):
            projected.pop(key, None)
        projected["failure"] = failure
        projected["text"] = failure["message"]
    elif failure is not None and source_type in {"run.failed", "run.canceled", "run.expired"}:
        for key in ("error", "final_answer", "message", "reason", "text"):
            projected.pop(key, None)
        projected["failure"] = failure
        projected["failure_category"] = failure["category"]
        projected["error"] = failure["message"]
    elif failure is not None and source_type == "run.status_changed":
        projected = {
            key: value
            for key, value in projected.items()
            if key in {"from_status", "to_status"}
        }
        projected["failure"] = failure
        projected["failure_category"] = failure["category"]
        projected["error"] = failure["message"]
    elif failure is not None:
        for key in ("details", "diagnostic", "error", "errors", "exception", "message", "reason", "result", "text", "traceback"):
            projected.pop(key, None)
        projected["failure"] = failure
    return projected


def run_event_to_ui_message(
    event: Any,
    *,
    run: Any | None = None,
    org_id: str | None = None,
    replayed: bool = False,
) -> dict[str, Any] | None:
    """Project a durable AgentRun event into the public Cortex stream vocabulary."""

    if run_is_headless(run):
        return None

    source_type = str(getattr(event, "event_type", None) or "")
    ui_type = _INTERNAL_TO_UI_TYPE.get(source_type)
    if ui_type is None:
        return None

    raw_payload = dict(getattr(event, "payload", None) or {})
    failure = _public_failure_for_event(
        event,
        run=run,
        source_type=source_type,
        payload=raw_payload,
    )
    if failure is not None and source_type == "run.text_delta":
        # Historical partial deltas may contain the diagnostic that ultimately
        # failed the run. The terminal event carries the single safe message.
        return None
    if source_type == "run.status_changed" and (
        failure is None or failure.get("status") != "expired"
    ):
        return None
    payload = public_run_event_payload(raw_payload, source_type, failure=failure)
    event_id = int(getattr(event, "id", 0) or payload.get("event_id") or 0)
    run_id = int(getattr(event, "run_id", 0) or payload.get("run_id") or 0)
    root_run_id = int(getattr(event, "root_run_id", None) or payload.get("root_run_id") or run_id)
    sequence_no = int(getattr(event, "sequence_no", 0) or payload.get("sequence_no") or 0)
    thread_id = _text(
        getattr(run, "thread_id", None)
        or getattr(event, "_agent_run_thread_id", None)
        or payload.get("thread_id")
        or payload.get("idea_id")
    )
    profile = _text(getattr(run, "profile", None) or getattr(event, "_agent_run_profile", None) or payload.get("profile"))

    message = {
        **payload,
        "type": ui_type,
        "source_event_type": source_type,
        "event_channel": "run",
        "event_cursor": event_id,
        "run_event_id": event_id,
        "event_id": event_id,
        "run_id": run_id,
        "root_run_id": root_run_id,
        "sequence_no": sequence_no,
    }
    resolved_org_id = org_id or _text(getattr(run, "org_id", None) or getattr(event, "_agent_run_org_id", None))
    if resolved_org_id:
        message.setdefault("org_id", resolved_org_id)
    if thread_id:
        message.setdefault("thread_id", thread_id)
        message.setdefault("idea_id", thread_id)
    if profile:
        message.setdefault("profile", profile)
        message.setdefault("execution_profile", profile)

    _normalize_ui_payload(ui_type, source_type, message)
    if replayed:
        message["replayed"] = True
    created_at = getattr(event, "created_at", None)
    if created_at is not None:
        message["event_created_at"] = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
    return message


def _normalize_ui_payload(ui_type: str, source_type: str, message: dict[str, Any]) -> None:
    if ui_type == "text_delta":
        if source_type == "run.text_completed" and not _text(message.get("delta")):
            text = _text(message.get("text"))
            if text:
                message["delta"] = text
    elif ui_type == "step_started":
        label = _text(message.get("label") or message.get("activity") or message.get("step") or message.get("step_key"))
        if label:
            message["label"] = label
            message.setdefault("activity", label)
    elif ui_type == "tool_started":
        message["status"] = "running"
    elif ui_type == "tool_finished":
        message["status"] = "failed" if source_type == "run.tool_failed" else "completed"
    elif ui_type == "run_completed":
        if source_type == "run.failed":
            message["status"] = "failed"
        elif source_type == "run.canceled":
            message["status"] = "canceled"
        elif source_type == "run.expired":
            message["status"] = "expired"
        elif source_type == "run.status_changed" and isinstance(message.get("failure"), dict):
            message["status"] = message["failure"]["status"]
        else:
            message.setdefault("status", "completed")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "STABLE_RUN_EVENT_TYPES",
    "public_run_event_payload",
    "run_event_to_ui_message",
]
