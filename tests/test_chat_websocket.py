"""Chat websocket contract tests for conversation and thread scoping."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.schemas.chat import ChatDmCreate, ChatMessageCreate
from brain.app.api.services.chat import ChatService
from brain.app.api.ws.auth import WsTokenClaims
from brain.app.api.ws.events import ServerEvent
from brain.app.api.ws.manager import ConnectionManager
from brain.platform.db.models.chat import ChatConversationRead, ChatNotification
from tests.test_chat_api_routes import (
    ORG_ID,
    USER_1_ID,
    USER_2_ID,
    _user_context,
    chat_db_session,
)


pytestmark = pytest.mark.asyncio


def _claims(user_id: str, org_id: str = "org-1") -> WsTokenClaims:
    return WsTokenClaims(
        user_id=user_id,
        org_id=org_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        session_id=f"session-{user_id}",
    )


async def test_conversation_events_only_fan_out_to_subscribed_sockets():
    manager = ConnectionManager()
    ws_room_a = AsyncMock()
    ws_room_b = AsyncMock()
    ws_other_room = AsyncMock()

    await manager.connect(_claims("user-1"), ws_room_a)
    await manager.connect(_claims("user-2"), ws_room_b)
    await manager.connect(_claims("user-3"), ws_other_room)
    for ws in (ws_room_a, ws_room_b, ws_other_room):
        ws.send_json.reset_mock()

    await manager.subscribe_conversation("user-1", ws_room_a, "room-1")
    await manager.subscribe_conversation("user-2", ws_room_b, "room-1")
    await manager.subscribe_conversation("user-3", ws_other_room, "room-2")
    for ws in (ws_room_a, ws_room_b, ws_other_room):
        ws.send_json.reset_mock()

    await manager.publish_chat_message_created(
        conversation_id="room-1",
        message={"id": 11, "conversation_id": "room-1"},
    )

    ws_room_a.send_json.assert_called_once_with(
        {
            "type": ServerEvent.CHAT_MESSAGE_CREATED,
            "conversation_id": "room-1",
            "message": {"id": 11, "conversation_id": "room-1"},
        }
    )
    ws_room_b.send_json.assert_called_once_with(
        {
            "type": ServerEvent.CHAT_MESSAGE_CREATED,
            "conversation_id": "room-1",
            "message": {"id": 11, "conversation_id": "room-1"},
        }
    )
    ws_other_room.send_json.assert_not_called()


async def test_thread_subscriptions_are_cleaned_up_on_disconnect():
    manager = ConnectionManager()
    ws_thread_a = AsyncMock()
    ws_thread_b = AsyncMock()

    await manager.connect(_claims("user-1"), ws_thread_a)
    await manager.connect(_claims("user-2"), ws_thread_b)
    for ws in (ws_thread_a, ws_thread_b):
        ws.send_json.reset_mock()

    await manager.subscribe_thread(
        "user-1",
        ws_thread_a,
        conversation_id="room-1",
        thread_root_message_id=42,
    )
    await manager.subscribe_thread(
        "user-2",
        ws_thread_b,
        conversation_id="room-1",
        thread_root_message_id=42,
    )
    for ws in (ws_thread_a, ws_thread_b):
        ws.send_json.reset_mock()

    await manager.disconnect("user-1", ws_thread_a)
    for ws in (ws_thread_a, ws_thread_b):
        ws.send_json.reset_mock()

    await manager.publish_chat_thread_reply_created(
        conversation_id="room-1",
        thread_root_message_id=42,
        message={"id": 84, "thread_root_message_id": 42},
    )

    ws_thread_a.send_json.assert_not_called()
    ws_thread_b.send_json.assert_called_once_with(
        {
            "type": ServerEvent.CHAT_THREAD_REPLY_CREATED,
            "conversation_id": "room-1",
            "thread_root_message_id": 42,
            "message": {"id": 84, "thread_root_message_id": 42},
        }
    )


async def test_thread_reply_events_reach_room_conversation_subscribers():
    manager = ConnectionManager()
    ws_room = AsyncMock()
    ws_thread = AsyncMock()
    ws_other_room = AsyncMock()

    await manager.connect(_claims("user-1"), ws_room)
    await manager.connect(_claims("user-2"), ws_thread)
    await manager.connect(_claims("user-3"), ws_other_room)
    for ws in (ws_room, ws_thread, ws_other_room):
        ws.send_json.reset_mock()

    await manager.subscribe_conversation("user-1", ws_room, "room-1")
    await manager.subscribe_thread(
        "user-2",
        ws_thread,
        conversation_id="room-1",
        thread_root_message_id=42,
    )
    await manager.subscribe_conversation("user-3", ws_other_room, "room-2")
    for ws in (ws_room, ws_thread, ws_other_room):
        ws.send_json.reset_mock()

    await manager.publish_chat_thread_reply_created(
        conversation_id="room-1",
        thread_root_message_id=42,
        message={"id": 84, "thread_root_message_id": 42},
    )

    expected = {
        "type": ServerEvent.CHAT_THREAD_REPLY_CREATED,
        "conversation_id": "room-1",
        "thread_root_message_id": 42,
        "message": {"id": 84, "thread_root_message_id": 42},
    }
    ws_room.send_json.assert_called_once_with(expected)
    ws_thread.send_json.assert_called_once_with(expected)
    ws_other_room.send_json.assert_not_called()


async def test_unsubscribe_only_affects_existing_socket_subscriptions():
    manager = ConnectionManager()
    ws_subscribed = AsyncMock()
    ws_intruder = AsyncMock()

    await manager.connect(_claims("user-1"), ws_subscribed)
    await manager.connect(_claims("user-2"), ws_intruder)
    for ws in (ws_subscribed, ws_intruder):
        ws.send_json.reset_mock()

    await manager.subscribe_conversation("user-1", ws_subscribed, "room-1")
    await manager.subscribe_thread(
        "user-1",
        ws_subscribed,
        conversation_id="room-1",
        thread_root_message_id=42,
    )
    for ws in (ws_subscribed, ws_intruder):
        ws.send_json.reset_mock()

    await manager.unsubscribe_conversation("user-2", ws_intruder, "room-1")
    await manager.unsubscribe_thread("user-2", ws_intruder, thread_root_message_id=42)

    ws_subscribed.send_json.assert_not_called()
    ws_intruder.send_json.assert_not_called()


async def test_chat_mark_read_rejects_broadcast_only_unread_payload(monkeypatch: pytest.MonkeyPatch):
    from brain.app.api.routers import ws as ws_router

    fake_ws = AsyncMock()
    monkeypatch.setattr(
        ws_router,
        "UnitOfWork",
        lambda: pytest.fail("invalid read payload should not open a database session"),
    )

    await ws_router._handle_chat_mark_read(
        "user-2",
        "org-1",
        {
            "conversation_id": "conversation-1",
            "unread_count": 0,
            "unread_summary": {"room": 0, "dms": 0, "total": 0},
        },
        fake_ws,
    )

    fake_ws.send_json.assert_awaited_once_with(
        {"type": ServerEvent.CHAT_ERROR, "code": "CHAT_READ_CURSOR_REQUIRED"}
    )


class _SessionUnitOfWork:
    def __init__(self, session: AsyncSession):
        self._db_session = session
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            await self._db_session.rollback()
        else:
            await self._db_session.commit()
        return False


@pytest.mark.requires_db
async def test_chat_mark_read_persists_and_publishes_server_state_after_commit(
    chat_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    from brain.app.api.routers import ws as ws_router

    dm, sent = await _create_unread_dm(chat_db_session)
    events: list[str] = []
    published: dict[str, dict] = {}

    original_commit = chat_db_session.commit

    async def commit_spy():
        events.append("commit")
        return await original_commit()

    async def publish_read_updated(**kwargs):
        events.append("publish_read")
        published["read"] = kwargs

    async def publish_unread_updated(**kwargs):
        events.append("publish_unread")
        published["unread"] = kwargs

    async def publish_notification_summary_updated(**kwargs):
        events.append("publish_notification_summary")
        published["notification_summary"] = kwargs

    monkeypatch.setattr(ws_router, "UnitOfWork", lambda: _SessionUnitOfWork(chat_db_session))
    monkeypatch.setattr(chat_db_session, "commit", commit_spy)
    monkeypatch.setattr(ws_router.ws_manager, "publish_chat_read_updated", publish_read_updated)
    monkeypatch.setattr(ws_router.ws_manager, "publish_chat_unread_updated", publish_unread_updated)
    monkeypatch.setattr(
        ws_router.ws_manager,
        "publish_notification_summary_updated",
        publish_notification_summary_updated,
    )

    fake_ws = AsyncMock()
    await ws_router._handle_chat_mark_read(
        USER_2_ID,
        ORG_ID,
        {
            "conversation_id": dm.id,
            "last_read_message_id": sent.id,
            "unread_count": 999,
            "unread_summary": {"room": 999, "dms": 999, "total": 999},
        },
        fake_ws,
    )

    fake_ws.send_json.assert_not_called()
    assert events == [
        "commit",
        "publish_read",
        "publish_unread",
        "publish_notification_summary",
    ]
    assert published["read"] == {
        "user_id": USER_2_ID,
        "conversation_id": dm.id,
        "last_read_message_id": sent.id,
        "last_read_conversation_seq": sent.conversation_seq,
    }
    assert published["unread"] == {
        "user_id": USER_2_ID,
        "conversation_id": dm.id,
        "unread_summary": {"room": 0, "dms": 0, "total": 0},
    }
    assert published["notification_summary"]["user_id"] == USER_2_ID
    assert published["notification_summary"]["summary"]["chat_unread_total"] == 0

    read_state = (await chat_db_session.scalars(
        select(ChatConversationRead).where(
            ChatConversationRead.conversation_id == dm.id,
            ChatConversationRead.user_id == USER_2_ID,
        )
    )).one()
    assert read_state.last_read_conversation_seq == sent.conversation_seq
    assert read_state.last_read_message_id == sent.id

    notification = (await chat_db_session.scalars(
        select(ChatNotification).where(
            ChatNotification.user_id == USER_2_ID,
            ChatNotification.message_id == sent.id,
        )
    )).one()
    assert notification.read_at is not None


@pytest.mark.requires_db
async def test_chat_mark_read_does_not_publish_when_commit_fails(
    chat_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    from brain.app.api.routers import ws as ws_router

    dm, sent = await _create_unread_dm(chat_db_session)
    events: list[str] = []

    async def failing_commit():
        events.append("commit")
        raise RuntimeError("boom")

    async def unexpected_publish(**kwargs):
        events.append("publish")

    monkeypatch.setattr(ws_router, "UnitOfWork", lambda: _SessionUnitOfWork(chat_db_session))
    monkeypatch.setattr(chat_db_session, "commit", failing_commit)
    monkeypatch.setattr(ws_router.ws_manager, "publish_chat_read_updated", unexpected_publish)
    monkeypatch.setattr(ws_router.ws_manager, "publish_chat_unread_updated", unexpected_publish)
    monkeypatch.setattr(
        ws_router.ws_manager,
        "publish_notification_summary_updated",
        unexpected_publish,
    )

    fake_ws = AsyncMock()
    await ws_router._handle_chat_mark_read(
        USER_2_ID,
        ORG_ID,
        {
            "conversation_id": dm.id,
            "last_read_message_id": sent.id,
        },
        fake_ws,
    )

    assert events == ["commit"]
    fake_ws.send_json.assert_awaited_once_with(
        {"type": ServerEvent.CHAT_ERROR, "code": "CHAT_MARK_READ_FAILED"}
    )


async def _create_unread_dm(chat_db_session: AsyncSession):
    dm = await ChatService(
        chat_db_session,
        await _user_context(chat_db_session, USER_1_ID),
    ).create_or_fetch_dm(ChatDmCreate(user_id=USER_2_ID))
    await chat_db_session.commit()
    sent, _ = await ChatService(
        chat_db_session,
        await _user_context(chat_db_session, USER_1_ID),
    ).post_conversation_message(
        dm.id,
        ChatMessageCreate(body="Persistent WS read state"),
    )
    await chat_db_session.commit()
    return dm, sent
