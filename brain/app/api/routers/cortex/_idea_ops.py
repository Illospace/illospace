"""Cortex idea operations — threads, mentions, presence, unified stream."""
from __future__ import annotations

import base64
import binascii
import copy
import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any, NamedTuple

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, func, or_, select, union
from sqlalchemy.orm import aliased

from brain.contracts.statuses import OPEN_RUN_STATUS_VALUES
from brain.systems.runs.status import (
    TERMINAL_RUN_STATUSES,
    coerce_run_status,
)
from brain.systems.runs.cancel import async_cancel_open_runs_for_thread
from brain.systems.runs.store import AsyncAgentRunStore
from brain.systems.runs.ui_events import public_run_event_payload
from brain.app.api.auth import get_current_user
from brain.app.api.services.notifications import (
    async_build_notification_summary,
)
from brain.systems.runs.cortex.read_models import (
    public_failed_run_artifact,
    public_failure_for_run,
    run_stream_payload,
)
from brain.app.api.routers.cortex._helpers import (
    _infer_feedback_tags,
    _parse_message_type,
    _presence_cleanup,
    _presence_get,
    _presence_join,
    _presence_leave,
    _record_implicit_feedback,
    _a_require_idea_for_user as _require_idea_for_user,
)
from brain.app.api.routers.cortex._router import router
from brain.app.api.routers.ws import ws_manager
from brain.systems.cortex.thread_attachments import (
    build_thread_attachment_context,
    project_context_from_text_attachments,
)
from brain.systems.cortex.thought_lifecycle import (
    ThoughtStatusCommand,
    ThreadMessageCommand,
    post_thread_message,
    transition_thought_status,
)
from brain.systems.cortex.project_context.merge import merge_project_context_resources
from brain.systems.cortex.project_context.snapshot import (
    ProjectContextValidationError,
    validated_project_context_snapshot,
)
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow
from brain.platform.db.models.run import AgentRun
from brain.platform.db.models.idea import IdeaThread, VisualBlock
from brain.platform.db.models.org import User
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)

def _coerce_run_id(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _metadata_requests_live_guidance(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("live_guidance") or metadata.get("fast_steer") or metadata.get("target_run_id"))


def _metadata_target_run_id(metadata: dict[str, Any]) -> int:
    run_id = _coerce_run_id(metadata.get("target_run_id"))
    if run_id is None:
        raise HTTPException(status_code=400, detail="target_run_id is required for live guidance")
    return run_id


async def _append_live_guidance_from_thread_message(
    *,
    session,
    idea_id: str,
    role: str,
    content: str,
    metadata: Any,
    thread_msg: IdeaThread,
    user_id: str | None,
) -> int | None:
    if role != "user" or not _metadata_requests_live_guidance(metadata):
        return None
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="Live guidance metadata must be an object")

    run_id = _metadata_target_run_id(metadata)
    run = await session.get(AgentRun, run_id)
    if run is None or str(run.thread_id) != str(idea_id):
        raise HTTPException(status_code=404, detail=f"Run #{run_id} not found")
    if coerce_run_status(run.status) in TERMINAL_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="Run is no longer active")

    event = await AsyncAgentRunStore(session).append_steering(
        run_id,
        content,
        user_id=str(user_id) if user_id else None,
        thread_message_id=thread_msg.id,
    )
    thread_msg.metadata_ = {
        **dict(metadata),
        "live_guidance": True,
        "fast_steer": True,
        "target_run_id": run_id,
        "steering_event_id": event.id,
    }
    await session.flush()
    return int(event.id)


def _message_run_id(item: dict[str, Any]) -> int | None:
    if item.get("type") != "message":
        return None
    if str(item.get("role") or "").strip().lower() not in {"illo", "assistant"}:
        return None
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    if metadata.get("hidden") is True:
        return None
    if not str(item.get("content") or "").strip():
        return None
    return _coerce_run_id(metadata.get("run_id"))


def _final_answer_message_from_artifact(
    run_item: dict[str, Any],
    artifact: dict[str, Any],
) -> dict[str, Any] | None:
    if artifact.get("artifact_type") != "final_answer":
        return None
    text = str(artifact.get("text") or "").strip()
    if not text:
        return None
    run_id = _coerce_run_id(run_item.get("run_id") or run_item.get("id"))
    if run_id is None:
        return None
    artifact_id = artifact.get("id") or "latest"
    timestamp = (
        artifact.get("created_at")
        or run_item.get("completed_at")
        or run_item.get("updated_at")
        or run_item.get("started_at")
        or run_item.get("created_at")
        or run_item.get("timestamp")
        or ""
    )
    profile = run_item.get("profile") or run_item.get("requested_run_profile")
    return {
        "type": "message",
        "timestamp": timestamp,
        "id": f"run-final-{run_id}-{artifact_id}",
        "role": "illo",
        "content": text,
        "attachments": [],
        "metadata": {
            "run_id": run_id,
            "execution_profile": profile,
            "synthetic_from_run_artifact": True,
            "artifact_id": artifact_id,
        },
        "message_type": "agent_response",
        "user_name": None,
        "user_color": None,
    }


def _append_final_answer_messages(items: list[dict[str, Any]]) -> None:
    message_run_ids = {
        run_id
        for run_id in (_message_run_id(item) for item in items)
        if run_id is not None
    }
    additions: list[dict[str, Any]] = []
    for item in list(items):
        if item.get("type") != "run":
            continue
        run_id = _coerce_run_id(item.get("run_id") or item.get("id"))
        if run_id is None or run_id in message_run_ids:
            continue
        artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), list) else []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            message = _final_answer_message_from_artifact(item, artifact)
            if message is None:
                continue
            additions.append(message)
            message_run_ids.add(run_id)
            break
    items.extend(additions)


def _project_failed_run_message(
    item: dict[str, Any],
    failure: dict[str, str] | None,
) -> None:
    if failure is None or item.get("type") != "message":
        return
    item["content"] = failure["message"]
    metadata = dict(item.get("metadata") or {})
    for key in ("error", "final_answer", "output", "reason", "result", "text"):
        metadata.pop(key, None)
    metadata["failure"] = dict(failure)
    item["metadata"] = metadata


def _project_run_artifacts(
    artifacts: list[dict[str, Any]],
    failure: dict[str, str] | None,
) -> list[dict[str, Any]]:
    return [public_failed_run_artifact(artifact, failure) for artifact in artifacts]


_RUN_WORK_EVENT_TYPES = {
    "run.started",
    "run.activity",
    "run.step_started",
    "run.tool_started",
    "run.tool_completed",
    "run.tool_failed",
    "run.completed",
    "run.failed",
    "run.canceled",
}
_RUN_WORK_EVENT_LIMIT_PER_RUN = 120


def _run_work_events_stmt(run_ids: list[int]):
    ranked_events = (
        select(
            AgentRunEventRow,
            func.row_number()
            .over(
                partition_by=AgentRunEventRow.run_id,
                order_by=(AgentRunEventRow.sequence_no.desc(), AgentRunEventRow.id.desc()),
            )
            .label("event_rank"),
        )
        .where(AgentRunEventRow.run_id.in_(run_ids))
        .where(AgentRunEventRow.event_type.in_(sorted(_RUN_WORK_EVENT_TYPES)))
        .subquery()
    )
    event = aliased(AgentRunEventRow, ranked_events)
    return (
        select(event)
        .where(ranked_events.c.event_rank <= _RUN_WORK_EVENT_LIMIT_PER_RUN)
        .order_by(event.run_id.asc(), event.sequence_no.asc(), event.id.asc())
    )


def _compact_json(value: Any, *, limit: int = 180) -> str | None:
    if value in (None, "", {}, []):
        return None
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _event_created_at(event: Any) -> str | None:
    value = getattr(event, "created_at", None)
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _duration_from_item(item: dict[str, Any]) -> int | None:
    if item.get("duration_sec") is not None:
        try:
            return int(item["duration_sec"])
        except (TypeError, ValueError):
            pass
    start = item.get("started_at") or item.get("created_at") or item.get("timestamp")
    end = item.get("completed_at") or item.get("failed_at") or item.get("canceled_at") or item.get("updated_at")
    if not start or not end:
        return None
    try:
        return max(0, int((datetime.fromisoformat(str(end)) - datetime.fromisoformat(str(start))).total_seconds()))
    except Exception:
        return None


def _tool_display_label(payload: dict[str, Any]) -> str | None:
    display = payload.get("tool_display")
    if not isinstance(display, dict):
        return None
    label = str(display.get("label") or "").strip()
    return label or None


def _activity_from_event(event_type: str, payload: dict[str, Any]) -> str | None:
    if event_type == "run.started":
        return "Started"
    if event_type in {"run.activity", "run.step_started"}:
        label = payload.get("label") or payload.get("activity") or payload.get("step") or payload.get("step_key")
        return str(label).strip() if label else None
    if event_type == "run.tool_started":
        label = _tool_display_label(payload)
        if label:
            return label
        tool_name = str(payload.get("tool_name") or "tool").strip()
        return f"Using {tool_name}" if tool_name else "Using a tool"
    if event_type in {"run.tool_completed", "run.tool_failed"}:
        label = _tool_display_label(payload)
        if label:
            return label
        tool_name = str(payload.get("tool_name") or "tool").strip()
        status = "failed" if event_type == "run.tool_failed" else "completed"
        return f"{tool_name} {status}" if tool_name else f"Tool {status}"
    if event_type == "run.completed":
        return "Completed"
    if event_type == "run.failed":
        return "Failed"
    if event_type == "run.canceled":
        return "Canceled"
    return None


def _apply_run_events_to_item(
    item: dict[str, Any],
    events: list[Any],
    failure: dict[str, str] | None = None,
) -> None:
    activity_trace: list[dict[str, Any]] = []
    work_log: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []

    for event in events:
        event_type = str(getattr(event, "event_type", "") or "")
        if event_type not in _RUN_WORK_EVENT_TYPES:
            continue
        payload = public_run_event_payload(
            getattr(event, "payload", None),
            event_type,
            failure=failure,
        )
        at = _event_created_at(event)
        label = _activity_from_event(event_type, payload)
        if label:
            entry = {
                "at": at,
                "activity": label,
                "kind": event_type,
                "sequence_no": getattr(event, "sequence_no", None),
            }
            if payload.get("tool_name"):
                entry["tool_name"] = payload.get("tool_name")
            if payload.get("error"):
                entry["error"] = payload.get("error")
            activity_trace.append(entry)
            work_log.append({"time": at, "text": label, "kind": event_type})

        if event_type == "run.tool_started":
            tool_calls.append({
                "tool": str(payload.get("tool_name") or "tool"),
                "args": _compact_json(payload.get("args")),
                "at": at,
                "status": "running",
                "display": payload.get("tool_display"),
            })
        elif event_type in {"run.tool_completed", "run.tool_failed"}:
            tool = str(payload.get("tool_name") or "tool")
            status = "failed" if event_type == "run.tool_failed" else "completed"
            match = next((call for call in reversed(tool_calls) if call.get("tool") == tool and call.get("status") == "running"), None)
            if match is None:
                match = {"tool": tool, "args": _compact_json(payload.get("args")), "at": at}
                tool_calls.append(match)
            match["status"] = status
            match["finished_at"] = at
            display = payload.get("tool_display")
            if display and (not match.get("display") or display.get("target")):
                match["display"] = payload.get("tool_display")
            if isinstance(match.get("display"), dict):
                match["display"]["status"] = status
            if payload.get("error"):
                match["error"] = str(payload.get("error"))[:500]
            elif payload.get("result"):
                match["result"] = str(payload.get("result"))[:500]

    if activity_trace:
        item["activity_trace"] = activity_trace
    if work_log:
        item["work_log"] = work_log
    if tool_calls:
        item["tool_calls"] = tool_calls

    duration_sec = _duration_from_item(item)
    item["work_summary"] = {
        "duration_sec": duration_sec,
        "activity_count": len(activity_trace),
        "tool_count": len(tool_calls),
        "tool_names": sorted({str(call.get("tool")) for call in tool_calls if call.get("tool")}),
        "status": item.get("status"),
    }


def _extract_project_context_from_message(
    attachments: list[dict[str, Any]] | None,
    metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return UI-attached Project Context without trusting arbitrary other metadata."""
    explicit: dict[str, Any] | None = None
    if isinstance(metadata, dict):
        candidate = metadata.get("project_context") or metadata.get("project_context_snapshot")
        if isinstance(candidate, dict):
            explicit = copy.deepcopy(candidate)
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        candidate = attachment.get("project_context")
        if attachment.get("type") == "project_context" and isinstance(candidate, dict):
            explicit = merge_project_context_resources(explicit, candidate)
    readable_attachments = project_context_from_text_attachments(attachments)
    return merge_project_context_resources(explicit, readable_attachments)


def _validate_thread_project_context(project_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not project_context:
        return project_context
    try:
        validated_project_context_snapshot(project_context, validate_local_paths=False)
    except ProjectContextValidationError as exc:
        raise HTTPException(status_code=422, detail={"validation_errors": exc.errors}) from exc
    return project_context


def _merge_project_context_into_idea(idea: Any, project_context: dict[str, Any] | None) -> None:
    if not project_context:
        return
    existing = dict(idea.agent_details or {}) if isinstance(idea.agent_details, dict) else {}
    existing["project_context"] = project_context
    idea.agent_details = existing


async def _publish_notification_summary_updates(
    *,
    org_id: str | None,
    user_ids: set[str],
) -> None:
    if not org_id or not user_ids:
        return

    async with UnitOfWork() as uow:
        summaries = {
            user_id: await async_build_notification_summary(uow.session, user_id=user_id, org_id=org_id)
            for user_id in sorted(user_ids)
        }
    for user_id, summary in summaries.items():
        await ws_manager.publish_notification_summary_updated(
            user_id=user_id,
            summary=summary.model_dump(mode="json"),
        )


# ── Idea operations ────────────────────────────────────────────

@router.post("/ideas/{idea_id}/cancel-all")
async def idea_cancel_all(idea_id: str, user: dict[str, Any] = Depends(get_current_user)):
    async with UnitOfWork() as uow:
        await _require_idea_for_user(uow.session, idea_id, user)
        count = await async_cancel_open_runs_for_thread(uow.session, idea_id)
    return {"ok": True, "canceled": count, "cancelled": count}


@router.post("/ideas/{idea_id}/mark-read")
async def mark_read(idea_id: str, request: Request, user: dict[str, Any] = Depends(get_current_user)):
    user_id = str(user.get("id"))
    org_id = str(user.get("org_id")) if user.get("org_id") else None
    async with UnitOfWork() as uow:
        idea = await _require_idea_for_user(uow.session, idea_id, user)
        if idea.status == "unread_reply":
            idea.read_at = datetime.now(timezone.utc)
            await transition_thought_status(
                uow.session,
                idea=idea,
                command=ThoughtStatusCommand(
                    to_status="needs_input",
                    trigger="user_read",
                    actor=user,
                ),
            )
        await uow.notifications.mark_read_for_idea(user_id=user_id, idea_id=idea_id)
    try:
        await _publish_notification_summary_updates(org_id=org_id, user_ids={user_id})
    except Exception as exc:
        logger.warning("workspace_notification_summary_publish_failed: %s", exc)
    return {"ok": True}


@router.patch("/ideas/{idea_id}/position")
async def update_position(idea_id: str, request: Request, user: dict[str, Any] = Depends(get_current_user)):
    data = await request.json()
    async with UnitOfWork() as uow:
        idea = await _require_idea_for_user(uow.session, idea_id, user)
        if idea and idea.archived_at is None:
            idea.position_x = data.get("x")
            idea.position_y = data.get("y")
    return {"ok": True}


@router.post("/ideas/{idea_id}/thread")
async def add_thread_message_raw(idea_id: str, request: Request, user: dict[str, Any] = Depends(get_current_user)):
    """Add a Cortex thread message."""
    data = await request.json()
    role = data.get("role", "user")
    content = data.get("content", "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content is required")
    if role not in ("user", "assistant", "illo"):
        raise HTTPException(status_code=400, detail="Role must be 'user', 'assistant', or 'illo'")
    from brain.systems.cortex.events import publish

    attachments_data = data.get("attachments", [])
    if not isinstance(attachments_data, list):
        attachments_data = []
    metadata = data.get("metadata")
    notification_org_id: str | None = str(user.get("org_id")) if user.get("org_id") else None
    async with UnitOfWork() as uow:
        idea = await _require_idea_for_user(uow.session, idea_id, user)

        async def resolve_mentioned_users(names: list[str], org_id: str) -> dict[str, str]:
            stmt = (
                select(User.id, func.lower(User.name).label("name"))
                .where(
                    User.org_id == org_id,
                    func.lower(User.name).in_(names),
                )
            )
            result = await uow.session.execute(stmt)
            return {row.name: str(row.id) for row in result.all()}

        async def append_live_guidance(**kwargs):
            await _append_live_guidance_from_thread_message(
                session=uow.session,
                **kwargs,
            )

        result = await post_thread_message(
            uow.session,
            idea=idea,
            command=ThreadMessageCommand(
                idea_id=idea_id,
                role=role,
                content=content,
                actor={
                    "user_id": user.get("id"),
                    "org_id": user.get("org_id"),
                    "name": user.get("name"),
                    "color": user.get("color"),
                },
                attachments=attachments_data,
                metadata=metadata if isinstance(metadata, dict) else None,
            ),
            mention_repo=uow.user_mentions,
            notification_repo=uow.notifications,
            resolve_mentioned_users=resolve_mentioned_users,
            publish=publish,
            validate_project_context=_validate_thread_project_context,
            extract_project_context=_extract_project_context_from_message,
            build_attachment_context=build_thread_attachment_context,
            parse_message_type=_parse_message_type,
            append_live_guidance=append_live_guidance,
        )
        msg = result.message_payload
        status_change = result.status_change
        notification_org_id = result.notification_org_id
        notification_user_ids = result.notification_user_ids

    if role == "user":
        feedback_tags = _infer_feedback_tags(content)
        if feedback_tags:
            async with UnitOfWork() as uow:
                await _record_implicit_feedback(uow.session, idea_id, content, feedback_tags)

    await ws_manager.broadcast_product_event(
        "thread_message",
        {"idea_id": idea_id, "message": msg},
        org_id=notification_org_id,
    )
    if status_change:
        await ws_manager.broadcast_product_event(
            "status_change",
            {"idea_id": idea_id, "new_status": status_change["new_status"]},
            org_id=notification_org_id,
        )
    try:
        await _publish_notification_summary_updates(
            org_id=notification_org_id,
            user_ids=notification_user_ids,
        )
    except Exception as exc:
        logger.warning("workspace_notification_summary_publish_failed: %s", exc)
    return JSONResponse(content=msg, status_code=201)


@router.post("/ideas/{idea_id}/mentions/seen")
async def mark_mentions_seen(idea_id: str, user: dict[str, Any] = Depends(get_current_user)):
    user_id = user.get("id")
    async with UnitOfWork() as uow:
        await _require_idea_for_user(uow.session, idea_id, user)
        count = await uow.user_mentions.mark_seen_for_idea(
            user_id=str(user_id),
            idea_id=idea_id,
        )
        return {"cleared": count}


@router.get("/mentions/unread")
async def get_unread_mentions(user: dict[str, Any] = Depends(get_current_user)):
    user_id = user.get("id")
    async with UnitOfWork() as uow:
        return await uow.user_mentions.list_unread_for_user(
            user_id=str(user_id),
        )


# ── Presence ───────────────────────────────────────────────────

@router.post("/presence")
async def update_presence(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    data = await request.json()
    idea_id = data.get("idea_id")
    action = data.get("action")
    user_id = str(user.get("id"))
    user_name = user.get("name", "")
    user_color = user.get("color", "#6366f1")

    if not idea_id or action not in ("join", "leave"):
        raise HTTPException(status_code=400, detail="idea_id and action (join/leave) required")

    async with UnitOfWork() as uow:
        await _require_idea_for_user(uow.session, idea_id, user)

    _presence_cleanup()
    if action == "join":
        _presence_join(idea_id, user_id, user_name, user_color)
    else:
        _presence_leave(idea_id, user_id)

    viewers = _presence_get(idea_id)
    from brain.systems.cortex.events import publish
    publish("presence", {"idea_id": idea_id, "viewers": viewers, "status": "online"})
    return {"viewers": viewers}


# ── Retired legacy agent status ────────────────────────────────

@router.post("/ideas/{idea_id}/agent-status")
async def update_agent_status(idea_id: str, request: Request, user: dict[str, Any] = Depends(get_current_user)):
    _ = (idea_id, request, user)
    raise HTTPException(
        status_code=410,
        detail="Legacy agent-status updates are retired; use AgentRun events and projections.",
    )


# ── Unified stream ─────────────────────────────────────────────


_STREAM_PAGE_LIMIT = 200
# Head-only active roots are bounded independently of the physical page size.
_STREAM_ACTIVE_ROOT_LIMIT = 20
_STREAM_KIND_RANK = {"message": 0, "run": 1, "visual_block": 2}


class _StreamKey(NamedTuple):
    created_at: datetime
    kind_rank: int
    row_id: int


class _StreamCandidate(NamedTuple):
    key: _StreamKey
    kind: str
    row: Any


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("Stream timestamp must be a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stream_candidate(kind: str, row: Any) -> _StreamCandidate:
    physical_row = row[0] if kind == "message" else row
    return _StreamCandidate(
        key=_StreamKey(
            _utc_datetime(physical_row.created_at),
            _STREAM_KIND_RANK[kind],
            int(physical_row.id),
        ),
        kind=kind,
        row=row,
    )


def _encode_stream_cursor(key: _StreamKey) -> str:
    payload = "|".join((
        "v1",
        key.created_at.isoformat(timespec="microseconds"),
        str(key.kind_rank),
        str(key.row_id),
    )).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_stream_cursor(value: str | None) -> _StreamKey | None:
    if value is None:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode((value + padding).encode("ascii"), altchars=b"-_", validate=True)
        version, timestamp, raw_kind_rank, raw_row_id = raw.decode("ascii").split("|")
        if version != "v1":
            raise ValueError
        kind_rank = int(raw_kind_rank)
        row_id = int(raw_row_id)
        if (
            (raw_kind_rank, raw_row_id) != (str(kind_rank), str(row_id))
            or kind_rank not in _STREAM_KIND_RANK.values() or row_id <= 0
        ):
            raise ValueError
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed_timestamp.tzinfo is None:
            raise ValueError
        return _StreamKey(_utc_datetime(parsed_timestamp), kind_rank, row_id)
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise HTTPException(status_code=400, detail="Invalid unified stream cursor") from None


def _stream_before_clause(model: Any, kind: str, cursor: _StreamKey | None):
    if cursor is None:
        return None
    rank = _STREAM_KIND_RANK[kind]
    if rank < cursor.kind_rank:
        return model.created_at <= cursor.created_at
    if rank > cursor.kind_rank:
        return model.created_at < cursor.created_at
    return or_(
        model.created_at < cursor.created_at,
        and_(model.created_at == cursor.created_at, model.id < cursor.row_id),
    )


def _stream_messages_stmt(
    idea_id: str,
    cursor: _StreamKey | None,
    candidate_limit: int,
):
    stmt = (
        select(IdeaThread, User.name.label("user_name"), User.color.label("user_color"))
        .outerjoin(User, IdeaThread.user_id == User.id)
        .where(IdeaThread.idea_id == idea_id)
    )
    before_clause = _stream_before_clause(IdeaThread, "message", cursor)
    if before_clause is not None:
        stmt = stmt.where(before_clause)
    return stmt.order_by(IdeaThread.created_at.desc(), IdeaThread.id.desc()).limit(candidate_limit)


def _visible_run_clause():
    return AgentRun.metadata_["headless"].as_boolean().is_not(True)


def _stream_runs_stmt(
    idea_id: str,
    cursor: _StreamKey | None,
    candidate_limit: int,
):
    recent = select(AgentRun.id).where(
        AgentRun.thread_id == idea_id,
        _visible_run_clause(),
    )
    before_clause = _stream_before_clause(AgentRun, "run", cursor)
    if before_clause is not None:
        recent = recent.where(before_clause)
    recent = recent.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(candidate_limit)
    recent_ids = recent.cte("recent_stream_runs")
    selected_ids = select(recent_ids.c.id)
    if cursor is None:
        active_roots = (
            select(AgentRun.id)
            .where(
                AgentRun.thread_id == idea_id,
                AgentRun.parent_run_id.is_(None),
                AgentRun.status.in_(OPEN_RUN_STATUS_VALUES),
                _visible_run_clause(),
            )
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(_STREAM_ACTIVE_ROOT_LIMIT)
            .cte("active_stream_roots")
        )
        selected_ids = union(selected_ids, select(active_roots.c.id))
    return (
        select(AgentRun)
        .where(AgentRun.id.in_(selected_ids))
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
    )


def _stream_visuals_stmt(
    idea_id: str,
    cursor: _StreamKey | None,
    candidate_limit: int,
):
    stmt = select(VisualBlock).where(VisualBlock.idea_id == idea_id)
    before_clause = _stream_before_clause(VisualBlock, "visual_block", cursor)
    if before_clause is not None:
        stmt = stmt.where(before_clause)
    return stmt.order_by(VisualBlock.created_at.desc(), VisualBlock.id.desc()).limit(candidate_limit)


def _select_stream_page(
    candidates: list[_StreamCandidate],
    limit: int,
) -> tuple[list[_StreamCandidate], bool, str | None]:
    newest = sorted(candidates, key=lambda candidate: candidate.key, reverse=True)[: limit + 1]
    has_more = len(newest) > limit
    selected = sorted(newest[:limit], key=lambda candidate: candidate.key)
    next_before = _encode_stream_cursor(selected[0].key) if has_more else None
    return selected, has_more, next_before


def _active_root_injections(
    run_rows: list[Any],
    selected: list[_StreamCandidate],
) -> list[_StreamCandidate]:
    if not selected:
        return []
    oldest_key = selected[0].key
    selected_run_ids = {
        candidate.key.row_id for candidate in selected if candidate.kind == "run"
    }
    injections: list[_StreamCandidate] = []
    for run in run_rows:
        if (
            int(run.id) in selected_run_ids
            or run.parent_run_id is not None
            or run.status not in OPEN_RUN_STATUS_VALUES
        ):
            continue
        candidate = _stream_candidate("run", run)
        if candidate.key < oldest_key:
            injections.append(candidate)
    return sorted(injections, key=lambda candidate: candidate.key)


def _serialize_stream_candidate(candidate: _StreamCandidate) -> dict[str, Any]:
    if candidate.kind == "run":
        return run_stream_payload(candidate.row)
    if candidate.kind == "visual_block":
        visual = candidate.row
        return {
            "type": "visual_block",
            "timestamp": visual.created_at.isoformat() if visual.created_at else "",
            "id": f"vb-{visual.id}",
            "content_type": visual.content_type,
            "title": visual.title,
            "content": visual.content,
            "display_mode": visual.display_mode or "inline",
            "run_id": visual.run_id,
            "position_after": visual.position_after,
        }
    row = candidate.row
    message = row[0]
    return {
        "type": "message",
        "timestamp": message.created_at.isoformat() if message.created_at else "",
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "attachments": message.attachments or [],
        "metadata": message.metadata_ or {},
        "message_type": message.message_type,
        "user_name": row.user_name,
        "user_color": row.user_color,
    }


async def unified_stream_payload(
    idea_id: str,
    *,
    limit: int = _STREAM_PAGE_LIMIT,
    before: str | None = None,
    user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    cursor = _decode_stream_cursor(before)
    candidate_limit = limit + 1

    async with UnitOfWork() as uow:
        await _require_idea_for_user(uow.session, idea_id, user)
        message_rows = list(
            (await uow.session.execute(_stream_messages_stmt(idea_id, cursor, candidate_limit))).all()
        )
        all_run_rows = list((await uow.session.scalars(
            _stream_runs_stmt(idea_id, cursor, candidate_limit)
        )).all())
        recent_run_rows = sorted(
            all_run_rows,
            key=lambda run: _stream_candidate("run", run).key,
            reverse=True,
        )[:candidate_limit]
        visual_rows = list((await uow.session.scalars(
            _stream_visuals_stmt(idea_id, cursor, candidate_limit)
        )).all())

        candidates = [
            *(_stream_candidate("message", row) for row in message_rows),
            *(_stream_candidate("run", row) for row in recent_run_rows),
            *(_stream_candidate("visual_block", row) for row in visual_rows),
        ]
        selected, has_more, next_before = _select_stream_page(candidates, limit)
        if cursor is None:
            selected.extend(_active_root_injections(all_run_rows, selected))
        selected.sort(key=lambda candidate: candidate.key)
        items = [_serialize_stream_candidate(candidate) for candidate in selected]

        run_ids = [int(item["id"]) for item in items if item.get("type") == "run"]
        for item in items:
            if item["type"] == "run":
                item["tool_calls"] = []

        run_rows_by_id = {int(run.id): run for run in all_run_rows}
        referenced_run_ids = {
            run_id
            for run_id in (_message_run_id(item) for item in items)
            if run_id is not None
        }
        missing_referenced_run_ids = referenced_run_ids.difference(run_rows_by_id)
        if missing_referenced_run_ids:
            referenced_runs = (
                await uow.session.scalars(
                    select(AgentRun).where(
                        AgentRun.id.in_(sorted(missing_referenced_run_ids)),
                        AgentRun.thread_id == idea_id,
                        _visible_run_clause(),
                    )
                )
            ).all()
            run_rows_by_id.update({int(run.id): run for run in referenced_runs})
        child_runs: dict[int, list[dict[str, Any]]] = {}
        run_artifacts: dict[int, list[dict[str, Any]]] = {}
        run_events: dict[int, list[Any]] = {}
        if run_ids:
            children_stmt = (
                select(AgentRun)
                .where(AgentRun.parent_run_id.in_(run_ids), _visible_run_clause())
                .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
            )
            for child in (await uow.session.scalars(children_stmt)).all():
                if child.parent_run_id is None:
                    continue
                child_runs.setdefault(int(child.parent_run_id), []).append(run_stream_payload(child))

            artifact_stmt = (
                select(AgentRunArtifactRow)
                .where(AgentRunArtifactRow.run_id.in_(run_ids))
                .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
            )
            for artifact in (await uow.session.scalars(artifact_stmt)).all():
                run_artifacts.setdefault(int(artifact.run_id), []).append({
                    "id": artifact.id,
                    "artifact_type": artifact.artifact_type,
                    "title": artifact.title,
                    "payload": artifact.payload or {},
                    "text": artifact.text,
                    "uri": artifact.uri,
                    "visibility": artifact.visibility,
                    "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
                })

        event_run_ids = sorted(set(run_ids).union(referenced_run_ids))
        if event_run_ids:
            event_stmt = _run_work_events_stmt(event_run_ids)
            for event in (await uow.session.scalars(event_stmt)).all():
                run_events.setdefault(int(event.run_id), []).append(event)

        for item in items:
            if item["type"] == "run":
                run_id = int(item.get("id", 0))
                run_row = run_rows_by_id.get(run_id)
                children = child_runs.get(run_id, [])
                events = run_events.get(run_id, [])
                failure = public_failure_for_run(run_row, events) if run_row is not None else None
                if failure is not None:
                    item["failure"] = failure
                artifacts = _project_run_artifacts(run_artifacts.get(run_id, []), failure)
                if children:
                    item["child_runs"] = children
                if artifacts:
                    item["artifacts"] = artifacts
                _apply_run_events_to_item(item, events, failure)

        run_failures = {
            run_id: public_failure_for_run(run, run_events.get(run_id, []))
            for run_id, run in run_rows_by_id.items()
        }
        for item in items:
            run_id = _message_run_id(item)
            if run_id is not None:
                _project_failed_run_message(item, run_failures.get(run_id))

        _append_final_answer_messages(items)

    items.sort(key=lambda item: _utc_datetime(item["timestamp"]))
    return {
        "idea_id": idea_id,
        "items": items,
        "has_more": has_more,
        "next_before": next_before,
    }


@router.get("/ideas/{idea_id}/unified-stream")
async def idea_unified_stream(
    idea_id: str,
    limit: Annotated[int, Query(ge=1, le=_STREAM_PAGE_LIMIT)] = _STREAM_PAGE_LIMIT,
    before: str | None = None,
    user: dict[str, Any] = Depends(get_current_user),
):
    return await unified_stream_payload(
        idea_id=idea_id,
        limit=limit,
        before=before,
        user=user,
    )
