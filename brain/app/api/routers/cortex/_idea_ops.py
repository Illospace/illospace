"""Cortex idea operations — threads, mentions, presence, unified stream."""
from __future__ import annotations

import json
import logging
import copy
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from brain.systems.runs.status import TERMINAL_RUN_STATUSES, coerce_run_status
from brain.systems.runs.store import AgentRunStore
from brain.app.api.auth import get_current_user
from brain.app.api.services.notifications import (
    build_notification_summary,
    compact_notification_text,
)
from brain.systems.runs.cortex.read_models import run_stream_payload
from brain.app.api.routers.cortex._helpers import (
    _extract_mentions,
    _infer_feedback_tags,
    _parse_message_type,
    _presence_cleanup,
    _presence_get,
    _presence_join,
    _presence_leave,
    _record_implicit_feedback,
    _require_idea_for_user,
)
from brain.app.api.routers.cortex._router import router
from brain.app.api.routers.ws import ws_manager
from brain.systems.cortex.thread_attachments import (
    build_thread_attachment_context,
    project_context_from_text_attachments,
)
from brain.systems.cortex.project_context.snapshot import (
    ProjectContextValidationError,
    validated_project_context_snapshot,
)
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow
from brain.platform.db.models.run import AgentRun
from brain.platform.db.models.idea import IdeaStateLog, IdeaThread
from brain.platform.db.models.notification import (
    NOTIFICATION_KIND_WORKSPACE_MENTION,
    NOTIFICATION_KIND_WORKSPACE_THREAD_ATTENTION,
    NOTIFICATION_SOURCE_WORKSPACE,
)
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


def _append_live_guidance_from_thread_message(
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
    run = session.get(AgentRun, run_id)
    if run is None or str(run.thread_id) != str(idea_id):
        raise HTTPException(status_code=404, detail=f"Run #{run_id} not found")
    if coerce_run_status(run.status) in TERMINAL_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="Run is no longer active")

    event = AgentRunStore(session).append_steering(
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
    session.flush()
    return int(event.id)


def _message_run_id(item: dict[str, Any]) -> int | None:
    if item.get("type") != "message":
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


def _activity_from_event(event_type: str, payload: dict[str, Any]) -> str | None:
    if event_type == "run.started":
        return "Started"
    if event_type in {"run.activity", "run.step_started"}:
        label = payload.get("label") or payload.get("activity") or payload.get("step") or payload.get("step_key")
        return str(label).strip() if label else None
    if event_type == "run.tool_started":
        tool_name = str(payload.get("tool_name") or "tool").strip()
        return f"Using {tool_name}" if tool_name else "Using a tool"
    if event_type in {"run.tool_completed", "run.tool_failed"}:
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


def _apply_run_events_to_item(item: dict[str, Any], events: list[Any]) -> None:
    activity_trace: list[dict[str, Any]] = []
    work_log: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []

    for event in events:
        event_type = str(getattr(event, "event_type", "") or "")
        if event_type not in _RUN_WORK_EVENT_TYPES:
            continue
        payload = dict(getattr(event, "payload", None) or {})
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


def _resource_identity(resource: dict[str, Any]) -> str:
    for key in ("path", "uri", "repo", "name", "label"):
        value = resource.get(key)
        if isinstance(value, str) and value.strip():
            return f"{key}:{value.strip()}"
    return json.dumps(resource, sort_keys=True, default=str)


def _merge_project_context_payloads(
    base: dict[str, Any] | None,
    addition: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not base:
        return copy.deepcopy(addition) if addition else None
    if not addition:
        return copy.deepcopy(base)

    merged = copy.deepcopy(base)
    resources = [
        dict(resource)
        for resource in (merged.get("resources") or [])
        if isinstance(resource, dict)
    ]
    seen = {_resource_identity(resource) for resource in resources}
    for resource in addition.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        key = _resource_identity(resource)
        if key in seen:
            continue
        seen.add(key)
        resources.append(copy.deepcopy(resource))
    merged["resources"] = resources
    merged.setdefault("source", "cortex-thread-message")
    if addition.get("source"):
        sources = [
            item
            for item in [merged.get("source"), addition.get("source")]
            if isinstance(item, str) and item.strip()
        ]
        if sources:
            merged["source"] = "+".join(dict.fromkeys(sources))
    return merged


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
            explicit = _merge_project_context_payloads(explicit, candidate)
    readable_attachments = project_context_from_text_attachments(attachments)
    return _merge_project_context_payloads(explicit, readable_attachments)


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
    if isinstance(idea.agent_details, dict):
        existing = dict(idea.agent_details or {})
    else:
        existing = {}
        if idea.agent_details not in (None, [], {}):
            existing["legacy_agent_details"] = copy.deepcopy(idea.agent_details)
    existing["project_context"] = project_context
    idea.agent_details = existing


async def _publish_notification_summary_updates(
    *,
    org_id: str | None,
    user_ids: set[str],
) -> None:
    if not org_id or not user_ids:
        return

    with UnitOfWork() as uow:
        for user_id in sorted(user_ids):
            summary = build_notification_summary(uow.session, user_id=user_id, org_id=org_id)
            await ws_manager.publish_notification_summary_updated(
                user_id=user_id,
                summary=summary.model_dump(mode="json"),
            )


# ── Idea operations ────────────────────────────────────────────

@router.post("/ideas/{idea_id}/cancel-all")
def idea_cancel_all(idea_id: str, user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.runs.cortex import cancel_runs_for_idea
    with UnitOfWork() as uow:
        _require_idea_for_user(uow.session, idea_id, user)
    count = cancel_runs_for_idea(idea_id)
    return {"ok": True, "canceled": count, "cancelled": count}


@router.post("/ideas/{idea_id}/mark-read")
async def mark_read(idea_id: str, request: Request, user: dict[str, Any] = Depends(get_current_user)):
    user_id = str(user.get("id"))
    org_id = str(user.get("org_id")) if user.get("org_id") else None
    with UnitOfWork() as uow:
        idea = _require_idea_for_user(uow.session, idea_id, user)
        if idea.status == "unread_reply":
            idea.status = "needs_input"
            idea.read_at = datetime.now(timezone.utc)
            uow.session.add(IdeaStateLog(
                idea_id=idea_id,
                from_state="unread_reply",
                to_state="needs_input",
                trigger="user_read",
            ))
        uow.notifications.mark_read_for_idea(user_id=user_id, idea_id=idea_id)
    try:
        await _publish_notification_summary_updates(org_id=org_id, user_ids={user_id})
    except Exception as exc:
        logger.warning("workspace_notification_summary_publish_failed: %s", exc)
    return {"ok": True}


@router.patch("/ideas/{idea_id}/position")
async def update_position(idea_id: str, request: Request, user: dict[str, Any] = Depends(get_current_user)):
    data = await request.json()
    with UnitOfWork() as uow:
        idea = _require_idea_for_user(uow.session, idea_id, user)
        if idea and idea.archived_at is None:
            idea.position_x = data.get("x")
            idea.position_y = data.get("y")
    return {"ok": True}


@router.post("/ideas/{idea_id}/thread")
async def add_thread_message_raw(idea_id: str, request: Request, user: dict[str, Any] = Depends(get_current_user)):
    """Add thread message — ORM version with full lifecycle logic."""
    data = await request.json()
    role = data.get("role", "user")
    content = data.get("content", "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content is required")
    if role not in ("user", "assistant", "illo"):
        raise HTTPException(status_code=400, detail="Role must be 'user', 'assistant', or 'illo'")
    raw_uid = user.get("id")
    # "system" is not a valid UUID — skip user_id for system/localhost users
    user_id = raw_uid if role == "user" and raw_uid and raw_uid != "system" else None
    org_id = user.get("org_id")

    from brain.systems.cortex.events import publish

    new_status = None
    notification_user_ids: set[str] = set()
    notification_org_id: str | None = str(org_id) if org_id else None
    with UnitOfWork() as uow:
        idea = _require_idea_for_user(uow.session, idea_id, user)
        current_status = idea.status
        if idea.org_id:
            notification_org_id = str(idea.org_id)

        attachments_data = data.get("attachments", [])
        if not isinstance(attachments_data, list):
            attachments_data = []
        metadata = data.get("metadata")
        thread_attachment_context = build_thread_attachment_context(attachments_data)
        project_context = _extract_project_context_from_message(
            attachments_data,
            metadata if isinstance(metadata, dict) else None,
        )
        project_context = _validate_thread_project_context(project_context)
        if project_context or thread_attachment_context:
            next_metadata = dict(metadata or {}) if isinstance(metadata, dict) else {}
            if project_context:
                next_metadata["project_context"] = project_context
            if thread_attachment_context:
                next_metadata["thread_attachment_context"] = thread_attachment_context
            metadata = next_metadata
        if project_context:
            _merge_project_context_into_idea(idea, project_context)
        msg_type = _parse_message_type(content, role)
        thread_msg = IdeaThread(
            idea_id=idea_id,
            role=role,
            content=content,
            attachments=attachments_data,
            metadata_=metadata,
            user_id=user_id,
            message_type=msg_type,
        )
        uow.session.add(thread_msg)
        uow.session.flush()

        _append_live_guidance_from_thread_message(
            session=uow.session,
            idea_id=idea_id,
            role=role,
            content=content,
            metadata=metadata,
            thread_msg=thread_msg,
            user_id=user_id,
        )

        msg = {
            "id": thread_msg.id,
            "idea_id": thread_msg.idea_id,
            "role": thread_msg.role,
            "content": thread_msg.content,
            "attachments": thread_msg.attachments or [],
            "metadata": thread_msg.metadata_,
            "user_id": thread_msg.user_id,
            "message_type": thread_msg.message_type,
            "created_at": thread_msg.created_at.isoformat() if thread_msg.created_at else None,
        }
        if user_id and user.get("name"):
            msg["user_name"] = user.get("name")
            msg["user_color"] = user.get("color", "#6366f1")

        # Extract and resolve @mentions
        thread_msg_id = thread_msg.id
        mentions = _extract_mentions(content)
        person_mentions = [m for m in mentions if m != "illo"]

        if person_mentions and user_id and org_id:
            stmt = (
                select(User.id, func.lower(User.name).label("name"))
                .where(
                    User.org_id == org_id,
                    func.lower(User.name).in_(person_mentions),
                )
            )
            rows = uow.session.execute(stmt).all()
            resolved = {row.name: row.id for row in rows}

            for name in person_mentions:
                if name in resolved:
                    mentioned_user_id = str(resolved[name])
                    if mentioned_user_id == str(user_id):
                        continue
                    _, created = uow.user_mentions.create_if_missing(
                        user_id=mentioned_user_id,
                        idea_id=str(idea_id),
                        mentioned_by=str(user_id),
                        thread_message_id=thread_msg_id,
                    )
                    if created and notification_org_id:
                        preview = compact_notification_text(content)
                        uow.notifications.create_or_coalesce(
                            org_id=notification_org_id,
                            user_id=mentioned_user_id,
                            source=NOTIFICATION_SOURCE_WORKSPACE,
                            kind=NOTIFICATION_KIND_WORKSPACE_MENTION,
                            actor_user_id=str(user_id),
                            title=f"{user.get('name') or 'Someone'} mentioned you in workspace",
                            body=preview,
                            coalesce_key=f"workspace:mention:{mentioned_user_id}:{idea_id}:{thread_msg_id}",
                            payload={
                                "preview": preview,
                                "idea_title": idea.title,
                                "thread_message_id": thread_msg_id,
                            },
                            idea_id=str(idea_id),
                        )
                        notification_user_ids.add(mentioned_user_id)

            for name, uid in resolved.items():
                if str(uid) == str(user_id):
                    continue
                publish("mention", {
                    "idea_id": str(idea_id),
                    "user_id": str(uid),
                    "mentioned_by": {"user_id": str(user_id), "name": user.get("name"), "color": user.get("color")},
                })

        # Auto-lifecycle
        new_status = None
        if role == "user" and current_status in ("needs_input", "unread_reply", "emerged"):
            new_status = "active"
        elif role in ("illo", "assistant") and current_status in ("active", "working", "queued"):
            new_status = "unread_reply"
        elif role == "illo" and current_status not in ("resolved",):
            new_status = "unread_reply"

        if new_status and new_status != current_status:
            idea.status = new_status
            idea.updated_at = datetime.now(timezone.utc)
            uow.session.add(IdeaStateLog(
                idea_id=idea_id,
                from_state=current_status,
                to_state=new_status,
                trigger=f"auto_{role}_message",
            ))
            publish("status_change", {"idea_id": idea_id, "old_status": current_status, "new_status": new_status})
            if (
                new_status == "unread_reply"
                and notification_org_id
                and idea.user_id
            ):
                owner_user_id = str(idea.user_id)
                preview = compact_notification_text(content)
                uow.notifications.create_or_coalesce(
                    org_id=notification_org_id,
                    user_id=owner_user_id,
                    source=NOTIFICATION_SOURCE_WORKSPACE,
                    kind=NOTIFICATION_KIND_WORKSPACE_THREAD_ATTENTION,
                    actor_user_id=None,
                    title=f"Illo replied in {idea.title}",
                    body=preview,
                    coalesce_key=f"workspace:thread_attention:{owner_user_id}:{idea_id}",
                    payload={
                        "preview": preview,
                        "idea_title": idea.title,
                        "thread_message_id": thread_msg_id,
                    },
                    idea_id=str(idea_id),
                )
                notification_user_ids.add(owner_user_id)

    if role == "user":
        feedback_tags = _infer_feedback_tags(content)
        if feedback_tags:
            _record_implicit_feedback(idea_id, content, feedback_tags)

    await ws_manager.broadcast_product_event(
        "thread_message",
        {"idea_id": idea_id, "message": msg},
        org_id=notification_org_id,
    )
    if new_status and new_status != current_status:
        await ws_manager.broadcast_product_event(
            "status_change",
            {"idea_id": idea_id, "new_status": new_status},
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
def mark_mentions_seen(idea_id: str, user: dict[str, Any] = Depends(get_current_user)):
    user_id = user.get("id")
    with UnitOfWork() as uow:
        _require_idea_for_user(uow.session, idea_id, user)
        count = uow.user_mentions.mark_seen_for_idea(
            user_id=str(user_id),
            idea_id=idea_id,
        )
    return {"cleared": count}


@router.get("/mentions/unread")
def get_unread_mentions(user: dict[str, Any] = Depends(get_current_user)):
    user_id = user.get("id")
    with UnitOfWork() as uow:
        mentions = uow.user_mentions.list_unread_for_user(
            user_id=str(user_id),
        )
    return mentions


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

    with UnitOfWork() as uow:
        _require_idea_for_user(uow.session, idea_id, user)

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

def unified_stream_payload(
    idea_id: str,
    include_debug: bool = False,
    user: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    items = []

    with UnitOfWork() as uow:
        _require_idea_for_user(uow.session, idea_id, user)
        stmt = (
            select(IdeaThread, User.name.label("user_name"), User.color.label("user_color"))
            .outerjoin(User, IdeaThread.user_id == User.id)
            .where(IdeaThread.idea_id == idea_id)
            .order_by(IdeaThread.created_at)
        )
        for row in uow.session.execute(stmt).all():
            t = row[0]
            items.append({
                "type": "message",
                "timestamp": t.created_at.isoformat() if t.created_at else "",
                "id": str(t.id),
                "role": t.role,
                "content": t.content,
                "attachments": t.attachments or [],
                "metadata": t.metadata_ or {},
                "message_type": t.message_type,
                "user_name": row.user_name,
                "user_color": row.user_color,
            })

    if include_debug:
        from brain.systems.runs.cortex import idea_run_history
        for dp in idea_run_history(idea_id):
            ts = dp.get("started_at") or dp.get("created_at")
            items.append({
                "type": "run",
                "timestamp": ts.isoformat() if hasattr(ts, 'isoformat') else str(ts),
                **dp,
            })
    else:
        with UnitOfWork() as uow:
            stmt = (
                select(AgentRun)
                .where(AgentRun.thread_id == idea_id)
                .order_by(AgentRun.created_at.desc())
                .limit(20)
            )
            for run in uow.session.scalars(stmt).all():
                items.append(run_stream_payload(run))

    run_ids = [int(item["id"]) for item in items if item.get("type") == "run"]
    for item in items:
        if item["type"] == "run":
            item["tool_calls"] = []

    child_runs: dict[int, list[dict[str, Any]]] = {}
    run_artifacts: dict[int, list[dict[str, Any]]] = {}
    run_events: dict[int, list[Any]] = {}
    if run_ids:
        try:
            with UnitOfWork() as uow:
                children_stmt = (
                    select(AgentRun)
                    .where(AgentRun.parent_run_id.in_(run_ids))
                    .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
                )
                for child in uow.session.scalars(children_stmt).all():
                    if child.parent_run_id is None:
                        continue
                    child_runs.setdefault(int(child.parent_run_id), []).append(run_stream_payload(child))

                artifact_stmt = (
                    select(AgentRunArtifactRow)
                    .where(AgentRunArtifactRow.run_id.in_(run_ids))
                    .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
                )
                for artifact in uow.session.scalars(artifact_stmt).all():
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

                event_stmt = _run_work_events_stmt(run_ids)
                for event in uow.session.scalars(event_stmt).all():
                    run_events.setdefault(int(event.run_id), []).append(event)
        except Exception:
            pass

    for item in items:
        if item["type"] == "run":
            run_id = int(item.get("id", 0))
            children = child_runs.get(run_id, [])
            artifacts = run_artifacts.get(run_id, [])
            events = run_events.get(run_id, [])
            if children:
                item["child_runs"] = children
            if artifacts:
                item["artifacts"] = artifacts
            _apply_run_events_to_item(item, events)

    _append_final_answer_messages(items)

    # Include visual blocks in the stream
    from brain.platform.db.models.idea import VisualBlock
    with UnitOfWork() as uow:
        vb_stmt = (
            select(VisualBlock)
            .where(VisualBlock.idea_id == idea_id)
            .order_by(VisualBlock.created_at)
        )
        for vb in uow.session.scalars(vb_stmt).all():
            items.append({
                "type": "visual_block",
                "timestamp": vb.created_at.isoformat() if vb.created_at else "",
                "id": f"vb-{vb.id}",
                "content_type": vb.content_type,
                "title": vb.title,
                "content": vb.content,
                "display_mode": vb.display_mode or "inline",
                "run_id": vb.run_id,
                "position_after": vb.position_after,
            })

    items.sort(key=lambda x: x.get("timestamp", ""))
    return items


@router.get("/ideas/{idea_id}/unified-stream")
def idea_unified_stream(
    idea_id: str,
    include_debug: bool = False,
    user: dict[str, Any] = Depends(get_current_user),
):
    return unified_stream_payload(idea_id=idea_id, include_debug=include_debug, user=user)
