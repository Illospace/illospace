"""Repositories for the unified notification inbox."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, select

from brain.platform.db.models.notification import NotificationEvent
from brain.platform.db.repositories.base import BaseRepository


class NotificationEventRepository(BaseRepository[NotificationEvent]):
    model = NotificationEvent

    @staticmethod
    def _list_for_user_stmt(
        user_id: str,
        *,
        unread_only: bool,
        limit: int,
    ):
        stmt = (
            select(NotificationEvent)
            .where(NotificationEvent.user_id == user_id)
            .order_by(NotificationEvent.updated_at.desc(), NotificationEvent.id.desc())
            .limit(limit)
        )
        if unread_only:
            stmt = stmt.where(NotificationEvent.read_at.is_(None))
        return stmt

    @staticmethod
    def _count_unread_stmt(user_id: str):
        return select(func.count(NotificationEvent.id)).where(
            NotificationEvent.user_id == user_id,
            NotificationEvent.read_at.is_(None),
        )

    @staticmethod
    def _count_unread_by_source_stmt(user_id: str):
        return (
            select(NotificationEvent.source, func.count(NotificationEvent.id))
            .where(
                NotificationEvent.user_id == user_id,
                NotificationEvent.read_at.is_(None),
            )
            .group_by(NotificationEvent.source)
        )

    @staticmethod
    def _coalesced_unread_stmt(user_id: str, coalesce_key: str):
        return (
            select(NotificationEvent)
            .where(
                NotificationEvent.user_id == user_id,
                NotificationEvent.coalesce_key == coalesce_key,
                NotificationEvent.read_at.is_(None),
            )
            .order_by(NotificationEvent.updated_at.desc(), NotificationEvent.id.desc())
        )

    @staticmethod
    def _notification_for_user_stmt(notification_id: int, user_id: str):
        return select(NotificationEvent).where(
            NotificationEvent.id == notification_id,
            NotificationEvent.user_id == user_id,
        )

    @staticmethod
    def _unread_for_user_stmt(user_id: str):
        return select(NotificationEvent).where(
            NotificationEvent.user_id == user_id,
            NotificationEvent.read_at.is_(None),
        )

    @staticmethod
    def _unread_for_idea_stmt(
        *,
        user_id: str,
        idea_id: str,
        kinds: Sequence[str] | None,
    ):
        stmt = select(NotificationEvent).where(
            NotificationEvent.user_id == user_id,
            NotificationEvent.idea_id == idea_id,
            NotificationEvent.read_at.is_(None),
        )
        if kinds:
            stmt = stmt.where(NotificationEvent.kind.in_(list(kinds)))
        return stmt

    @staticmethod
    def _unread_for_chat_conversation_stmt(*, user_id: str, conversation_id: str):
        return select(NotificationEvent).where(
            NotificationEvent.user_id == user_id,
            NotificationEvent.source == "chat",
            NotificationEvent.conversation_id == conversation_id,
            NotificationEvent.read_at.is_(None),
        )

    @staticmethod
    def _touch_unread(notifications: Sequence[NotificationEvent]) -> int:
        now = datetime.now(timezone.utc)
        for notification in notifications:
            notification.read_at = now
            notification.updated_at = now
        return len(notifications)

    @staticmethod
    def _update_coalesced(
        existing: NotificationEvent,
        *,
        org_id: str,
        source: str,
        kind: str,
        actor_user_id: str | None,
        title: str,
        body: str | None,
        payload: dict | None,
        idea_id: str | None,
        conversation_id: str | None,
        thread_root_message_id: int | None,
    ) -> NotificationEvent:
        existing.org_id = org_id
        existing.source = source
        existing.kind = kind
        existing.actor_user_id = actor_user_id
        existing.idea_id = idea_id
        existing.conversation_id = conversation_id
        existing.thread_root_message_id = thread_root_message_id
        existing.title = title
        existing.body = body
        existing.payload = payload
        existing.occurrence_count = max(1, int(existing.occurrence_count or 1)) + 1
        existing.updated_at = datetime.now(timezone.utc)
        return existing

    def list_for_user(
        self,
        user_id: str,
        *,
        unread_only: bool = True,
        limit: int = 50,
    ) -> Sequence[NotificationEvent]:
        return self._session.scalars(
            self._list_for_user_stmt(user_id, unread_only=unread_only, limit=limit)
        ).all()

    async def a_list_for_user(
        self,
        user_id: str,
        *,
        unread_only: bool = True,
        limit: int = 50,
    ) -> Sequence[NotificationEvent]:
        return (
            await self._session.scalars(
                self._list_for_user_stmt(user_id, unread_only=unread_only, limit=limit)
            )
        ).all()

    def count_unread(self, user_id: str) -> int:
        return int(self._session.scalar(self._count_unread_stmt(user_id)) or 0)

    async def a_count_unread(self, user_id: str) -> int:
        return int(await self._session.scalar(self._count_unread_stmt(user_id)) or 0)

    def count_unread_by_source(self, user_id: str) -> dict[str, int]:
        rows = self._session.execute(self._count_unread_by_source_stmt(user_id)).all()
        return {str(source): int(count) for source, count in rows}

    async def a_count_unread_by_source(self, user_id: str) -> dict[str, int]:
        rows = (await self._session.execute(self._count_unread_by_source_stmt(user_id))).all()
        return {str(source): int(count) for source, count in rows}

    def create_or_coalesce(
        self,
        *,
        org_id: str,
        user_id: str,
        source: str,
        kind: str,
        actor_user_id: str | None,
        title: str,
        body: str | None,
        coalesce_key: str,
        payload: dict | None = None,
        idea_id: str | None = None,
        conversation_id: str | None = None,
        thread_root_message_id: int | None = None,
    ) -> NotificationEvent:
        existing = self._session.scalars(
            self._coalesced_unread_stmt(user_id, coalesce_key)
        ).first()
        if existing is not None:
            return self._update_coalesced(
                existing,
                org_id=org_id,
                source=source,
                kind=kind,
                actor_user_id=actor_user_id,
                title=title,
                body=body,
                payload=payload,
                idea_id=idea_id,
                conversation_id=conversation_id,
                thread_root_message_id=thread_root_message_id,
            )

        notification = self.create(
            org_id=org_id,
            user_id=user_id,
            source=source,
            kind=kind,
            actor_user_id=actor_user_id,
            idea_id=idea_id,
            conversation_id=conversation_id,
            thread_root_message_id=thread_root_message_id,
            title=title,
            body=body,
            payload=payload,
            coalesce_key=coalesce_key,
            occurrence_count=1,
            updated_at=datetime.now(timezone.utc),
        )
        return notification

    async def a_create_or_coalesce(
        self,
        *,
        org_id: str,
        user_id: str,
        source: str,
        kind: str,
        actor_user_id: str | None,
        title: str,
        body: str | None,
        coalesce_key: str,
        payload: dict | None = None,
        idea_id: str | None = None,
        conversation_id: str | None = None,
        thread_root_message_id: int | None = None,
    ) -> NotificationEvent:
        existing = (
            await self._session.scalars(self._coalesced_unread_stmt(user_id, coalesce_key))
        ).first()
        if existing is not None:
            return self._update_coalesced(
                existing,
                org_id=org_id,
                source=source,
                kind=kind,
                actor_user_id=actor_user_id,
                title=title,
                body=body,
                payload=payload,
                idea_id=idea_id,
                conversation_id=conversation_id,
                thread_root_message_id=thread_root_message_id,
            )

        return await self.a_create(
            org_id=org_id,
            user_id=user_id,
            source=source,
            kind=kind,
            actor_user_id=actor_user_id,
            idea_id=idea_id,
            conversation_id=conversation_id,
            thread_root_message_id=thread_root_message_id,
            title=title,
            body=body,
            payload=payload,
            coalesce_key=coalesce_key,
            occurrence_count=1,
            updated_at=datetime.now(timezone.utc),
        )

    def mark_read(self, notification_id: int, user_id: str) -> NotificationEvent | None:
        notification = self._session.scalars(
            self._notification_for_user_stmt(notification_id, user_id)
        ).first()
        if notification is None:
            return None
        if notification.read_at is None:
            now = datetime.now(timezone.utc)
            notification.read_at = now
            notification.updated_at = now
        return notification

    async def a_mark_read(self, notification_id: int, user_id: str) -> NotificationEvent | None:
        notification = (
            await self._session.scalars(
                self._notification_for_user_stmt(notification_id, user_id)
            )
        ).first()
        if notification is None:
            return None
        if notification.read_at is None:
            now = datetime.now(timezone.utc)
            notification.read_at = now
            notification.updated_at = now
        return notification

    def mark_all_read(self, user_id: str) -> int:
        notifications = self._session.scalars(self._unread_for_user_stmt(user_id)).all()
        return self._touch_unread(notifications)

    async def a_mark_all_read(self, user_id: str) -> int:
        notifications = (await self._session.scalars(self._unread_for_user_stmt(user_id))).all()
        return self._touch_unread(notifications)

    def mark_read_for_idea(
        self,
        *,
        user_id: str,
        idea_id: str,
        kinds: Sequence[str] | None = None,
    ) -> int:
        notifications = self._session.scalars(
            self._unread_for_idea_stmt(user_id=user_id, idea_id=idea_id, kinds=kinds)
        ).all()
        return self._touch_unread(notifications)

    async def a_mark_read_for_idea(
        self,
        *,
        user_id: str,
        idea_id: str,
        kinds: Sequence[str] | None = None,
    ) -> int:
        notifications = (
            await self._session.scalars(
                self._unread_for_idea_stmt(user_id=user_id, idea_id=idea_id, kinds=kinds)
            )
        ).all()
        return self._touch_unread(notifications)

    def mark_read_for_chat_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> int:
        notifications = self._session.scalars(
            self._unread_for_chat_conversation_stmt(
                user_id=user_id,
                conversation_id=conversation_id,
            )
        ).all()
        return self._touch_unread(notifications)

    async def a_mark_read_for_chat_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> int:
        notifications = (
            await self._session.scalars(
                self._unread_for_chat_conversation_stmt(
                    user_id=user_id,
                    conversation_id=conversation_id,
                )
            )
        ).all()
        return self._touch_unread(notifications)
