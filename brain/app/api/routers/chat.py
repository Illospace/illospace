"""Native chat API for the shared room and DMs."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.chat import (
    ChatBootstrapRead,
    ChatConversationSummaryRead,
    ChatDmCreate,
    ChatMessageCreate,
    ChatMessagePageRead,
    ChatMessageRead,
    ChatNotificationRead,
    ChatReadUpdate,
    ChatSearchResultRead,
    ChatThreadRead,
    ChatUnreadSummaryRead,
)
from brain.app.api.services.chat import (
    ChatPublishState,
    ChatReadPublishState,
    ChatService,
    extract_mention_tokens,
    resolve_user_mentions,
    validated_limit,
)
from brain.app.api.services.notifications import async_build_notification_summary
from brain.platform.db.models.chat import ChatConversation, ChatMessage

router = APIRouter(
    prefix="/api/chat",
    tags=["chat"],
    dependencies=[Depends(rate_limit)],
)
logger = logging.getLogger(__name__)


async def _publish_message_events(publish: ChatPublishState, *, is_thread_reply: bool) -> None:
    from brain.app.api.routers.ws import ws_manager

    if is_thread_reply:
        await ws_manager.publish_chat_thread_reply_created(
            conversation_id=publish.conversation_id,
            thread_root_message_id=publish.root_message_id,
            message=publish.message.model_dump(mode="json"),
        )
        if publish.root_message is not None:
            await ws_manager.publish_chat_thread_summary_updated(
                conversation_id=publish.conversation_id,
                thread_root_message_id=publish.root_message_id,
                root_message=publish.root_message.model_dump(mode="json"),
            )
    else:
        await ws_manager.publish_chat_message_created(
            conversation_id=publish.conversation_id,
            message=publish.message.model_dump(mode="json"),
        )

    for member_id in publish.member_ids:
        await ws_manager.publish_chat_unread_updated(
            user_id=member_id,
            conversation_id=publish.conversation_id,
            unread_summary=publish.unread_by_user[member_id].model_dump(mode="json"),
        )
        for notification in publish.notifications_by_user.get(member_id, []):
            await ws_manager.publish_chat_notification_created(
                user_id=member_id,
                notification=notification.model_dump(mode="json"),
            )


async def _publish_read_event(publish: ChatReadPublishState) -> None:
    from brain.app.api.routers.ws import ws_manager

    await ws_manager.publish_chat_read_updated(
        user_id=publish.user_id,
        conversation_id=publish.conversation_id,
        last_read_message_id=publish.last_read_message_id,
        last_read_conversation_seq=publish.last_read_conversation_seq,
    )
    await ws_manager.publish_chat_unread_updated(
        user_id=publish.user_id,
        conversation_id=publish.conversation_id,
        unread_summary=publish.unread_summary.model_dump(mode="json"),
    )


async def _publish_notification_summary_payloads(summaries: dict[str, dict[str, Any]]) -> None:
    from brain.app.api.routers.ws import ws_manager

    for user_id, summary in sorted(summaries.items()):
        await ws_manager.publish_notification_summary_updated(
            user_id=user_id,
            summary=summary,
        )


async def _route_chat_illo_if_needed(
    db: AsyncSession,
    *,
    user: dict[str, Any],
    message_id: int,
) -> None:
    message = await db.get(ChatMessage, int(message_id))
    if message is None:
        return
    metadata = dict(message.metadata_ or {})
    if not metadata.get("illo_invoked"):
        return
    conversation = await db.get(ChatConversation, str(message.conversation_id))
    if conversation is None or conversation.type != "room":
        return

    from brain.app.triggers.adapters.internal import build_chat_mention_trigger
    from brain.app.triggers.router import async_route_trigger

    root_message = (
        await db.get(ChatMessage, int(message.thread_root_message_id))
        if message.thread_root_message_id
        else None
    )
    event = "room_thread_mention" if message.thread_root_message_id else "room_message_mention"
    trigger = build_chat_mention_trigger(
        event=event,
        conversation=conversation,
        message=message,
        root_message=root_message,
        user=user,
    )
    result = await async_route_trigger(trigger, session=db)
    if result.ok and result.run_id is not None:
        metadata["illo_run_id"] = result.run_id
        message.metadata_ = metadata


async def _notification_summary_payloads(
    db: AsyncSession,
    *,
    org_id: str,
    user_ids: list[str],
) -> dict[str, dict[str, Any]]:
    return {
        user_id: (
            await async_build_notification_summary(
                db,
                user_id=user_id,
                org_id=org_id,
            )
        ).model_dump(mode="json")
        for user_id in sorted(set(user_ids))
    }


@router.get("/bootstrap", response_model=ChatBootstrapRead)
async def get_chat_bootstrap(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await ChatService(db, user).bootstrap()


@router.get("/conversations", response_model=list[ChatConversationSummaryRead])
async def list_conversations(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await ChatService(db, user).list_conversations()


@router.post("/dms", response_model=ChatConversationSummaryRead)
async def create_or_fetch_dm(
    body: ChatDmCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await ChatService(db, user).create_or_fetch_dm(body)


@router.get("/conversations/{conversation_id}/messages", response_model=ChatMessagePageRead)
async def get_conversation_messages(
    conversation_id: str,
    before_seq: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await ChatService(db, user).get_conversation_messages(
        conversation_id,
        before_seq=before_seq,
        limit=validated_limit(limit),
    )


@router.get("/search", response_model=list[ChatSearchResultRead])
async def search_room_messages(
    query: str = Query(min_length=2, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await ChatService(db, user).search_room_messages(
        query=query,
        limit=validated_limit(limit),
    )


@router.post("/conversations/{conversation_id}/messages", response_model=ChatMessageRead)
async def post_conversation_message(
    conversation_id: str,
    body: ChatMessageCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    message, publish = await ChatService(db, user).post_conversation_message(conversation_id, body)
    await _route_chat_illo_if_needed(db, user=user, message_id=message.id)
    await db.commit()
    summaries = await _notification_summary_payloads(
        db,
        org_id=str(user["org_id"]),
        user_ids=publish.member_ids,
    )
    await _publish_message_events(publish, is_thread_reply=False)
    try:
        await _publish_notification_summary_payloads(summaries)
    except Exception as exc:
        logger.warning("notification_summary_publish_failed: %s", exc)
    return message


@router.get("/messages/{message_id}/thread", response_model=ChatThreadRead)
async def get_message_thread(
    message_id: int,
    before_seq: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await ChatService(db, user).get_message_thread(
        message_id,
        before_seq=before_seq,
        limit=validated_limit(limit),
    )


@router.post("/messages/{message_id}/thread", response_model=ChatMessageRead)
async def post_thread_reply(
    message_id: int,
    body: ChatMessageCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    reply, publish = await ChatService(db, user).post_thread_reply(message_id, body)
    await _route_chat_illo_if_needed(db, user=user, message_id=reply.id)
    await db.commit()
    summaries = await _notification_summary_payloads(
        db,
        org_id=str(user["org_id"]),
        user_ids=publish.member_ids,
    )
    await _publish_message_events(publish, is_thread_reply=True)
    try:
        await _publish_notification_summary_payloads(summaries)
    except Exception as exc:
        logger.warning("notification_summary_publish_failed: %s", exc)
    return reply


@router.post("/conversations/{conversation_id}/read", response_model=ChatUnreadSummaryRead)
async def mark_conversation_read(
    conversation_id: str,
    body: ChatReadUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    unread_summary, publish = await ChatService(db, user).mark_conversation_read(conversation_id, body)
    await db.commit()
    summaries = await _notification_summary_payloads(
        db,
        org_id=str(user["org_id"]),
        user_ids=[str(user["id"])],
    )
    await _publish_read_event(publish)
    try:
        await _publish_notification_summary_payloads(summaries)
    except Exception as exc:
        logger.warning("notification_summary_publish_failed: %s", exc)
    return unread_summary


@router.get("/notifications", response_model=list[ChatNotificationRead])
async def list_notifications(
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await ChatService(db, user).list_notifications(limit=validated_limit(limit))


@router.post("/notifications/{notification_id}/read", response_model=dict[str, bool])
async def mark_notification_read(
    notification_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    await ChatService(db, user).mark_notification_read(notification_id)
    return {"ok": True}


@router.post("/notifications/read-all", response_model=dict[str, int])
async def mark_all_notifications_read(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    count = await ChatService(db, user).mark_all_notifications_read()
    return {"updated": count}
