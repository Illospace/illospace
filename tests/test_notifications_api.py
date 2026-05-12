"""Focused route verification for unified notifications."""
from __future__ import annotations

import uuid
from collections.abc import Callable, Generator

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from brain.kernel import config
from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.main import app
from brain.platform.db.models.chat import (
    ChatConversation,
    ChatConversationMember,
    ChatMessage,
    ChatConversationRead,
)
from brain.platform.db.models.idea import Idea, IdeaThread, UserMention
from brain.platform.db.models.notification import (
    NOTIFICATION_KIND_WORKSPACE_MENTION,
    NOTIFICATION_SOURCE_WORKSPACE,
    NotificationEvent,
)
from brain.platform.db.models.org import Org, User

ORG_ID = "00000000-0000-4000-8000-000000000001"
USER_1_ID = "00000000-0000-4000-8000-000000000101"
USER_2_ID = "00000000-0000-4000-8000-000000000102"
IDEA_ID = "00000000-0000-4000-8000-000000000201"

_TABLES = (
    Org.__table__,
    User.__table__,
    Idea.__table__,
    IdeaThread.__table__,
    UserMention.__table__,
    ChatConversation.__table__,
    ChatConversationMember.__table__,
    ChatMessage.__table__,
    ChatConversationRead.__table__,
    NotificationEvent.__table__,
)


def _schema_name() -> str:
    return f"notification_test_{uuid.uuid4().hex[:8]}"


def _seed_users(session: Session, schema: str) -> None:
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
        ]
    )
    session.commit()


def _user_context(session: Session, user_id: str) -> dict[str, str]:
    user = session.get(User, user_id)
    assert user is not None
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "color": user.color,
        "org_id": user.org_id,
    }


def _seed_workspace_notification(session: Session) -> int:
    session.add(
        Idea(
            id=IDEA_ID,
            title="Notifications hardening",
            status="needs_input",
            user_id=USER_2_ID,
            org_id=ORG_ID,
        )
    )
    session.add(
        IdeaThread(
            id=41,
            idea_id=IDEA_ID,
            role="user",
            content="Ping @redam about notifications",
            user_id=USER_1_ID,
        )
    )
    session.flush()
    session.add(
        UserMention(
            user_id=USER_2_ID,
            idea_id=IDEA_ID,
            mentioned_by=USER_1_ID,
            thread_message_id=41,
        )
    )
    notification = NotificationEvent(
        org_id=ORG_ID,
        user_id=USER_2_ID,
        source=NOTIFICATION_SOURCE_WORKSPACE,
        kind=NOTIFICATION_KIND_WORKSPACE_MENTION,
        actor_user_id=USER_1_ID,
        idea_id=IDEA_ID,
        title="Alex mentioned you in workspace",
        body="Check the latest thread update",
        coalesce_key=f"workspace:mention:{USER_2_ID}:{IDEA_ID}:41",
        payload={"thread_message_id": 41},
    )
    session.add(notification)
    session.commit()
    return notification.id


def _build_request_as(session: Session) -> Callable[..., object]:
    def override_db() -> Generator[Session, None, None]:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    def _request(user_id: str, method: str, path: str, **kwargs):
        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: _user_context(session, user_id)
        app.dependency_overrides[rate_limit] = lambda: None
        try:
            with TestClient(app) as client:
                return client.request(method, path, **kwargs)
        finally:
            app.dependency_overrides.clear()

    return _request


def test_openapi_registers_notification_routes():
    with TestClient(app) as client:
        response = client.get("/api/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/notifications/summary" in paths
    assert "/api/notifications/preferences" in paths
    assert "/api/notifications" in paths
    assert "/api/notifications/{notification_id}/read" in paths
    assert "/api/notifications/read-all" in paths


def test_notification_preferences_are_persisted():
    schema = _schema_name()
    engine = create_engine(config.DB_SYNC_URL)
    admin_conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    admin_conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    conn = engine.connect()
    conn.execute(text(f'SET search_path TO "{schema}", public'))

    try:
        for table in _TABLES:
            table.create(bind=conn)

        session = sessionmaker(bind=conn, expire_on_commit=False)()
        session.execute(text(f'SET search_path TO "{schema}", public'))
        _seed_users(session, schema)

        request_as = _build_request_as(session)
        response = request_as(USER_2_ID, "GET", "/api/notifications/preferences")
        assert response.status_code == 200
        assert response.json() == {
            "sound_enabled": True,
            "message_notifications_enabled": True,
        }

        response = request_as(
            USER_2_ID,
            "PATCH",
            "/api/notifications/preferences",
            json={"sound_enabled": False, "message_notifications_enabled": False},
        )
        assert response.status_code == 200
        assert response.json() == {
            "sound_enabled": False,
            "message_notifications_enabled": False,
        }

        session.expire_all()
        user = session.get(User, USER_2_ID)
        assert user is not None
        assert user.notification_sound_enabled is False
        assert user.message_notifications_enabled is False
    finally:
        try:
            session.close()
        except Exception:
            pass
        conn.close()
        admin_conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_conn.close()
        engine.dispose()


def test_workspace_notification_read_marks_user_mentions_seen():
    schema = _schema_name()
    engine = create_engine(config.DB_SYNC_URL)
    admin_conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    admin_conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    conn = engine.connect()
    conn.execute(text(f'SET search_path TO "{schema}", public'))

    try:
        for table in _TABLES:
            table.create(bind=conn)

        session = sessionmaker(bind=conn, expire_on_commit=False)()
        session.execute(text(f'SET search_path TO "{schema}", public'))
        _seed_users(session, schema)
        notification_id = _seed_workspace_notification(session)

        request_as = _build_request_as(session)
        response = request_as(USER_2_ID, "POST", f"/api/notifications/{notification_id}/read")
        assert response.status_code == 200
        assert response.json()["unread_notification_total"] == 0

        notification = session.get(NotificationEvent, notification_id)
        assert notification is not None
        assert notification.read_at is not None

        mention_row = session.execute(
            text(
                """
                SELECT seen_at
                FROM user_mentions
                WHERE user_id = :user_id AND idea_id = :idea_id AND thread_message_id = :thread_message_id
                """
            ),
            {
                "user_id": USER_2_ID,
                "idea_id": IDEA_ID,
                "thread_message_id": 41,
            },
        ).mappings().first()
        assert mention_row is not None
        assert mention_row["seen_at"] is not None
    finally:
        try:
            session.close()
        except Exception:
            pass
        conn.close()
        admin_conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_conn.close()
        engine.dispose()
