"""Focused route verification for the native chat backend MVP."""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient, Response
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.schema import CreateTable

from brain.kernel import config
from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.main import app
from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.chat import (
    ChatConversation,
    ChatConversationMember,
    ChatConversationRead,
    ChatMessage,
    ChatMessageMention,
    ChatNotification,
)
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.notification import NotificationEvent
from brain.platform.db.models.org import Org, User
from tests.db_engine_utils import create_async_test_engine

pytestmark = pytest.mark.requires_db

_TABLES = (
    Org.__table__,
    User.__table__,
    AgentRunRow.__table__,
    AgentRunEventRow.__table__,
    Idea.__table__,
    ChatConversation.__table__,
    ChatConversationMember.__table__,
    ChatMessage.__table__,
    ChatMessageMention.__table__,
    ChatNotification.__table__,
    ChatConversationRead.__table__,
    NotificationEvent.__table__,
)


ORG_ID = '00000000-0000-0000-0000-000000000001'
USER_1_ID = '10000000-0000-0000-0000-000000000001'
USER_2_ID = '10000000-0000-0000-0000-000000000002'
USER_3_ID = '10000000-0000-0000-0000-000000000003'


def _schema_name() -> str:
    return f"chat_test_{uuid.uuid4().hex[:8]}"


async def _seed_users(session: AsyncSession, schema: str) -> None:
    session.add(Org(id=ORG_ID, name="Org 1", slug=f"slug-{schema}"))
    session.add_all(
        [
            User(
                id=USER_1_ID,
                org_id=ORG_ID,
                name="Alex Example",
                email=f"alex-{schema}@example.com",
                color="#111111",
                role="owner",
                approved=True,
            ),
            User(
                id=USER_2_ID,
                org_id=ORG_ID,
                name="Riley Example",
                email=f"redam-{schema}@example.com",
                color="#222222",
                role="member",
                approved=True,
            ),
            User(
                id=USER_3_ID,
                org_id=ORG_ID,
                name="Sam Guest",
                email=f"sam-{schema}@example.com",
                color="#333333",
                role="member",
                approved=False,
            ),
        ]
    )
    await session.commit()


async def _user_context(session: AsyncSession, user_id: str) -> dict[str, str]:
    user = await session.get(User, user_id)
    assert user is not None
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "color": user.color,
        "org_id": user.org_id,
    }


@pytest_asyncio.fixture
async def chat_db_session() -> AsyncIterator[AsyncSession]:
    schema = _schema_name()
    engine = create_async_test_engine(config.DB_URL)
    try:
        admin_conn = await engine.connect()
    except (OSError, SQLAlchemyError) as exc:
        await engine.dispose()
        pytest.skip(f"Postgres test DB unavailable: {exc}")
    admin_conn = await admin_conn.execution_options(isolation_level="AUTOCOMMIT")
    await admin_conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    conn = await engine.connect()
    session: AsyncSession | None = None
    try:
        await conn.execute(text(f'SET search_path TO "{schema}", public'))
        for table in _TABLES:
            await conn.execute(CreateTable(table))
        await conn.commit()

        factory = async_sessionmaker(bind=conn, expire_on_commit=False)
        session = factory()
        await session.execute(text(f'SET search_path TO "{schema}", public'))
        await _seed_users(session, schema)
        yield session
    finally:
        if session is not None:
            await session.close()
        await conn.close()
        await admin_conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_conn.close()
        await engine.dispose()


@pytest.fixture
def request_as(chat_db_session: AsyncSession) -> Callable[..., Awaitable[Response]]:
    async def override_db() -> AsyncIterator[AsyncSession]:
        try:
            yield chat_db_session
            await chat_db_session.commit()
        except Exception:
            await chat_db_session.rollback()
            raise

    async def _request(user_id: str, method: str, path: str, **kwargs) -> Response:
        async def override_user() -> dict[str, str]:
            return await _user_context(chat_db_session, user_id)

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = override_user
        app.dependency_overrides[rate_limit] = lambda: None
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, **kwargs)
        finally:
            app.dependency_overrides.clear()

    return _request


async def test_openapi_registers_chat_routes():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/chat/bootstrap" in paths
    assert "/api/chat/conversations" in paths
    assert "/api/chat/dms" in paths
    assert "/api/chat/conversations/{conversation_id}/messages" in paths
    assert "/api/chat/search" in paths
    assert "/api/chat/messages/{message_id}/thread" in paths
    assert "/api/chat/unreads" in paths
    assert "/api/chat/notifications" in paths


async def test_bootstrap_creates_room_and_syncs_new_approved_members(
    chat_db_session: AsyncSession,
    request_as: Callable[..., object],
):
    response = await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")
    assert response.status_code == 200

    payload = response.json()
    room_id = payload["room"]["id"]
    assert payload["room"]["stable_key"] == "org-room"
    assert payload["room"]["participant_count"] == 2
    assert payload["default_conversation_id"] == room_id

    members_result = await chat_db_session.scalars(
        select(ChatConversationMember.user_id).where(
            ChatConversationMember.conversation_id == room_id
        )
    )
    members = members_result.all()
    assert sorted(str(member_id) for member_id in members) == [USER_1_ID, USER_2_ID]

    pending_user = await chat_db_session.get(User, USER_3_ID)
    assert pending_user is not None
    pending_user.approved = True
    await chat_db_session.commit()

    second_response = await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")
    assert second_response.status_code == 200
    assert second_response.json()["room"]["participant_count"] == 3


async def test_dm_creation_is_idempotent_from_either_side(
    chat_db_session: AsyncSession,
    request_as: Callable[..., object],
):
    first = await request_as(USER_1_ID, "POST", "/api/chat/dms", json={"user_id": USER_2_ID})
    second = await request_as(USER_1_ID, "POST", "/api/chat/dms", json={"user_id": USER_2_ID})
    third = await request_as(USER_2_ID, "POST", "/api/chat/dms", json={"user_id": USER_1_ID})

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200

    first_payload = first.json()
    assert first_payload["id"] == second.json()["id"] == third.json()["id"]
    assert first_payload["stable_key"] == f"dm:{USER_1_ID}:{USER_2_ID}"

    dm_count = await chat_db_session.scalar(
        select(func.count()).select_from(ChatConversation).where(ChatConversation.type == "dm")
    )
    assert dm_count == 1


async def test_room_history_excludes_replies_and_thread_history_returns_root_plus_replies(
    request_as: Callable[..., object],
):
    room = (await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")).json()["room"]
    root_message = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{room['id']}/messages",
            json={"body": "Ship the first room message"},
        )
    ).json()
    reply = (
        await request_as(
            USER_2_ID,
            "POST",
            f"/api/chat/messages/{root_message['id']}/thread",
            json={"body": "Reply inside the room thread"},
        )
    ).json()

    room_page = await request_as(
        USER_1_ID,
        "GET",
        f"/api/chat/conversations/{room['id']}/messages",
    )
    assert room_page.status_code == 200
    room_payload = room_page.json()
    assert [message["id"] for message in room_payload["messages"]] == [root_message["id"]]
    assert room_payload["messages"][0]["reply_count"] == 1
    assert room_payload["messages"][0]["last_reply_message_id"] == reply["id"]
    assert room_payload["messages"][0]["thread_preview_participants"][0]["id"] == reply["sender_user_id"]
    assert room_payload["messages"][0]["thread_preview_participants"][0]["name"] == reply["sender_name"]
    assert room_payload["messages"][0]["thread_preview_participants"][0]["color"] == reply["sender_color"]
    assert room_payload["messages"][0]["thread_preview_participants"][0]["email"]

    thread_page = await request_as(
        USER_1_ID,
        "GET",
        f"/api/chat/messages/{root_message['id']}/thread",
    )
    assert thread_page.status_code == 200
    thread_payload = thread_page.json()
    assert thread_payload["root_message"]["id"] == root_message["id"]
    assert [message["id"] for message in thread_payload["replies"]] == [reply["id"]]
    assert thread_payload["root_message"]["reply_count"] == 1
    assert thread_payload["root_message"]["last_reply_message_id"] == reply["id"]
    assert thread_payload["root_message"]["thread_preview_participants"][0]["id"] == reply["sender_user_id"]
    assert thread_payload["root_message"]["thread_preview_participants"][0]["name"] == reply["sender_name"]
    assert thread_payload["root_message"]["thread_preview_participants"][0]["color"] == reply["sender_color"]
    assert thread_payload["root_message"]["thread_preview_participants"][0]["email"]


async def test_unread_threads_groups_room_replies_by_root(
    request_as: Callable[..., object],
):
    room = (await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")).json()["room"]
    root = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{room['id']}/messages",
            json={"body": "Root unread for the team"},
        )
    ).json()
    reply = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/messages/{root['id']}/thread",
            json={"body": "Follow-up unread reply"},
        )
    ).json()

    response = await request_as(USER_2_ID, "GET", "/api/chat/unreads")
    assert response.status_code == 200

    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["kind"] == "thread"
    assert payload[0]["conversation"]["id"] == room["id"]
    assert payload[0]["root_message"]["id"] == root["id"]
    assert [message["id"] for message in payload[0]["unread_messages"]] == [root["id"], reply["id"]]
    assert payload[0]["unread_count"] == 2


async def test_unread_threads_follow_chat_read_cursor_after_notification_read(
    request_as: Callable[..., object],
):
    dm = (await request_as(USER_1_ID, "POST", "/api/chat/dms", json={"user_id": USER_2_ID})).json()
    sent = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{dm['id']}/messages",
            json={"body": "Still unread in chat"},
        )
    ).json()

    notifications = (await request_as(USER_2_ID, "GET", "/api/chat/notifications")).json()
    read_notification = await request_as(
        USER_2_ID,
        "POST",
        f"/api/chat/notifications/{notifications[0]['id']}/read",
    )
    assert read_notification.status_code == 200

    bootstrap = (await request_as(USER_2_ID, "GET", "/api/chat/bootstrap")).json()
    assert bootstrap["unread_summary"]["dms"] == 1

    response = await request_as(USER_2_ID, "GET", "/api/chat/unreads")
    assert response.status_code == 200

    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["kind"] == "dm"
    assert payload[0]["conversation"]["id"] == dm["id"]
    assert payload[0]["unread_count"] == 1
    assert payload[0]["notification_ids"] == []
    assert [message["id"] for message in payload[0]["unread_messages"]] == [sent["id"]]


async def test_unread_threads_limit_applies_after_grouping(
    request_as: Callable[..., object],
):
    dm = (await request_as(USER_1_ID, "POST", "/api/chat/dms", json={"user_id": USER_2_ID})).json()
    dm_message = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{dm['id']}/messages",
            json={"body": "Older unread DM"},
        )
    ).json()

    room = (await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")).json()["room"]
    root = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{room['id']}/messages",
            json={"body": "Newer root unread"},
        )
    ).json()
    replies = []
    for index in range(6):
        reply = (
            await request_as(
                USER_1_ID,
                "POST",
                f"/api/chat/messages/{root['id']}/thread",
                json={"body": f"Newer reply {index + 1}"},
            )
        ).json()
        replies.append(reply)

    response = await request_as(USER_2_ID, "GET", "/api/chat/unreads", params={"limit": 2})
    assert response.status_code == 200

    payload = response.json()
    assert len(payload) == 2
    thread_item = next(item for item in payload if item["kind"] == "thread")
    dm_item = next(item for item in payload if item["kind"] == "dm")
    assert thread_item["root_message"]["id"] == root["id"]
    assert thread_item["unread_count"] == 7
    assert [message["id"] for message in thread_item["unread_messages"]] == [
        root["id"],
        *[reply["id"] for reply in replies[:4]],
    ]
    assert dm_item["conversation"]["id"] == dm["id"]
    assert dm_item["unread_count"] == 1
    assert [message["id"] for message in dm_item["unread_messages"]] == [dm_message["id"]]


async def test_room_search_returns_root_matches_and_thread_replies(
    request_as: Callable[..., object],
):
    room = (await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")).json()["room"]
    matching_root = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{room['id']}/messages",
            json={"body": "Search indexing needs a quick pass"},
        )
    ).json()
    thread_root = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{room['id']}/messages",
            json={"body": "Thread root without the keyword"},
        )
    ).json()
    matching_reply = (
        await request_as(
            USER_2_ID,
            "POST",
            f"/api/chat/messages/{thread_root['id']}/thread",
            json={"body": "The search indexing conversation belongs in here"},
        )
    ).json()
    dm = (await request_as(USER_1_ID, "POST", "/api/chat/dms", json={"user_id": USER_2_ID})).json()
    await request_as(
        USER_1_ID,
        "POST",
        f"/api/chat/conversations/{dm['id']}/messages",
        json={"body": "Search should not include DM messages yet"},
    )

    response = await request_as(USER_1_ID, "GET", "/api/chat/search", params={"query": "search"})
    assert response.status_code == 200

    payload = response.json()
    assert [item["message"]["id"] for item in payload] == [
        matching_reply["id"],
        matching_root["id"],
    ]
    assert payload[0]["root_message"]["id"] == thread_root["id"]
    assert payload[1]["root_message"]["id"] == matching_root["id"]


async def test_attachment_only_message_is_allowed(
    request_as: Callable[..., object],
):
    room = (await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")).json()["room"]
    response = await request_as(
        USER_1_ID,
        "POST",
        f"/api/chat/conversations/{room['id']}/messages",
        json={
            "body": "",
            "attachments": [
                {
                    "url": "/static/uploads/demo.png",
                    "filename": "demo.png",
                    "type": "image/png",
                    "size": 1234,
                }
            ],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["body"] == ""
    assert payload["attachments"][0]["filename"] == "demo.png"


async def test_dm_unread_read_cursor_and_notifications_flow(
    chat_db_session: AsyncSession,
    request_as: Callable[..., object],
):
    dm = (await request_as(USER_1_ID, "POST", "/api/chat/dms", json={"user_id": USER_2_ID})).json()
    sent = await request_as(
        USER_1_ID,
        "POST",
        f"/api/chat/conversations/{dm['id']}/messages",
        json={"body": "Hello from Alex"},
    )
    assert sent.status_code == 200
    sent_payload = sent.json()

    sender_read_result = await chat_db_session.scalars(
        select(ChatConversationRead).where(
            ChatConversationRead.conversation_id == dm["id"],
            ChatConversationRead.user_id == USER_1_ID,
        )
    )
    sender_read = sender_read_result.one()
    assert sender_read.last_read_conversation_seq == sent_payload["conversation_seq"]
    assert sender_read.last_read_message_id == sent_payload["id"]

    sender_bootstrap = (await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")).json()
    assert sender_bootstrap["unread_summary"]["dms"] == 0

    recipient_bootstrap = (await request_as(USER_2_ID, "GET", "/api/chat/bootstrap")).json()
    assert recipient_bootstrap["unread_summary"]["dms"] == 1
    assert recipient_bootstrap["dms"][0]["unread_count"] == 1

    notifications = await request_as(USER_2_ID, "GET", "/api/chat/notifications")
    assert notifications.status_code == 200
    assert notifications.json() == [
        {
            "id": notifications.json()[0]["id"],
            "type": "dm_message",
            "conversation_id": dm["id"],
            "message_id": sent_payload["id"],
            "actor_user_id": USER_1_ID,
            "actor_name": "Alex Example",
            "actor_color": "#111111",
            "metadata": None,
            "created_at": notifications.json()[0]["created_at"],
            "read_at": None,
        }
    ]

    read = await request_as(
        USER_2_ID,
        "POST",
        f"/api/chat/conversations/{dm['id']}/read",
        json={
            "last_read_conversation_seq": sent_payload["conversation_seq"],
            "last_read_message_id": sent_payload["id"],
        },
    )
    assert read.status_code == 200
    assert read.json() == {"room": 0, "dms": 0, "total": 0}

    recipient_read_result = await chat_db_session.scalars(
        select(ChatConversationRead).where(
            ChatConversationRead.conversation_id == dm["id"],
            ChatConversationRead.user_id == USER_2_ID,
        )
    )
    recipient_read = recipient_read_result.one()
    assert recipient_read.last_read_conversation_seq == sent_payload["conversation_seq"]
    assert recipient_read.last_read_message_id == sent_payload["id"]

    refreshed_bootstrap = (await request_as(USER_2_ID, "GET", "/api/chat/bootstrap")).json()
    assert refreshed_bootstrap["unread_summary"]["dms"] == 0
    assert refreshed_bootstrap["dms"][0]["unread_count"] == 0


async def test_room_messages_notify_other_team_members(
    chat_db_session: AsyncSession,
    request_as: Callable[..., object],
):
    room = (await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")).json()["room"]
    sent = await request_as(
        USER_1_ID,
        "POST",
        f"/api/chat/conversations/{room['id']}/messages",
        json={"body": "Team update for everyone"},
    )
    assert sent.status_code == 200
    sent_payload = sent.json()

    sender_notifications = await request_as(USER_1_ID, "GET", "/api/chat/notifications")
    assert sender_notifications.status_code == 200
    assert sender_notifications.json() == []

    recipient_notifications = await request_as(USER_2_ID, "GET", "/api/chat/notifications")
    assert recipient_notifications.status_code == 200
    assert recipient_notifications.json() == [
        {
            "id": recipient_notifications.json()[0]["id"],
            "type": "room_message",
            "conversation_id": room["id"],
            "message_id": sent_payload["id"],
            "actor_user_id": USER_1_ID,
            "actor_name": "Alex Example",
            "actor_color": "#111111",
            "metadata": None,
            "created_at": recipient_notifications.json()[0]["created_at"],
            "read_at": None,
        }
    ]

    unified_notification_result = await chat_db_session.scalars(
        select(NotificationEvent).where(NotificationEvent.user_id == USER_2_ID)
    )
    unified_notification = unified_notification_result.one()
    assert unified_notification.kind == "chat.room_message"
    assert unified_notification.title == "Alex Example posted in team chat"
    assert unified_notification.conversation_id == room["id"]

    recipient_bootstrap = (await request_as(USER_2_ID, "GET", "/api/chat/bootstrap")).json()
    assert recipient_bootstrap["unread_summary"]["room"] == 1


async def test_mentions_create_notifications_and_illo_routes_room_run(
    chat_db_session: AsyncSession,
    request_as: Callable[..., object],
):
    room = (await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")).json()["room"]
    posted = await request_as(
        USER_1_ID,
        "POST",
        f"/api/chat/conversations/{room['id']}/messages",
        json={"body": "Ping @redam and @illo about this thread"},
    )
    assert posted.status_code == 200
    payload = posted.json()
    assert payload["metadata"] == {
        "illo_invoked": True,
        "mentioned_user_ids": [USER_2_ID],
    }

    mention_rows_result = await chat_db_session.scalars(select(ChatMessageMention))
    mention_rows = mention_rows_result.all()
    assert len(mention_rows) == 1
    assert str(mention_rows[0].mentioned_user_id) == USER_2_ID

    notifications = await request_as(USER_2_ID, "GET", "/api/chat/notifications")
    assert notifications.status_code == 200
    assert notifications.json()[0]["type"] == "mention"
    assert notifications.json()[0]["actor_user_id"] == USER_1_ID

    message_count = await chat_db_session.scalar(select(func.count()).select_from(ChatMessage))
    notification_count = await chat_db_session.scalar(select(func.count()).select_from(ChatNotification))
    assert message_count == 1
    assert notification_count == 1

    run_result = await chat_db_session.scalars(select(AgentRunRow))
    run = run_result.one()
    assert run.thread_id == f"chat:{room['id']}:{payload['id']}"
    assert run.user_id == USER_1_ID
    assert run.org_id == ORG_ID
    assert run.target_ref["kind"] == "chat_message"
    assert run.metadata_["chat_trigger"]["message_id"] == payload["id"]
    assert run.metadata_["illo_trigger"]["event_type"] == "chat.room_message_mention"


async def test_dm_illo_mention_does_not_route_run(
    chat_db_session: AsyncSession,
    request_as: Callable[..., object],
):
    dm = (await request_as(USER_1_ID, "POST", "/api/chat/dms", json={"user_id": USER_2_ID})).json()
    posted = await request_as(
        USER_1_ID,
        "POST",
        f"/api/chat/conversations/{dm['id']}/messages",
        json={"body": "@illo this stays private"},
    )

    assert posted.status_code == 200
    assert posted.json()["metadata"] == {"illo_invoked": True}
    assert await chat_db_session.scalar(select(func.count()).select_from(AgentRunRow)) == 0


async def test_thread_illo_mention_routes_thread_target(
    chat_db_session: AsyncSession,
    request_as: Callable[..., object],
):
    room = (await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")).json()["room"]
    root = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{room['id']}/messages",
            json={"body": "Root topic"},
        )
    ).json()
    reply = await request_as(
        USER_2_ID,
        "POST",
        f"/api/chat/messages/{root['id']}/thread",
        json={"body": "@illo please handle this thread"},
    )

    assert reply.status_code == 200
    run_result = await chat_db_session.scalars(select(AgentRunRow))
    run = run_result.one()
    assert run.thread_id == f"chat:{room['id']}:{root['id']}"
    assert run.metadata_["chat_trigger"]["message_id"] == reply.json()["id"]
    assert run.metadata_["chat_trigger"]["thread_root_message_id"] == root["id"]
    assert run.metadata_["chat_trigger"]["response_target"] == {
        "conversation_id": room["id"],
        "thread_root_message_id": root["id"],
    }


async def test_agent_message_posts_to_room_and_rejects_dm(
    chat_db_session: AsyncSession,
    request_as: Callable[..., object],
):
    from fastapi import HTTPException

    from brain.app.api.services.chat import ChatService

    room = (await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")).json()["room"]
    service = ChatService(chat_db_session, await _user_context(chat_db_session, USER_1_ID))
    message, publish = await service.post_agent_message(
        conversation_id=room["id"],
        body="I created a Cortex thought for this.",
    )
    await chat_db_session.commit()

    assert message.sender_kind == "agent"
    assert message.sender_user_id is None
    assert message.sender_name == "Illo"
    assert publish.conversation_id == room["id"]

    dm = (await request_as(USER_1_ID, "POST", "/api/chat/dms", json={"user_id": USER_2_ID})).json()
    with pytest.raises(HTTPException, match="team room"):
        await ChatService(
            chat_db_session,
            await _user_context(chat_db_session, USER_1_ID),
        ).post_agent_message(
            conversation_id=dm["id"],
            body="Nope",
        )


async def test_read_with_message_id_only_preserves_partial_unread(
    chat_db_session: AsyncSession,
    request_as: Callable[..., object],
):
    dm = (await request_as(USER_1_ID, "POST", "/api/chat/dms", json={"user_id": USER_2_ID})).json()
    first = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{dm['id']}/messages",
            json={"body": "First DM"},
        )
    ).json()
    second = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{dm['id']}/messages",
            json={"body": "Second DM"},
        )
    ).json()

    read = await request_as(
        USER_2_ID,
        "POST",
        f"/api/chat/conversations/{dm['id']}/read",
        json={"last_read_message_id": first["id"]},
    )
    assert read.status_code == 200
    assert read.json() == {"room": 0, "dms": 1, "total": 1}

    recipient_read_result = await chat_db_session.scalars(
        select(ChatConversationRead).where(
            ChatConversationRead.conversation_id == dm["id"],
            ChatConversationRead.user_id == USER_2_ID,
        )
    )
    recipient_read = recipient_read_result.one()
    assert recipient_read.last_read_message_id == first["id"]
    assert recipient_read.last_read_conversation_seq == first["conversation_seq"]

    refreshed_bootstrap = (await request_as(USER_2_ID, "GET", "/api/chat/bootstrap")).json()
    assert refreshed_bootstrap["dms"][0]["unread_count"] == 1
    assert refreshed_bootstrap["unread_summary"]["dms"] == 1
    assert second["conversation_seq"] > first["conversation_seq"]


async def test_partial_chat_read_syncs_legacy_rows_but_keeps_unified_conversation_notification_open(
    chat_db_session: AsyncSession,
    request_as: Callable[..., object],
):
    dm = (await request_as(USER_1_ID, "POST", "/api/chat/dms", json={"user_id": USER_2_ID})).json()
    first = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{dm['id']}/messages",
            json={"body": "First unread DM"},
        )
    ).json()
    second = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{dm['id']}/messages",
            json={"body": "Second unread DM"},
        )
    ).json()

    unread_unified_result = await chat_db_session.scalars(
        select(NotificationEvent).where(
            NotificationEvent.user_id == USER_2_ID,
            NotificationEvent.conversation_id == dm["id"],
            NotificationEvent.read_at.is_(None),
        )
    )
    unread_unified = unread_unified_result.all()
    assert len(unread_unified) == 1

    read = await request_as(
        USER_2_ID,
        "POST",
        f"/api/chat/conversations/{dm['id']}/read",
        json={"last_read_message_id": first["id"]},
    )
    assert read.status_code == 200

    first_legacy_result = await chat_db_session.scalars(
        select(ChatNotification).where(
            ChatNotification.user_id == USER_2_ID,
            ChatNotification.message_id == first["id"],
        )
    )
    first_legacy = first_legacy_result.one()
    second_legacy_result = await chat_db_session.scalars(
        select(ChatNotification).where(
            ChatNotification.user_id == USER_2_ID,
            ChatNotification.message_id == second["id"],
        )
    )
    second_legacy = second_legacy_result.one()
    assert first_legacy.read_at is not None
    assert second_legacy.read_at is None

    unified_after_partial_result = await chat_db_session.scalars(
        select(NotificationEvent).where(
            NotificationEvent.user_id == USER_2_ID,
            NotificationEvent.conversation_id == dm["id"],
        )
    )
    unified_after_partial = unified_after_partial_result.one()
    assert unified_after_partial.read_at is None

    finished = await request_as(
        USER_2_ID,
        "POST",
        f"/api/chat/conversations/{dm['id']}/read",
        json={
            "last_read_conversation_seq": second["conversation_seq"],
            "last_read_message_id": second["id"],
        },
    )
    assert finished.status_code == 200

    await chat_db_session.refresh(unified_after_partial)
    assert unified_after_partial.read_at is not None


async def test_dm_notifications_coalesce_into_one_unified_inbox_row(
    chat_db_session: AsyncSession,
    request_as: Callable[..., object],
):
    dm = (await request_as(USER_1_ID, "POST", "/api/chat/dms", json={"user_id": USER_2_ID})).json()
    first = await request_as(
        USER_1_ID,
        "POST",
        f"/api/chat/conversations/{dm['id']}/messages",
        json={"body": "First unread DM"},
    )
    second = await request_as(
        USER_1_ID,
        "POST",
        f"/api/chat/conversations/{dm['id']}/messages",
        json={"body": "Second unread DM"},
    )

    assert first.status_code == 200
    assert second.status_code == 200

    legacy_rows_result = await chat_db_session.scalars(
        select(ChatNotification).where(
            ChatNotification.user_id == USER_2_ID,
            ChatNotification.conversation_id == dm["id"],
        )
    )
    legacy_rows = legacy_rows_result.all()
    assert len(legacy_rows) == 2

    unified_rows_result = await chat_db_session.scalars(
        select(NotificationEvent).where(
            NotificationEvent.user_id == USER_2_ID,
            NotificationEvent.conversation_id == dm["id"],
            NotificationEvent.read_at.is_(None),
        )
    )
    unified_rows = unified_rows_result.all()
    assert len(unified_rows) == 1
    assert unified_rows[0].occurrence_count == 2
    assert unified_rows[0].body == "Second unread DM"


async def test_invalid_thread_reply_target_returns_400(
    request_as: Callable[..., object],
):
    room = (await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")).json()["room"]
    root_one = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{room['id']}/messages",
            json={"body": "Root one"},
        )
    ).json()
    root_two = (
        await request_as(
            USER_1_ID,
            "POST",
            f"/api/chat/conversations/{room['id']}/messages",
            json={"body": "Root two"},
        )
    ).json()

    response = await request_as(
        USER_2_ID,
        "POST",
        f"/api/chat/messages/{root_one['id']}/thread",
        json={
            "body": "This should fail",
            "reply_to_message_id": root_two["id"],
        },
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Reply target not found in thread"}


async def test_chat_service_404_preserves_detail(
    request_as: Callable[..., object],
):
    response = await request_as(
        USER_1_ID,
        "POST",
        "/api/chat/dms",
        json={"user_id": USER_3_ID},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "User not found"}


async def test_message_publish_waits_for_commit(
    chat_db_session: AsyncSession,
    request_as: Callable[..., object],
    monkeypatch: pytest.MonkeyPatch,
):
    from brain.app.api.routers import chat as chat_router

    room = (await request_as(USER_1_ID, "GET", "/api/chat/bootstrap")).json()["room"]
    publish_mock = AsyncMock()
    monkeypatch.setattr(chat_router, "_publish_message_events", publish_mock)

    original_commit = chat_db_session.commit
    commit_calls = {"count": 0}

    async def failing_commit():
        commit_calls["count"] += 1
        if commit_calls["count"] == 1:
            raise RuntimeError("boom")
        return await original_commit()

    monkeypatch.setattr(chat_db_session, "commit", failing_commit)

    async def override_db() -> AsyncIterator[AsyncSession]:
        try:
            yield chat_db_session
            await chat_db_session.commit()
        except Exception:
            await chat_db_session.rollback()
            raise

    async def override_user() -> dict[str, str]:
        return await _user_context(chat_db_session, USER_1_ID)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[rate_limit] = lambda: None
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/chat/conversations/{room['id']}/messages",
                json={"body": "This should not publish"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert publish_mock.await_count == 0
