"""Repositories for the unified notification inbox."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, select

from brain.platform.db.models.notification import NotificationEvent
from brain.platform.db.repositories.base import BaseRepository


class NotificationEventRepository(BaseRepository[NotificationEvent]):
    model = NotificationEvent

    def list_for_user(
        self,
        user_id: str,
        *,
        unread_only: bool = True,
        limit: int = 50,
    ) -> Sequence[NotificationEvent]:
        stmt = (
            select(NotificationEvent)
            .where(NotificationEvent.user_id == user_id)
            .order_by(NotificationEvent.updated_at.desc(), NotificationEvent.id.desc())
            .limit(limit)
        )
        if unread_only:
            stmt = stmt.where(NotificationEvent.read_at.is_(None))
        return self._session.scalars(stmt).all()

    def count_unread(self, user_id: str) -> int:
        return int(
            self._session.scalar(
                select(func.count(NotificationEvent.id)).where(
                    NotificationEvent.user_id == user_id,
                    NotificationEvent.read_at.is_(None),
                )
            )
            or 0
        )

    def count_unread_by_source(self, user_id: str) -> dict[str, int]:
        rows = self._session.execute(
            select(NotificationEvent.source, func.count(NotificationEvent.id))
            .where(
                NotificationEvent.user_id == user_id,
                NotificationEvent.read_at.is_(None),
            )
            .group_by(NotificationEvent.source)
        ).all()
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
        now = datetime.now(timezone.utc)
        existing = self._session.scalars(
            select(NotificationEvent)
            .where(
                NotificationEvent.user_id == user_id,
                NotificationEvent.coalesce_key == coalesce_key,
                NotificationEvent.read_at.is_(None),
            )
            .order_by(NotificationEvent.updated_at.desc(), NotificationEvent.id.desc())
        ).first()
        if existing is not None:
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
            existing.updated_at = now
            return existing

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
            updated_at=now,
        )
        return notification

    def mark_read(self, notification_id: int, user_id: str) -> NotificationEvent | None:
        notification = self._session.scalars(
            select(NotificationEvent).where(
                NotificationEvent.id == notification_id,
                NotificationEvent.user_id == user_id,
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
        now = datetime.now(timezone.utc)
        notifications = self._session.scalars(
            select(NotificationEvent).where(
                NotificationEvent.user_id == user_id,
                NotificationEvent.read_at.is_(None),
            )
        ).all()
        for notification in notifications:
            notification.read_at = now
            notification.updated_at = now
        return len(notifications)

    def mark_read_for_idea(
        self,
        *,
        user_id: str,
        idea_id: str,
        kinds: Sequence[str] | None = None,
    ) -> int:
        now = datetime.now(timezone.utc)
        stmt = select(NotificationEvent).where(
            NotificationEvent.user_id == user_id,
            NotificationEvent.idea_id == idea_id,
            NotificationEvent.read_at.is_(None),
        )
        if kinds:
            stmt = stmt.where(NotificationEvent.kind.in_(list(kinds)))
        notifications = self._session.scalars(stmt).all()
        for notification in notifications:
            notification.read_at = now
            notification.updated_at = now
        return len(notifications)

    def mark_read_for_chat_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> int:
        now = datetime.now(timezone.utc)
        notifications = self._session.scalars(
            select(NotificationEvent).where(
                NotificationEvent.user_id == user_id,
                NotificationEvent.source == "chat",
                NotificationEvent.conversation_id == conversation_id,
                NotificationEvent.read_at.is_(None),
            )
        ).all()
        for notification in notifications:
            notification.read_at = now
            notification.updated_at = now
        return len(notifications)
