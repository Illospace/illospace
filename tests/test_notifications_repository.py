from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from brain.platform.db.models.notification import NotificationEvent
from brain.platform.db.repositories.notifications import NotificationEventRepository


class _ScalarResult:
    def __init__(
        self,
        *,
        first: NotificationEvent | None = None,
        all_: list[NotificationEvent] | None = None,
    ) -> None:
        self._first = first
        self._all = all_ if all_ is not None else ([] if first is None else [first])

    def first(self) -> NotificationEvent | None:
        return self._first

    def all(self) -> list[NotificationEvent]:
        return list(self._all)


class _AsyncNotificationSession:
    def __init__(self, *scalar_results: _ScalarResult) -> None:
        self._scalar_results = list(scalar_results)
        self.added: list[NotificationEvent] = []
        self.scalars_statements: list[Any] = []

    async def scalars(self, stmt: Any) -> _ScalarResult:
        self.scalars_statements.append(stmt)
        return self._scalar_results.pop(0)

    def add(self, obj: NotificationEvent) -> None:
        self.added.append(obj)


def _notification(**overrides: Any) -> NotificationEvent:
    values = {
        "org_id": "00000000-0000-0000-0000-000000000001",
        "user_id": "00000000-0000-0000-0000-000000000002",
        "source": "chat",
        "kind": "chat.dm_message",
        "actor_user_id": None,
        "title": "Original",
        "body": None,
        "payload": None,
        "coalesce_key": "chat:dm:user:conversation",
        "occurrence_count": 1,
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return NotificationEvent(**values)


@pytest.mark.asyncio
async def test_async_notification_create_or_coalesce_updates_existing_row():
    existing = _notification(occurrence_count=2)
    session = _AsyncNotificationSession(_ScalarResult(first=existing))
    repo = NotificationEventRepository(session)

    result = await repo.a_create_or_coalesce(
        org_id="00000000-0000-0000-0000-000000000003",
        user_id="00000000-0000-0000-0000-000000000002",
        source="workspace",
        kind="workspace.mention",
        actor_user_id="00000000-0000-0000-0000-000000000004",
        title="Updated",
        body="Mentioned you",
        coalesce_key="chat:dm:user:conversation",
        payload={"idea_id": "idea-1"},
        idea_id="00000000-0000-0000-0000-000000000005",
    )

    assert result is existing
    assert session.added == []
    assert existing.org_id == "00000000-0000-0000-0000-000000000003"
    assert existing.source == "workspace"
    assert existing.kind == "workspace.mention"
    assert existing.title == "Updated"
    assert existing.payload == {"idea_id": "idea-1"}
    assert existing.occurrence_count == 3
    assert existing.updated_at > datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_async_notification_create_or_coalesce_creates_new_row():
    session = _AsyncNotificationSession(_ScalarResult(first=None))
    repo = NotificationEventRepository(session)

    result = await repo.a_create_or_coalesce(
        org_id="00000000-0000-0000-0000-000000000001",
        user_id="00000000-0000-0000-0000-000000000002",
        source="chat",
        kind="chat.dm_message",
        actor_user_id=None,
        title="New DM",
        body="Hello",
        coalesce_key="chat:dm:user:conversation",
        payload={"preview": "Hello"},
        conversation_id="00000000-0000-0000-0000-000000000006",
    )

    assert result is session.added[0]
    assert result.title == "New DM"
    assert result.payload == {"preview": "Hello"}
    assert result.occurrence_count == 1


@pytest.mark.asyncio
async def test_async_notification_mark_all_read_touches_unread_rows():
    notifications = [_notification(), _notification()]
    session = _AsyncNotificationSession(_ScalarResult(all_=notifications))
    repo = NotificationEventRepository(session)

    count = await repo.a_mark_all_read("00000000-0000-0000-0000-000000000002")

    assert count == 2
    assert all(notification.read_at is not None for notification in notifications)
    assert all(notification.updated_at == notification.read_at for notification in notifications)
