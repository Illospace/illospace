"""Thread Discussion endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db
from brain.app.api.routers.cortex._helpers import _a_require_idea_for_user as _require_idea_for_user
from brain.app.api.routers.cortex._router import router
from brain.app.api.routers.ws import ws_manager
from brain.app.mentions import classify_mention_intent
from brain.platform.db.models.idea import ThreadDiscussionComment
from brain.platform.db.models.org import User
from brain.systems.cortex.object_references import (
    SOURCE_THREAD_DISCUSSION_COMMENT,
    merge_object_reference_metadata,
    store_object_references_for_source,
)

THREAD_DISCUSSION_SURFACE = "thread_discussion"
THREAD_DISCUSSION_REPLY_TOOL = "post_thread_discussion_reply"


class DiscussionCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


def _comment_payload(
    comment: ThreadDiscussionComment,
    *,
    author_name: str | None = None,
    author_color: str | None = None,
) -> dict[str, Any]:
    metadata = comment.metadata_ if isinstance(comment.metadata_, dict) else {}
    return {
        "id": comment.id,
        "thread_id": str(comment.thread_id),
        "org_id": str(comment.org_id),
        "author_user_id": str(comment.author_user_id) if comment.author_user_id else None,
        "author_kind": comment.author_kind,
        "author_name": author_name,
        "author_color": author_color,
        "body": comment.body,
        "attachments": comment.attachments or [],
        "metadata": metadata,
        "object_references": metadata.get("object_references") or [],
        "thread_references": metadata.get("thread_references") or [],
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
    }


def _thread_org_id(idea: Any, user: dict[str, Any]) -> str:
    return str(getattr(idea, "org_id", None) or user.get("org_id") or "")


def _discussion_trigger_context(
    *,
    idea: Any,
    comment: ThreadDiscussionComment,
    user: dict[str, Any],
) -> dict[str, Any]:
    thread_id = str(getattr(idea, "id", "") or comment.thread_id)
    comment_id = comment.id
    author_user_id = str(comment.author_user_id or user.get("id") or "") or None
    return {
        "surface": THREAD_DISCUSSION_SURFACE,
        "thread_id": thread_id,
        "comment_id": comment_id,
        "body": comment.body,
        "author_user_id": author_user_id,
        "response_target": {
            "surface": THREAD_DISCUSSION_SURFACE,
            "thread_id": thread_id,
            "reply_to_comment_id": comment_id,
        },
    }


def _discussion_trigger_metadata(
    *,
    comment: ThreadDiscussionComment,
    discussion_trigger: dict[str, Any],
) -> dict[str, Any]:
    return {
        **dict(comment.metadata_ or {}),
        "source": THREAD_DISCUSSION_SURFACE,
        "originating_surface": THREAD_DISCUSSION_SURFACE,
        "triggering_surface": THREAD_DISCUSSION_SURFACE,
        "source_surface": THREAD_DISCUSSION_SURFACE,
        "discussion_comment_id": comment.id,
        "discussion_trigger": discussion_trigger,
        "required_response_tool": THREAD_DISCUSSION_REPLY_TOOL,
        "final_answer_target_surface": THREAD_DISCUSSION_SURFACE,
        "thread_message": comment.body,
        "request_source_context": {
            "surface": THREAD_DISCUSSION_SURFACE,
            "thread_id": discussion_trigger["thread_id"],
            "comment_id": comment.id,
            "response_tool": THREAD_DISCUSSION_REPLY_TOOL,
        },
    }


async def _discussion_comment_payloads(
    db: AsyncSession,
    *,
    thread_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    stmt = (
        select(ThreadDiscussionComment, User.name.label("author_name"), User.color.label("author_color"))
        .outerjoin(User, ThreadDiscussionComment.author_user_id == User.id)
        .where(ThreadDiscussionComment.thread_id == str(thread_id))
        .order_by(ThreadDiscussionComment.created_at.asc(), ThreadDiscussionComment.id.asc())
        .limit(max(1, min(int(limit or 100), 300)))
    )
    return [
        _comment_payload(row[0], author_name=row.author_name, author_color=row.author_color)
        for row in (await db.execute(stmt)).all()
    ]


async def _trigger_illo_from_discussion(
    db: AsyncSession,
    *,
    idea: Any,
    comment: ThreadDiscussionComment,
    user: dict[str, Any],
) -> dict[str, Any]:
    from brain.app.triggers.adapters.internal import build_thread_discussion_mention_trigger
    from brain.app.triggers.router import async_route_trigger

    discussion_trigger = _discussion_trigger_context(idea=idea, comment=comment, user=user)
    metadata = _discussion_trigger_metadata(comment=comment, discussion_trigger=discussion_trigger)
    trigger = build_thread_discussion_mention_trigger(
        idea_id=str(idea.id),
        idea=idea,
        comment=comment,
        user=user,
        discussion_trigger=discussion_trigger,
        metadata=metadata,
        priority=0,
    )
    return (await async_route_trigger(trigger, session=db)).to_response()


@router.get("/ideas/{idea_id}/discussion")
async def list_thread_discussion(
    idea_id: str,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    await _require_idea_for_user(db, idea_id, user)
    return await _discussion_comment_payloads(db, thread_id=idea_id, limit=limit)


@router.post("/ideas/{idea_id}/discussion", status_code=201)
async def create_thread_discussion_comment(
    idea_id: str,
    body: DiscussionCommentCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    idea = await _require_idea_for_user(db, idea_id, user)
    text = body.body.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Discussion comment body is required")

    attachments = body.attachments if isinstance(body.attachments, list) else []
    org_id = _thread_org_id(idea, user)
    comment = ThreadDiscussionComment(
        thread_id=str(idea.id),
        org_id=org_id,
        author_user_id=str(user.get("id")) if user.get("id") else None,
        author_kind="user",
        body=text,
        attachments=attachments,
        metadata_=dict(body.metadata or {}),
    )
    db.add(comment)
    await db.flush()

    references = await store_object_references_for_source(
        db,
        source_type=SOURCE_THREAD_DISCUSSION_COMMENT,
        source_id=comment.id,
        org_id=org_id,
        text=text,
        user_id=str(user.get("id")) if user.get("id") else None,
    )
    if references:
        comment.metadata_ = merge_object_reference_metadata(comment.metadata_, references)
        await db.flush()

    trigger = None
    if classify_mention_intent(text, invoke_without_mentions=False).should_invoke_illo:
        trigger = await _trigger_illo_from_discussion(db, idea=idea, comment=comment, user=user)

    payload = _comment_payload(
        comment,
        author_name=user.get("name"),
        author_color=user.get("color"),
    )
    await db.commit()
    await ws_manager.broadcast_product_event(
        "thread_discussion_comment",
        {"idea_id": str(idea.id), "comment": payload},
        org_id=org_id,
    )
    return {
        "comment": payload,
        "trigger": trigger,
    }
