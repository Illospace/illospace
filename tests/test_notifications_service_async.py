from __future__ import annotations

from typing import Any

import pytest

from brain.app.api.services.notifications import NotificationService
from brain.platform.db.models.notification import (
    NOTIFICATION_KIND_CHAT_MENTION,
    NotificationEvent,
)


class _FakeDb:
    def __init__(self) -> None:
        self.flushes = 0
        self.scalar_calls = 0

    async def flush(self) -> None:
        self.flushes += 1

    async def scalar(self, stmt: Any) -> int:
        self.scalar_calls += 1
        return 0

    async def execute(self, stmt: Any):
        class _Rows:
            def all(self) -> list[tuple[str, int]]:
                return []

        return _Rows()


class _FakeNotificationRepo:
    def __init__(self, notification: NotificationEvent) -> None:
        self.notification = notification

    async def a_mark_read(self, notification_id: int, user_id: str) -> NotificationEvent:
        assert notification_id == 123
        assert user_id == "user-1"
        return self.notification

    async def a_count_unread(self, user_id: str) -> int:
        assert user_id == "user-1"
        return 0

    async def a_count_unread_by_source(self, user_id: str) -> dict[str, int]:
        assert user_id == "user-1"
        return {}


class _FakeChatNotificationRepo:
    def __init__(self) -> None:
        self.thread_reads: list[tuple[str, str, int]] = []

    async def a_mark_read_for_thread(
        self,
        *,
        user_id: str,
        conversation_id: str,
        thread_root_message_id: int,
    ) -> int:
        self.thread_reads.append((user_id, conversation_id, thread_root_message_id))
        return 1


class _FakeChatMentionRepo:
    def __init__(self) -> None:
        self.thread_reads: list[tuple[str, str, int]] = []

    async def a_mark_seen_for_thread(
        self,
        *,
        user_id: str,
        conversation_id: str,
        thread_root_message_id: int,
    ) -> int:
        self.thread_reads.append((user_id, conversation_id, thread_root_message_id))
        return 1


@pytest.mark.asyncio
async def test_async_notification_service_marks_chat_thread_sources_read():
    notification = NotificationEvent(
        id=123,
        org_id="org-1",
        user_id="user-1",
        source="chat",
        kind=NOTIFICATION_KIND_CHAT_MENTION,
        actor_user_id="actor-1",
        title="Mention",
        body="Ping",
        coalesce_key="chat:mention:user-1:conversation-1:7",
        conversation_id="conversation-1",
        thread_root_message_id=7,
        occurrence_count=1,
    )
    db = _FakeDb()
    service = NotificationService(db, {"id": "user-1", "org_id": "org-1"})
    chat_notifications = _FakeChatNotificationRepo()
    chat_mentions = _FakeChatMentionRepo()
    service.notification_repo = _FakeNotificationRepo(notification)
    service.chat_notification_repo = chat_notifications
    service.chat_mention_repo = chat_mentions

    summary = await service.mark_read(123)

    assert db.flushes == 1
    assert db.scalar_calls == 3
    assert summary.unread_notification_total == 0
    assert chat_notifications.thread_reads == [("user-1", "conversation-1", 7)]
    assert chat_mentions.thread_reads == [("user-1", "conversation-1", 7)]
