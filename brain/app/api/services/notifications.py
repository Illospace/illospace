"""Unified notification inbox services."""
from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.schemas.notifications import (
    NotificationPreferencesRead,
    NotificationPreferencesUpdate,
    NotificationRead,
    NotificationSummaryRead,
)
from brain.platform.db.models.chat import ChatConversation, ChatConversationMember, ChatConversationRead
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.org import User
from brain.platform.db.models.notification import (
    NOTIFICATION_KIND_CHAT_DM_MESSAGE,
    NOTIFICATION_KIND_CHAT_MENTION,
    NOTIFICATION_KIND_CHAT_ROOM_MESSAGE,
    NOTIFICATION_KIND_WORKSPACE_MENTION,
    NOTIFICATION_KIND_WORKSPACE_THREAD_ATTENTION,
    NotificationEvent,
)
from brain.platform.db.repositories.chat import (
    ChatMessageMentionRepository,
    ChatNotificationRepository,
)
from brain.platform.db.repositories.ideas import UserMentionRepository
from brain.platform.db.repositories.notifications import NotificationEventRepository


def compact_notification_text(text: str | None, limit: int = 160) -> str | None:
    normalized = " ".join((text or "").split())
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(limit - 3, 0)].rstrip()}..."


def _chat_unread_total_stmt(*, user_id: str, org_id: str):
    return (
        select(
            func.coalesce(
                func.sum(
                    ChatConversation.last_message_seq
                    - func.coalesce(ChatConversationRead.last_read_conversation_seq, 0)
                ),
                0,
            )
        )
        .select_from(ChatConversation)
        .join(
            ChatConversationMember,
            and_(
                ChatConversationMember.conversation_id == ChatConversation.id,
                ChatConversationMember.user_id == user_id,
            ),
        )
        .outerjoin(
            ChatConversationRead,
            and_(
                ChatConversationRead.conversation_id == ChatConversation.id,
                ChatConversationRead.user_id == user_id,
            ),
        )
        .where(
            ChatConversation.org_id == org_id,
            ChatConversation.is_archived.is_(False),
        )
    )


def _workspace_attention_total_stmt(*, user_id: str, org_id: str):
    return select(func.count(Idea.id)).where(
        Idea.org_id == org_id,
        Idea.user_id == user_id,
        Idea.archived_at.is_(None),
        Idea.status == "unread_reply",
    )


def _notification_summary_read(
    *,
    unread_notification_total: int,
    unread_by_source: dict[str, int],
    chat_unread_total: int,
    workspace_attention_total: int,
) -> NotificationSummaryRead:
    return NotificationSummaryRead(
        chat_unread_total=chat_unread_total,
        workspace_attention_total=workspace_attention_total,
        unread_notification_total=unread_notification_total,
        unread_chat_notification_total=int(unread_by_source.get("chat", 0)),
        unread_workspace_notification_total=int(unread_by_source.get("workspace", 0)),
    )


async def async_build_notification_summary(
    db: AsyncSession,
    *,
    user_id: str,
    org_id: str,
) -> NotificationSummaryRead:
    repo = NotificationEventRepository(db)
    unread_notification_total = await repo.a_count_unread(user_id)
    unread_by_source = await repo.a_count_unread_by_source(user_id)
    chat_unread_total = int(
        await db.scalar(_chat_unread_total_stmt(user_id=user_id, org_id=org_id)) or 0
    )
    workspace_attention_total = int(
        await db.scalar(_workspace_attention_total_stmt(user_id=user_id, org_id=org_id)) or 0
    )
    return _notification_summary_read(
        unread_notification_total=unread_notification_total,
        unread_by_source=unread_by_source,
        chat_unread_total=chat_unread_total,
        workspace_attention_total=workspace_attention_total,
    )


class NotificationService:
    def __init__(self, db: AsyncSession, user: dict[str, Any]):
        self.db = db
        self.user = user
        self.viewer_user_id = str(user["id"])
        self.org_id = self._require_org_id(user)
        self.notification_repo = NotificationEventRepository(db)
        self.chat_notification_repo = ChatNotificationRepository(db)
        self.chat_mention_repo = ChatMessageMentionRepository(db)
        self.user_mention_repo = UserMentionRepository(db)

    async def summary(self) -> NotificationSummaryRead:
        return await async_build_notification_summary(
            self.db,
            user_id=self.viewer_user_id,
            org_id=self.org_id,
        )

    async def preferences(self) -> NotificationPreferencesRead:
        user = await self.db.get(User, self.viewer_user_id)
        return NotificationPreferencesRead(
            sound_enabled=bool(getattr(user, "notification_sound_enabled", True)) if user else True,
            message_notifications_enabled=(
                bool(getattr(user, "message_notifications_enabled", True)) if user else True
            ),
        )

    async def update_preferences(
        self,
        update: NotificationPreferencesUpdate,
    ) -> NotificationPreferencesRead:
        user = await self.db.get(User, self.viewer_user_id)
        if user is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="User not found")
        if update.sound_enabled is not None:
            user.notification_sound_enabled = update.sound_enabled
        if update.message_notifications_enabled is not None:
            user.message_notifications_enabled = update.message_notifications_enabled
        await self.db.flush()
        return await self.preferences()

    async def list_notifications(
        self,
        *,
        unread_only: bool = True,
        limit: int = 50,
    ) -> list[NotificationRead]:
        notifications = list(
            await self.notification_repo.a_list_for_user(
                self.viewer_user_id,
                unread_only=unread_only,
                limit=max(1, min(limit, 100)),
            )
        )
        actors = await self._actors_by_id(notifications)
        return [self._serialize(notification, actors) for notification in notifications]

    async def mark_read(self, notification_id: int) -> NotificationSummaryRead:
        notification = await self.notification_repo.a_mark_read(notification_id, self.viewer_user_id)
        if notification is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Notification not found")
        await self._sync_source_read_state(notification)
        await self.db.flush()
        return await self.summary()

    async def mark_all_read(self) -> NotificationSummaryRead:
        notifications = list(
            await self.notification_repo.a_list_for_user(
                self.viewer_user_id,
                unread_only=True,
                limit=500,
            )
        )
        await self.notification_repo.a_mark_all_read(self.viewer_user_id)
        for notification in notifications:
            await self._sync_source_read_state(notification)
        await self.db.flush()
        return await self.summary()

    async def _actors_by_id(self, notifications: list[NotificationEvent]) -> dict[str, User]:
        actor_ids = sorted({str(item.actor_user_id) for item in notifications if item.actor_user_id})
        if not actor_ids:
            return {}
        users = (await self.db.scalars(select(User).where(User.id.in_(actor_ids)))).all()
        return {str(user.id): user for user in users}

    def _serialize(
        self,
        notification: NotificationEvent,
        actors_by_id: dict[str, User],
    ) -> NotificationRead:
        actor = actors_by_id.get(str(notification.actor_user_id)) if notification.actor_user_id else None
        return NotificationRead(
            id=notification.id,
            source=notification.source,
            kind=notification.kind,
            title=notification.title,
            body=notification.body,
            actor_user_id=str(notification.actor_user_id) if notification.actor_user_id else None,
            actor_name=actor.name if actor else None,
            actor_color=actor.color if actor else None,
            idea_id=str(notification.idea_id) if notification.idea_id else None,
            conversation_id=str(notification.conversation_id) if notification.conversation_id else None,
            thread_root_message_id=notification.thread_root_message_id,
            occurrence_count=notification.occurrence_count or 1,
            payload=notification.payload,
            created_at=notification.created_at,
            updated_at=notification.updated_at,
            read_at=notification.read_at,
        )

    async def _sync_source_read_state(self, notification: NotificationEvent) -> None:
        if notification.kind in {
            NOTIFICATION_KIND_CHAT_DM_MESSAGE,
            NOTIFICATION_KIND_CHAT_ROOM_MESSAGE,
        }:
            if notification.conversation_id:
                await self.chat_notification_repo.a_mark_read_for_conversation(
                    user_id=self.viewer_user_id,
                    conversation_id=str(notification.conversation_id),
                )
            return

        if notification.kind == NOTIFICATION_KIND_CHAT_MENTION:
            if notification.conversation_id and notification.thread_root_message_id is not None:
                await self.chat_notification_repo.a_mark_read_for_thread(
                    user_id=self.viewer_user_id,
                    conversation_id=str(notification.conversation_id),
                    thread_root_message_id=notification.thread_root_message_id,
                )
                await self.chat_mention_repo.a_mark_seen_for_thread(
                    user_id=self.viewer_user_id,
                    conversation_id=str(notification.conversation_id),
                    thread_root_message_id=notification.thread_root_message_id,
                )
            elif notification.conversation_id:
                await self.chat_notification_repo.a_mark_read_for_conversation(
                    user_id=self.viewer_user_id,
                    conversation_id=str(notification.conversation_id),
                )
            return

        if notification.kind == NOTIFICATION_KIND_WORKSPACE_MENTION:
            if notification.idea_id:
                thread_message_id = (notification.payload or {}).get("thread_message_id")
                if thread_message_id is not None:
                    await self.user_mention_repo.a_mark_seen_for_thread_message(
                        user_id=self.viewer_user_id,
                        idea_id=str(notification.idea_id),
                        thread_message_id=int(thread_message_id),
                    )
                else:
                    await self.user_mention_repo.a_mark_seen_for_idea(
                        user_id=self.viewer_user_id,
                        idea_id=str(notification.idea_id),
                    )
            return

        if notification.kind == NOTIFICATION_KIND_WORKSPACE_THREAD_ATTENTION:
            return

    @staticmethod
    def _require_org_id(user: dict[str, Any]) -> str:
        from fastapi import HTTPException

        org_id = user.get("org_id")
        if not org_id:
            raise HTTPException(status_code=400, detail="User is not attached to an org")
        return str(org_id)
