"""Native chat tool handlers for AgentRun."""
from __future__ import annotations

import json
from typing import Any

from brain.systems.runs.tool_catalog.handlers.common import _agent_context, logger

THREAD_DISCUSSION_SURFACE = "thread_discussion"


def _execution_metadata() -> dict[str, Any]:
    metadata = getattr(_agent_context, "execution_metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _metadata_trigger(name: str) -> dict[str, Any]:
    trigger = _execution_metadata().get(name)
    return dict(trigger) if isinstance(trigger, dict) else {}


def _current_chat_trigger() -> dict[str, Any]:
    trigger = getattr(_agent_context, "chat_trigger", None)
    if isinstance(trigger, dict):
        return dict(trigger)
    return _metadata_trigger("chat_trigger")


def _current_discussion_trigger() -> dict[str, Any]:
    trigger = _metadata_trigger("discussion_trigger")
    if trigger:
        return trigger
    target_ref = getattr(_agent_context, "target_ref", None)
    if isinstance(target_ref, dict) and isinstance(target_ref.get("discussion_trigger"), dict):
        return dict(target_ref["discussion_trigger"])
    return {}


def _current_run_id() -> int | None:
    run_id = getattr(_agent_context, "run_id", None)
    if run_id is None:
        run = getattr(_agent_context, "run", None)
        run_id = getattr(run, "run_id", None) or getattr(run, "id", None)
    try:
        return int(run_id) if run_id is not None else None
    except Exception:
        return None


def _current_thread_id() -> str | None:
    thread_id = getattr(_agent_context, "idea_id", None) or getattr(_agent_context, "thread_id", None)
    if thread_id:
        return str(thread_id)
    run = getattr(_agent_context, "run", None)
    thread_id = getattr(run, "thread_id", None)
    if thread_id:
        return str(thread_id)
    target_ref = _execution_metadata().get("target_ref")
    if isinstance(target_ref, dict):
        candidate = target_ref.get("idea_id") or target_ref.get("thread_id")
        if candidate:
            return str(candidate)
    return None


def _coerce_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("thread_root_message_id must be an integer")


def _coerce_comment_id(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("reply_to_comment_id must be an integer")


def _first_nonempty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _discussion_comment_payload(comment: Any) -> dict[str, Any]:
    return {
        "id": comment.id,
        "thread_id": str(comment.thread_id),
        "org_id": str(comment.org_id),
        "author_user_id": str(comment.author_user_id) if comment.author_user_id else None,
        "author_kind": comment.author_kind,
        "author_name": None,
        "author_color": None,
        "body": comment.body,
        "attachments": comment.attachments or [],
        "metadata": comment.metadata_ or {},
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
    }


async def _publish_chat_events(publish, summaries: dict[str, dict[str, Any]]) -> None:
    from brain.app.api.routers.chat import (
        _publish_message_events,
        _publish_notification_summary_payloads,
    )

    await _publish_message_events(
        publish,
        is_thread_reply=publish.root_message is not None,
    )
    await _publish_notification_summary_payloads(summaries)


async def _handle_post_chat_message(
    body: str,
    conversation_id: str | None = None,
    thread_root_message_id: int | None = None,
) -> str:
    """Post an Illo-authored message to the native team room."""
    from brain.app.api.routers.chat import _notification_summary_payloads
    from brain.app.api.services.chat import ChatService
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    trigger = _current_chat_trigger()
    response_target = trigger.get("response_target") if isinstance(trigger.get("response_target"), dict) else {}
    target_conversation_id = str(conversation_id or response_target.get("conversation_id") or "").strip()
    target_thread_root_message_id = (
        _coerce_optional_int(thread_root_message_id)
        if thread_root_message_id is not None
        else _coerce_optional_int(response_target.get("thread_root_message_id"))
    )
    if not target_conversation_id:
        return json.dumps({"error": "post_chat_message requires conversation_id outside a chat-triggered run"})
    actor_user_id = str(getattr(_agent_context, "user_id", "") or "").strip()
    org_id = str(getattr(_agent_context, "org_id", "") or "").strip()
    if not actor_user_id or not org_id:
        return json.dumps({"error": "post_chat_message requires a user-scoped org run"})

    async with UnitOfWork() as uow:
        message, publish = await ChatService(
            uow.session,
            {
                "id": actor_user_id,
                "org_id": org_id,
                "role": "member",
            },
        ).post_agent_message(
            conversation_id=target_conversation_id,
            body=body,
            thread_root_message_id=target_thread_root_message_id,
            metadata={
                "created_by_run_id": _current_run_id(),
                "chat_trigger_message_id": trigger.get("message_id"),
            },
        )
        summaries = await _notification_summary_payloads(
            uow.session,
            org_id=org_id,
            user_ids=publish.member_ids,
        )
        message_payload = message.model_dump(mode="json")
    if publish is not None:
        try:
            await _publish_chat_events(publish, summaries)
        except Exception as exc:
            logger.warning("agent_chat_publish_failed: %s", exc)
    return json.dumps({"ok": True, "message": message_payload}, default=str)


async def _handle_post_thread_discussion_reply(
    body: str,
    thread_id: str | None = None,
    reply_to_comment_id: int | None = None,
) -> str:
    """Post an Illo-authored reply to a Thread's Discussion surface."""
    from brain.app.api.routers.ws import ws_manager
    from brain.platform.db.models.idea import Idea, ThreadDiscussionComment
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    text = str(body or "").strip()
    if not text:
        return json.dumps({"error": "post_thread_discussion_reply requires body"})

    trigger = _current_discussion_trigger()
    response_target = trigger.get("response_target") if isinstance(trigger.get("response_target"), dict) else {}
    target_thread_id = _first_nonempty(
        thread_id,
        response_target.get("thread_id"),
        trigger.get("thread_id"),
        _current_thread_id(),
    )
    if not target_thread_id:
        return json.dumps({"error": "post_thread_discussion_reply requires a Thread id or active Thread run"})

    org_id = str(getattr(_agent_context, "org_id", "") or "").strip()
    if not org_id:
        return json.dumps({"error": "post_thread_discussion_reply requires an org-scoped run"})

    target_reply_to_comment_id = (
        _coerce_comment_id(reply_to_comment_id)
        if reply_to_comment_id is not None
        else _coerce_comment_id(response_target.get("reply_to_comment_id") or trigger.get("comment_id"))
    )

    async with UnitOfWork() as uow:
        idea = await uow.session.get(Idea, target_thread_id)
        if idea is None:
            return json.dumps({"error": "Thread not found"})
        idea_org_id = str(getattr(idea, "org_id", "") or "")
        if idea_org_id and idea_org_id != org_id:
            return json.dumps({"error": "Thread is outside this org"})

        metadata = {
            "source": "illo_agent",
            "surface": THREAD_DISCUSSION_SURFACE,
            "created_by_run_id": _current_run_id(),
        }
        if target_reply_to_comment_id is not None:
            metadata["reply_to_comment_id"] = target_reply_to_comment_id
        comment = ThreadDiscussionComment(
            thread_id=target_thread_id,
            org_id=org_id,
            author_user_id=None,
            author_kind="illo",
            body=text,
            attachments=[],
            metadata_=metadata,
        )
        uow.session.add(comment)
        await uow.session.flush()
        payload = _discussion_comment_payload(comment)

    await ws_manager.broadcast_product_event(
        "thread_discussion_comment",
        {"idea_id": target_thread_id, "comment": payload},
        org_id=org_id,
    )
    return json.dumps({"ok": True, "comment": payload}, default=str)


async def _handle_read_thread_discussion(
    thread_id: str | None = None,
    limit: int = 50,
) -> str:
    """Read Discussion comments intentionally from the current Thread."""
    from sqlalchemy import select

    from brain.platform.db.models.idea import ThreadDiscussionComment
    from brain.platform.db.models.org import User
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    target_thread_id = str(thread_id or _current_thread_id() or "").strip()
    if not target_thread_id:
        return json.dumps({"error": "read_thread_discussion requires a Thread id or an active Thread run"})
    org_id = str(getattr(_agent_context, "org_id", "") or "").strip()
    if not org_id:
        return json.dumps({"error": "read_thread_discussion requires an org-scoped run"})
    capped_limit = max(1, min(int(limit or 50), 200))

    async with UnitOfWork() as uow:
        stmt = (
            select(ThreadDiscussionComment, User.name.label("author_name"), User.color.label("author_color"))
            .outerjoin(User, ThreadDiscussionComment.author_user_id == User.id)
            .where(ThreadDiscussionComment.thread_id == target_thread_id)
            .where(ThreadDiscussionComment.org_id == org_id)
            .order_by(ThreadDiscussionComment.created_at.desc(), ThreadDiscussionComment.id.desc())
            .limit(capped_limit)
        )
        rows = list((await uow.session.execute(stmt)).all())

    comments = []
    for row in reversed(rows):
        comment = row[0]
        comments.append(
            {
                "id": comment.id,
                "thread_id": str(comment.thread_id),
                "author_kind": comment.author_kind,
                "author_user_id": str(comment.author_user_id) if comment.author_user_id else None,
                "author_name": row.author_name,
                "author_color": row.author_color,
                "body": comment.body,
                "attachments": comment.attachments or [],
                "metadata": comment.metadata_ or {},
                "created_at": comment.created_at.isoformat() if comment.created_at else None,
            }
        )
    return json.dumps(
        {
            "thread_id": target_thread_id,
            "count": len(comments),
            "comments": comments,
        },
        default=str,
    )


__all__ = [
    "_handle_post_chat_message",
    "_handle_post_thread_discussion_reply",
    "_handle_read_thread_discussion",
]
