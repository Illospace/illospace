"""AgentRun handlers for meetbot meeting automation tools."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from brain.platform.db.repositories.unit_of_work import UnitOfWork

from brain.systems.meetings.client import (
    MeetbotClient,
    MeetbotConfigurationError,
    MeetbotServiceError,
)
from brain.systems.meetings.session_record import create_requested_meetbot_session
from brain.systems.runs.tool_catalog.handlers.common import (
    _agent_context,
    _short_exception_reason,
)
from brain.systems.runs.slack_delivery import slack_response_target


async def _handle_join_meeting(
    meeting_url: str,
    display_name: str | None = None,
) -> str:
    meeting_url = str(meeting_url or "").strip()
    if not meeting_url:
        return _error("join_meeting requires meeting_url")
    origin, requested_by = _current_slack_origin()
    try:
        client = MeetbotClient()
        session_id = str(uuid4())
        async with UnitOfWork() as uow:
            await create_requested_meetbot_session(
                uow.session,
                session_id=session_id,
                meeting_url=meeting_url,
                requesting_run_id=_current_requesting_run_id(),
            )
        joined = await client.join(
            session_id=session_id,
            meeting_url=meeting_url,
            display_name=display_name,
            origin=origin,
            requested_by=requested_by,
        )
        returned_session_id = str(joined.get("session_id") or "").strip()
        if returned_session_id != session_id:
            return _error("meetbot join response did not preserve session_id")
        current = await client.poll_join_status(
            session_id,
            initial=joined,
        )
    except (MeetbotConfigurationError, MeetbotServiceError, ValueError) as exc:
        return _meetbot_error(exc)
    except SQLAlchemyError:
        return _error("meetbot join request could not be recorded in the database")

    status = str(current.get("status") or joined.get("status") or "starting").strip()
    result = {
        "ok": status != "failed",
        "session_id": session_id,
        "status": status,
        "message": _status_message(status, current),
    }
    for key in ("warning", "error", "poll_warning", "caption_lines"):
        if current.get(key) not in (None, ""):
            result[key] = current[key]
    return json.dumps(result, default=str)


async def _handle_meeting_status(session_id: str) -> str:
    try:
        payload = await MeetbotClient().status(session_id)
    except (MeetbotConfigurationError, MeetbotServiceError, ValueError) as exc:
        return _meetbot_error(exc)
    status = str(payload.get("status") or "unknown").strip()
    return json.dumps(
        {
            "ok": status != "failed",
            **payload,
            "status": status,
            "message": _status_message(status, payload),
        },
        default=str,
    )


async def _handle_leave_meeting(session_id: str) -> str:
    try:
        payload = await MeetbotClient().leave(session_id)
    except (MeetbotConfigurationError, MeetbotServiceError, ValueError) as exc:
        return _meetbot_error(exc)
    return json.dumps(
        {
            "ok": True,
            "session_id": str(session_id).strip(),
            "status": payload.get("status") or "leave_requested",
            "message": "Meetbot was asked to leave and finalize the meeting transcript.",
            **payload,
        },
        default=str,
    )


async def _handle_send_meeting_chat(session_id: str, text: str) -> str:
    if not str(text or "").strip():
        return _error("send_meeting_chat requires text")
    try:
        payload = await MeetbotClient().chat(session_id, text=str(text))
    except (MeetbotConfigurationError, MeetbotServiceError, ValueError) as exc:
        return _meetbot_error(exc)
    return json.dumps(
        {
            "ok": True,
            "session_id": str(session_id).strip(),
            "status": payload.get("status") or "accepted",
            "message": "Meeting chat message accepted by meetbot.",
            **payload,
        },
        default=str,
    )


def _current_slack_origin() -> tuple[dict[str, str], str | None]:
    """Read the persisted Slack trigger route for the active AgentRun."""

    run = getattr(_agent_context, "run", None)
    if run is None:
        metadata = getattr(_agent_context, "execution_metadata", None)
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        trigger = getattr(_agent_context, "slack_trigger", None)
        if isinstance(trigger, Mapping) and not isinstance(
            metadata.get("slack_trigger"), Mapping
        ):
            metadata["slack_trigger"] = dict(trigger)
        target_ref = getattr(_agent_context, "target_ref", None)
        run = SimpleNamespace(
            metadata_=metadata,
            target_ref=dict(target_ref) if isinstance(target_ref, Mapping) else {},
        )
    response_target = slack_response_target(run)
    trigger = response_target["trigger"]
    channel = str(response_target["channel_id"] or "").strip()
    thread_ts = str(response_target["thread_ts"] or "").strip()
    origin = {"channel": channel} if channel else {}
    if channel and thread_ts:
        origin["thread_ts"] = thread_ts
    requested_by = str(trigger.get("slack_user_id") or "").strip() or None
    return origin, requested_by


def _current_requesting_run_id() -> int | None:
    """Resolve the persisted agent_runs.id for this tool call when available."""

    run = getattr(_agent_context, "run", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", None)
    metadata = dict(execution_metadata) if isinstance(execution_metadata, Mapping) else {}
    value = (
        getattr(run, "id", None)
        or getattr(run, "run_id", None)
        or getattr(_agent_context, "run_id", None)
        or metadata.get("run_id")
    )
    try:
        run_id = int(value)
    except (TypeError, ValueError):
        return None
    return run_id if run_id > 0 else None


def _status_message(status: str, payload: Mapping[str, Any]) -> str:
    normalized = str(status or "").strip().lower()
    messages = {
        "starting": "Meetbot is opening Google Meet; admission is not confirmed yet.",
        "lobby": "Meetbot is in the lobby and is waiting for host admission.",
        "admitted": "Meetbot is admitted, but no caption flow has been observed yet.",
        "captions_flowing": "Meetbot is admitted and live captions are flowing.",
        "ended": "The meeting session has ended and transcript finalization was requested.",
        "failed": "Meetbot could not continue the meeting session.",
    }
    message = messages.get(normalized, f"Meetbot reported session state: {normalized or 'unknown'}.")
    warning = str(payload.get("warning") or "").strip()
    error = str(payload.get("error") or "").strip()
    if normalized == "failed" and error:
        return f"{message} Error: {error}"
    if warning:
        return f"{message} Warning: {warning}"
    return message


def _meetbot_error(exc: Exception) -> str:
    payload: dict[str, Any] = {
        "ok": False,
        "error": _short_exception_reason(exc),
    }
    if isinstance(exc, MeetbotServiceError):
        if exc.status_code is not None:
            payload["status_code"] = exc.status_code
        for key in ("active_session_id", "detail", "warning"):
            if exc.payload.get(key) not in (None, ""):
                payload[key] = exc.payload[key]
    return json.dumps(payload, default=str)


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message})


__all__ = [
    "_current_slack_origin",
    "_current_requesting_run_id",
    "_handle_join_meeting",
    "_handle_leave_meeting",
    "_handle_meeting_status",
    "_handle_send_meeting_chat",
]
