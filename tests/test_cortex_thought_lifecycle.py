from __future__ import annotations

import ast
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.asyncio


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushed = False

    def add(self, obj: object) -> None:
        if getattr(obj, "id", None) is None and obj.__class__.__name__ == "IdeaThread":
            obj.id = 41
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed = True


class _MentionRepo:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_if_missing(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id=99, **kwargs), True


class _NotificationRepo:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def create_or_coalesce(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id=len(self.created), **kwargs)


def _idea(**overrides):
    defaults = {
        "id": "idea-1",
        "title": "Launch work",
        "status": "emerged",
        "updated_at": datetime(2026, 5, 15, 11, 0, tzinfo=timezone.utc),
        "org_id": "org-1",
        "user_id": "owner-1",
        "agent_details": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _actor(**overrides):
    defaults = {
        "user_id": "user-1",
        "org_id": "org-1",
        "name": "Alex",
        "color": "#8DB7FF",
    }
    defaults.update(overrides)
    return defaults


async def test_user_thread_message_activates_thought_and_returns_thread_event():
    from brain.systems.cortex.thought_lifecycle import (
        ThreadMessageCommand,
        post_thread_message,
    )

    session = _Session()
    idea = _idea(status="emerged")

    result = await post_thread_message(
        session,
        idea=idea,
        command=ThreadMessageCommand(
            idea_id="idea-1",
            role="user",
            content="Please investigate this.",
            actor=_actor(),
        ),
    )

    assert idea.status == "active"
    assert session.flushed is True
    assert result.message_payload == {
        "id": 41,
        "idea_id": "idea-1",
        "role": "user",
        "content": "Please investigate this.",
        "attachments": [],
        "metadata": None,
        "user_id": "user-1",
        "message_type": "message",
        "created_at": "2026-05-15T12:00:00+00:00",
        "user_name": "Alex",
        "user_color": "#8DB7FF",
    }
    assert result.status_change == {
        "idea_id": "idea-1",
        "old_status": "emerged",
        "new_status": "active",
        "org_id": "org-1",
    }
    assert [
        (log.from_state, log.to_state, log.trigger)
        for log in session.added
        if log.__class__.__name__ == "IdeaStateLog"
    ] == [("emerged", "active", "auto_user_message")]


async def test_assistant_thread_message_marks_unread_and_notifies_owner():
    from brain.systems.cortex.thought_lifecycle import (
        ThreadMessageCommand,
        post_thread_message,
    )

    session = _Session()
    notifications = _NotificationRepo()
    idea = _idea(status="working", user_id="owner-1", title="Research thread")

    result = await post_thread_message(
        session,
        idea=idea,
        command=ThreadMessageCommand(
            idea_id="idea-1",
            role="illo",
            content="I found the root cause.",
            actor=_actor(user_id=None, name=None, color=None),
        ),
        notification_repo=notifications,
    )

    assert idea.status == "unread_reply"
    assert result.notification_user_ids == {"owner-1"}
    assert notifications.created[0]["user_id"] == "owner-1"
    assert notifications.created[0]["kind"] == "workspace.thread_attention"
    assert notifications.created[0]["idea_id"] == "idea-1"
    assert "I found the root cause." in notifications.created[0]["payload"]["preview"]


async def test_user_thread_message_records_teammate_mentions_and_notifications():
    from brain.systems.cortex.thought_lifecycle import (
        ThreadMessageCommand,
        post_thread_message,
    )

    session = _Session()
    mentions = _MentionRepo()
    notifications = _NotificationRepo()
    published: list[tuple[str, dict]] = []

    async def resolve_mentions(names, org_id):
        assert names == ["riley"]
        assert org_id == "org-1"
        return {"riley": "user-2"}

    result = await post_thread_message(
        session,
        idea=_idea(status="active"),
        command=ThreadMessageCommand(
            idea_id="idea-1",
            role="user",
            content="@Riley can you take a look?",
            actor=_actor(user_id="user-1", name="Alex"),
        ),
        mention_repo=mentions,
        notification_repo=notifications,
        resolve_mentioned_users=resolve_mentions,
        publish=lambda event_type, payload: published.append((event_type, payload)),
    )

    assert mentions.created == [
        {
            "user_id": "user-2",
            "idea_id": "idea-1",
            "mentioned_by": "user-1",
            "thread_message_id": 41,
        }
    ]
    assert result.notification_user_ids == {"user-2"}
    assert notifications.created[0]["kind"] == "workspace.mention"
    assert notifications.created[0]["actor_user_id"] == "user-1"
    assert published == [
        (
            "mention",
            {
                "idea_id": "idea-1",
                "user_id": "user-2",
                "mentioned_by": {
                    "user_id": "user-1",
                    "name": "Alex",
                    "color": "#8DB7FF",
                },
            },
        )
    ]


async def test_thread_message_promotes_project_context_and_attachment_context():
    from brain.systems.cortex.thought_lifecycle import (
        ThreadMessageCommand,
        post_thread_message,
    )

    project_context = {
        "selected_profile_name": "Launch repo",
        "resources": [{"kind": "folder", "path": "/workspace/launch"}],
    }
    thread_attachment_context = {
        "attachment_count": 1,
        "items": [{"kind": "text", "name": "brief.md", "text": "Brief"}],
    }
    session = _Session()
    idea = _idea(agent_details={"existing": True})

    result = await post_thread_message(
        session,
        idea=idea,
        command=ThreadMessageCommand(
            idea_id="idea-1",
            role="user",
            content="Use this context.",
            actor=_actor(),
            attachments=[{"type": "project_context", "project_context": project_context}],
            metadata={"thread_attachment_context": thread_attachment_context},
        ),
        validate_project_context=lambda value: value,
        build_attachment_context=lambda attachments: thread_attachment_context,
    )

    assert idea.agent_details == {"existing": True, "project_context": project_context}
    assert result.message_payload["metadata"]["project_context"] == project_context
    assert result.message_payload["metadata"]["thread_attachment_context"] == thread_attachment_context


async def test_legacy_thread_lifecycle_is_removed_from_api_route():
    tree = ast.parse(open("brain/app/api/routers/cortex/_idea_ops.py", encoding="utf-8").read())
    route = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "add_thread_message_raw"
    )

    called_names: set[str] = set()
    constructed_names: set[str] = set()
    for node in ast.walk(route):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called_names.add(node.func.id)
            constructed_names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called_names.add(node.func.attr)

    assert "post_thread_message" in called_names
    assert "IdeaThread" not in constructed_names
    assert "IdeaStateLog" not in constructed_names
