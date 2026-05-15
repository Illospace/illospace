"""Tests for cortex router — ideas CRUD, threads."""
import json
import inspect
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


class _AsyncSession:
    def __init__(self, sync_session):
        self.sync_session = sync_session

    def add(self, *args, **kwargs):
        return self.sync_session.add(*args, **kwargs)

    async def get(self, *args, **kwargs):
        return self.sync_session.get(*args, **kwargs)

    async def refresh(self, *args, **kwargs):
        return self.sync_session.refresh(*args, **kwargs)

    async def scalar(self, *args, **kwargs):
        return self.sync_session.scalar(*args, **kwargs)

    async def scalars(self, *args, **kwargs):
        return self.sync_session.scalars(*args, **kwargs)

    async def execute(self, *args, **kwargs):
        return self.sync_session.execute(*args, **kwargs)

    async def run_sync(self, fn):
        return fn(self.sync_session)

    async def flush(self):
        return self.sync_session.flush()

    async def commit(self):
        return self.sync_session.commit()

    async def rollback(self):
        return self.sync_session.rollback()

    async def close(self):
        return None


@pytest.fixture(autouse=True)
def mock_session_factory():
    """Mock the DB SessionFactory so no real database is needed."""
    session = MagicMock()

    def _async_factory():
        return _AsyncSession(session)

    with patch("brain.app.api.deps.SessionFactory", _async_factory):
        yield session


def _make_idea(**overrides):
    """Create a fake Idea-like object with from_attributes support."""
    defaults = {
        "id": str(uuid.uuid4()),
        "title": "Test Idea",
        "description": None,
        "status": "emerged",
        "origin": "user_created",
        "origin_ref": None,
        "salience_score": 5.0,
        "position_x": None,
        "position_y": None,
        "position_sticky": False,
        "parent_id": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "archived_at": None,
        "user_id": "system",
        "org_id": None,
        "author_name": None,
        "author_color": None,
        "orbit_anchor_type": None,
        "orbit_anchor_id": None,
        "agent_details": None,
    }
    defaults.update(overrides)
    obj = MagicMock()
    for k, v in defaults.items():
        setattr(obj, k, v)
    return obj


def _make_thread_msg(**overrides):
    defaults = {
        "id": 1,
        "idea_id": "test-idea-id",
        "role": "user",
        "content": "Hello",
        "user_id": "system",
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    obj = MagicMock()
    for k, v in defaults.items():
        setattr(obj, k, v)
    return obj


def _assign_thread_defaults_on_add(session):
    next_thread_id = 1

    def add(obj):
        nonlocal next_thread_id
        if obj.__class__.__name__ == "IdeaThread":
            obj.id = next_thread_id
            next_thread_id += 1
            if getattr(obj, "created_at", None) is None:
                obj.created_at = datetime.now(timezone.utc)
            if getattr(obj, "attachments", None) is None:
                obj.attachments = []
            if getattr(obj, "message_type", None) is None:
                obj.message_type = "message"

    session.add.side_effect = add


def test_project_context_extraction_from_thread_payload():
    from brain.app.api.routers.cortex._idea_ops import _extract_project_context_from_message

    snapshot = {
        "selected_profile_name": "Cortex UI",
        "validation_status": "client_validated",
        "resources": [{"type": "folder", "path": "frontend/src/lib/components/cortex"}],
    }

    assert _extract_project_context_from_message(
        [{"type": "project_context", "project_context": snapshot}],
        None,
    ) == snapshot
    assert _extract_project_context_from_message([], {"project_context": snapshot}) == snapshot


def test_thread_project_context_validation_rejects_empty_context():
    from fastapi import HTTPException

    from brain.app.api.routers.cortex._idea_ops import _validate_thread_project_context

    with pytest.raises(HTTPException) as excinfo:
        _validate_thread_project_context({"name": "Legacy empty project", "resources": []})

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["validation_errors"] == [
        "project_context_snapshot.resources must contain at least one resource."
    ]


def test_project_context_extraction_promotes_readable_thread_upload(tmp_path, monkeypatch):
    from brain.app.api.routers.cortex import _idea_ops
    from brain.systems.cortex import thread_attachments

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    uploaded = upload_dir / "note.md"
    uploaded.write_text("# Notes\n\nUseful context.", encoding="utf-8")
    monkeypatch.setattr(thread_attachments, "UPLOAD_DIR", upload_dir)

    result = _idea_ops._extract_project_context_from_message(
        [
            {
                "url": "/static/uploads/note.md",
                "filename": "Example YC Application W26.md",
                "type": "text/markdown",
                "size": uploaded.stat().st_size,
            }
        ],
        None,
    )

    assert result["source"] == "cortex-thread-attachments"
    assert result["resources"][0]["kind"] == "file"
    assert result["resources"][0]["name"] == "Example YC Application W26.md"
    assert result["resources"][0]["path"] == str(uploaded.resolve())


def test_project_context_extraction_merges_project_and_readable_upload(tmp_path, monkeypatch):
    from brain.app.api.routers.cortex import _idea_ops
    from brain.systems.cortex import thread_attachments

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    uploaded = upload_dir / "brief.md"
    uploaded.write_text("brief", encoding="utf-8")
    monkeypatch.setattr(thread_attachments, "UPLOAD_DIR", upload_dir)
    project = {
        "selected_profile_name": "YC Application",
        "resources": [{"type": "folder", "path": "/workspace/app"}],
    }

    result = _idea_ops._extract_project_context_from_message(
        [{"url": "/static/uploads/brief.md", "filename": "brief.md", "type": "text/markdown"}],
        {"project_context": project},
    )

    assert result["selected_profile_name"] == "YC Application"
    assert [resource.get("kind") or resource.get("type") for resource in result["resources"]] == ["folder", "file"]


def test_thread_attachment_context_promotes_text_and_image(tmp_path, monkeypatch):
    from brain.systems.cortex import thread_attachments

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    note = upload_dir / "note.md"
    note.write_text("hello attachment", encoding="utf-8")
    image = upload_dir / "screenshot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    monkeypatch.setattr(thread_attachments, "UPLOAD_DIR", upload_dir)

    context = thread_attachments.build_thread_attachment_context([
        {"url": "/static/uploads/note.md", "filename": "note.md", "type": "text/markdown"},
        {"url": "/static/uploads/screenshot.png", "filename": "screenshot.png", "type": "image/png"},
    ])
    blocks = thread_attachments.initial_user_content_blocks("Read these", context)

    assert context["attachment_count"] == 2
    assert context["items"][0]["text"] == "hello attachment"
    assert blocks[0]["type"] == "text"
    assert "hello attachment" in blocks[0]["text"]
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["media_type"] == "image/png"


def test_project_context_merge_into_idea_agent_details():
    from brain.app.api.routers.cortex._idea_ops import _merge_project_context_into_idea

    idea = _make_idea(agent_details={"existing": True})
    snapshot = {"validation_status": "client_validated", "resources": [{"path": "brain"}]}

    _merge_project_context_into_idea(idea, snapshot)

    assert idea.agent_details["existing"] is True
    assert idea.agent_details["project_context"] == snapshot


def test_project_context_merge_drops_non_dict_agent_details():
    from brain.app.api.routers.cortex._idea_ops import _merge_project_context_into_idea

    idea = _make_idea(agent_details=[{"old": True}])
    snapshot = {"validation_status": "client_validated", "resources": [{"path": "brain"}]}

    _merge_project_context_into_idea(idea, snapshot)

    assert idea.agent_details == {"project_context": snapshot}


async def test_notify_metadata_preserves_thread_project_context():
    from brain.app.api.routers.cortex._ideas import _effective_notify_metadata

    snapshot = {"resources": [{"path": "/workspace/context.md"}]}
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = {
        "project_context": snapshot,
        "execution_profile": "deep",
    }

    async_db = _AsyncSession(db)
    result = await _effective_notify_metadata(async_db, "idea-1", {"execution_profile": "fast"})

    assert result["project_context"] == snapshot
    assert result["execution_profile"] == "fast"


async def test_project_profile_resource_endpoints_mutate_context(tmp_path):
    from brain.app.api.routers.cortex import _project_context
    from brain.systems.cortex.project_context.schemas import (
        ProjectResourcesCreate,
        ProjectResourcesReorder,
    )

    first = tmp_path / "a.md"
    second = tmp_path / "b.md"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    profile = SimpleNamespace(
        id="project-1",
        org_id="test-org",
        user_id="user-1",
        slug="yc",
        name="YC Application",
        description=None,
        project_context={"resources": [{"id": "r1", "kind": "file", "path": str(first)}]},
        default_environment_binding_id=None,
        active=True,
        metadata_={},
        created_at=None,
    )
    session = MagicMock()
    session.scalar.return_value = profile
    db = _AsyncSession(session)
    user = {"id": "user-1", "org_id": "test-org", "role": "owner"}

    await _project_context.add_project_resources(
        "project-1",
        ProjectResourcesCreate(resources=[{"kind": "file", "path": str(second), "name": "brief"}]),
        db=db,
        user=user,
    )

    assert [resource["path"] for resource in profile.project_context["resources"]] == [str(first), str(second)]
    added_id = profile.project_context["resources"][1]["id"]

    await _project_context.reorder_project_resources(
        "project-1",
        ProjectResourcesReorder(resource_ids=[added_id, "r1"]),
        db=db,
        user=user,
    )

    assert [resource["id"] for resource in profile.project_context["resources"]] == [added_id, "r1"]


async def test_project_profile_resource_reorder_rejects_duplicates(tmp_path):
    from fastapi import HTTPException

    from brain.app.api.routers.cortex import _project_context
    from brain.systems.cortex.project_context.schemas import ProjectResourcesReorder

    first = tmp_path / "a.md"
    second = tmp_path / "b.md"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    profile = SimpleNamespace(
        id="project-1",
        org_id="test-org",
        user_id="user-1",
        slug="yc",
        name="YC Application",
        description=None,
        project_context={
            "resources": [
                {"id": "r1", "kind": "file", "path": str(first)},
                {"id": "r2", "kind": "file", "path": str(second)},
            ]
        },
        default_environment_binding_id=None,
        active=True,
        metadata_={},
        created_at=None,
    )
    session = MagicMock()
    session.scalar.return_value = profile
    db = _AsyncSession(session)

    with pytest.raises(HTTPException) as exc_info:
        await _project_context.reorder_project_resources(
            "project-1",
            ProjectResourcesReorder(resource_ids=["r1", "r1"]),
            db=db,
            user={"id": "user-1", "org_id": "test-org", "role": "owner"},
        )

    assert exc_info.value.status_code == 422


def test_manage_project_tool_is_available_to_agents():
    from brain.systems.runs.direct_agent import WORKER_TOOLS, _get_tool_handlers

    assert "manage_project" in {tool["name"] for tool in WORKER_TOOLS}
    assert "manage_project" in _get_tool_handlers()


def test_manage_idea_tool_is_available_to_agents():
    from brain.systems.runs.direct_agent import WORKER_TOOLS, _get_tool_handlers

    tool = next(item for item in WORKER_TOOLS if item["name"] == "manage_idea")

    assert "archive this thread" in tool["description"]
    assert "manage_idea" in _get_tool_handlers()


async def test_manage_idea_archive_defaults_to_current_thread(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import ideas as idea_tools

    idea = _make_idea(id="idea-1", status="active", org_id="org-1", user_id="user-1")
    session = MagicMock()
    published = []

    class FakeUnitOfWork:
        def __init__(self):
            self.session = session
            self.notifications = MagicMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    async def require_idea(session_arg, idea_id, actor):
        assert session_arg is session
        assert idea_id == "idea-1"
        assert actor["org_id"] == "org-1"
        return idea

    session.flush = AsyncMock()

    monkeypatch.setattr("brain.platform.db.repositories.unit_of_work.UnitOfWork", FakeUnitOfWork)
    monkeypatch.setattr("brain.app.api.routers.cortex._helpers._a_require_idea_for_user", require_idea)

    async def serialize_idea(idea_arg, session_arg):
        return {
            "id": str(idea_arg.id),
            "status": idea_arg.status,
            "archived_at": idea_arg.archived_at.isoformat() if idea_arg.archived_at else None,
        }

    monkeypatch.setattr(idea_tools, "_serialize_idea", serialize_idea)
    monkeypatch.setattr(
        "brain.systems.cortex.events.publish_safe",
        lambda event_type, data: published.append((event_type, data)),
    )

    with bind_agent_context({"idea_id": "idea-1", "org_id": "org-1", "user_id": "user-1"}):
        payload = json.loads(await idea_tools._handle_manage_idea(action="archive"))

    assert payload["ok"] is True
    assert payload["archived"] is True
    assert idea.status == "archived"
    assert idea.archived_at is not None
    assert session.add.call_count == 1
    assert published == [("idea_archived", {"idea_id": "idea-1", "idea": payload["idea"]})]


async def test_manage_idea_create_seeds_new_thread_message(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import ideas as idea_tools

    session = MagicMock()
    added = []
    published = []

    def add(obj):
        added.append(obj)

    async def flush():
        for obj in added:
            if obj.__class__.__name__ == "Idea" and getattr(obj, "id", None) is None:
                obj.id = "idea-created"
            if obj.__class__.__name__ == "IdeaThread" and getattr(obj, "id", None) is None:
                obj.id = 42

    class FakeUnitOfWork:
        def __init__(self):
            self.session = session

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    session.add.side_effect = add
    session.flush = AsyncMock(side_effect=flush)
    monkeypatch.setattr("brain.platform.db.repositories.unit_of_work.UnitOfWork", FakeUnitOfWork)

    async def serialize_idea(idea_arg, session_arg):
        return {"id": str(idea_arg.id), "status": idea_arg.status}

    monkeypatch.setattr(idea_tools, "_serialize_idea", serialize_idea)
    monkeypatch.setattr(
        "brain.systems.cortex.events.publish_safe",
        lambda event_type, data: published.append((event_type, data)),
    )

    with bind_agent_context({"idea_id": "parent-idea", "org_id": "org-1", "user_id": "user-1", "execution_metadata": {"run_id": 7}}):
        payload = json.loads(
            await idea_tools._handle_manage_idea(
                action="create",
                title="Check vault state",
                description="Inspect vault and AWS credential handoff path.",
                status="needs_input",
                parent_id="parent-idea",
                origin_ref="parent-idea",
            )
        )

    thread_rows = [obj for obj in added if obj.__class__.__name__ == "IdeaThread"]
    assert payload["created"] is True
    assert payload["thread_message_id"] == 42
    assert payload["run_started"] is False
    assert thread_rows[0].content == "Inspect vault and AWS credential handoff path."
    assert thread_rows[0].metadata_["source"] == "manage_idea.create"
    assert thread_rows[0].metadata_["created_by_run_id"] == 7
    assert published == [("idea_created", {"idea_id": "idea-created", "title": "Check vault state"})]


async def test_manage_idea_create_can_handoff_owner(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import ideas as idea_tools

    session = MagicMock()
    added = []
    target_user = SimpleNamespace(id="target-user", org_id="org-1", name="JB")

    def add(obj):
        added.append(obj)

    async def flush():
        for obj in added:
            if obj.__class__.__name__ == "Idea" and getattr(obj, "id", None) is None:
                obj.id = "idea-created"
            if obj.__class__.__name__ == "IdeaThread" and getattr(obj, "id", None) is None:
                obj.id = 42

    class FakeUnitOfWork:
        def __init__(self):
            self.session = session

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    session.add.side_effect = add
    session.flush = AsyncMock(side_effect=flush)
    session.scalar = AsyncMock(return_value=target_user)
    session.execute = AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: None))
    monkeypatch.setattr("brain.platform.db.repositories.unit_of_work.UnitOfWork", FakeUnitOfWork)
    monkeypatch.setattr("brain.systems.cortex.events.publish_safe", lambda *_: None)

    async def serialize_idea(idea_arg, session_arg):
        return {"id": str(idea_arg.id), "user_id": idea_arg.user_id}

    monkeypatch.setattr(idea_tools, "_serialize_idea", serialize_idea)

    with bind_agent_context({"idea_id": "parent-idea", "org_id": "org-1", "user_id": "user-1"}):
        payload = json.loads(
            await idea_tools._handle_manage_idea(
                action="create",
                title="JB follow-up",
                thread_message="Please take this follow-up.",
                parent_id="parent-idea",
                user_id="target-user",
            )
        )

    idea_rows = [obj for obj in added if obj.__class__.__name__ == "Idea"]
    thread_rows = [obj for obj in added if obj.__class__.__name__ == "IdeaThread"]
    assert payload["created"] is True
    assert payload["idea"]["user_id"] == "target-user"
    assert idea_rows[0].user_id == "target-user"
    assert thread_rows[0].user_id == "user-1"


async def test_manage_idea_create_queued_admits_run_instead_of_empty_queue(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import ideas as idea_tools

    session = MagicMock()
    added = []
    admitted = []

    def add(obj):
        added.append(obj)

    async def flush():
        for obj in added:
            if obj.__class__.__name__ == "Idea" and getattr(obj, "id", None) is None:
                obj.id = "idea-created"
            if obj.__class__.__name__ == "IdeaThread" and getattr(obj, "id", None) is None:
                obj.id = 43

    async def admit(session_arg, *, idea, seed_content, actor_user_id, parent_id, origin_ref, thread_message_id):
        assert session_arg is session
        assert idea.status == "emerged"
        assert seed_content == "Do the theme color work."
        assert actor_user_id == "user-1"
        assert parent_id == "parent-idea"
        assert origin_ref == "parent-idea"
        assert thread_message_id == 43
        idea.status = "working"
        admitted.append(str(idea.id))
        return SimpleNamespace(run_id=321)

    class FakeUnitOfWork:
        def __init__(self):
            self.session = session

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    session.add.side_effect = add
    session.flush = AsyncMock(side_effect=flush)
    monkeypatch.setattr("brain.platform.db.repositories.unit_of_work.UnitOfWork", FakeUnitOfWork)
    monkeypatch.setattr("brain.systems.cortex.events.publish_safe", lambda *_: None)
    monkeypatch.setattr(idea_tools, "_admit_created_idea_run", admit)

    async def serialize_idea(idea_arg, session_arg):
        return {"id": str(idea_arg.id), "status": idea_arg.status}

    monkeypatch.setattr(idea_tools, "_serialize_idea", serialize_idea)

    with bind_agent_context({"idea_id": "parent-idea", "org_id": "org-1", "user_id": "user-1"}):
        payload = json.loads(
            await idea_tools._handle_manage_idea(
                action="create",
                title="Fix streaming color",
                description="Do the theme color work.",
                status="queued",
                parent_id="parent-idea",
                origin_ref="parent-idea",
            )
        )

    assert payload["created"] is True
    assert payload["run_started"] is True
    assert payload["run_id"] == 321
    assert payload["idea"]["status"] == "working"
    assert admitted == ["idea-created"]


def test_unified_stream_synthesizes_visible_final_answer_from_run_artifact():
    from brain.app.api.routers.cortex._idea_ops import _append_final_answer_messages

    items = [
        {
            "type": "message",
            "id": "1",
            "role": "user",
            "content": "hi illo",
            "timestamp": "2026-05-03T18:38:10-04:00",
            "metadata": {"execution_profile": "fast"},
        },
        {
            "type": "run",
            "id": "7",
            "run_id": 7,
            "profile": "fast",
            "status": "completed",
            "created_at": "2026-05-03T18:38:11-04:00",
            "completed_at": "2026-05-03T18:38:13-04:00",
            "artifacts": [
                {
                    "id": 3,
                    "artifact_type": "final_answer",
                    "text": "Hi! How can I help?",
                    "created_at": "2026-05-03T18:38:12-04:00",
                }
            ],
        },
    ]

    _append_final_answer_messages(items)

    reply = items[-1]
    assert reply["type"] == "message"
    assert reply["role"] == "illo"
    assert reply["content"] == "Hi! How can I help?"
    assert reply["metadata"]["run_id"] == 7
    assert reply["metadata"]["synthetic_from_run_artifact"] is True


def test_unified_stream_does_not_duplicate_existing_run_reply():
    from brain.app.api.routers.cortex._idea_ops import _append_final_answer_messages

    items = [
        {
            "type": "message",
            "id": "run-final-7-3",
            "role": "illo",
            "content": "Already visible",
            "timestamp": "2026-05-03T18:38:12-04:00",
            "metadata": {"run_id": 7},
        },
        {
            "type": "run",
            "id": "7",
            "run_id": 7,
            "profile": "fast",
            "status": "completed",
            "artifacts": [{"id": 3, "artifact_type": "final_answer", "text": "Already visible"}],
        },
    ]

    _append_final_answer_messages(items)

    assert len([item for item in items if item.get("type") == "message"]) == 1


def test_unified_stream_ignores_hidden_run_messages_when_synthesizing_final_answer():
    from brain.app.api.routers.cortex._idea_ops import _append_final_answer_messages

    items = [
        {
            "type": "message",
            "id": "hidden-7",
            "role": "system",
            "content": "",
            "timestamp": "2026-05-03T18:38:12-04:00",
            "metadata": {"run_id": 7, "hidden": True},
        },
        {
            "type": "run",
            "id": "7",
            "run_id": 7,
            "profile": "fast",
            "status": "completed",
            "artifacts": [{"id": 3, "artifact_type": "final_answer", "text": "Still visible"}],
        },
    ]

    _append_final_answer_messages(items)

    visible_replies = [
        item for item in items
        if item.get("type") == "message" and item.get("role") == "illo"
    ]
    assert len(visible_replies) == 1
    assert visible_replies[0]["content"] == "Still visible"


def test_run_events_build_codex_style_work_summary():
    from brain.app.api.routers.cortex._idea_ops import _apply_run_events_to_item

    started = datetime(2026, 5, 3, 22, 0, 0, tzinfo=timezone.utc)
    finished = datetime(2026, 5, 3, 22, 0, 5, tzinfo=timezone.utc)
    item = {
        "type": "run",
        "id": "7",
        "run_id": 7,
        "profile": "fast",
        "status": "completed",
        "started_at": started.isoformat(),
        "completed_at": finished.isoformat(),
    }
    events = [
        SimpleNamespace(
            event_type="run.activity",
            payload={"label": "Reading context"},
            created_at=started,
            sequence_no=1,
        ),
        SimpleNamespace(
            event_type="run.tool_started",
            payload={"tool_name": "read_file", "args": {"path": "README.md"}},
            created_at=started,
            sequence_no=2,
        ),
        SimpleNamespace(
            event_type="run.tool_completed",
            payload={"tool_name": "read_file", "result": "ok"},
            created_at=finished,
            sequence_no=3,
        ),
        SimpleNamespace(
            event_type="run.completed",
            payload={"status": "completed"},
            created_at=finished,
            sequence_no=4,
        ),
    ]

    _apply_run_events_to_item(item, events)

    assert item["work_summary"] == {
        "duration_sec": 5,
        "activity_count": 4,
        "tool_count": 1,
        "tool_names": ["read_file"],
        "status": "completed",
    }
    assert item["tool_calls"] == [{
        "tool": "read_file",
        "args": '{"path": "README.md"}',
        "at": started.isoformat(),
        "status": "completed",
        "finished_at": finished.isoformat(),
        "result": "ok",
    }]
    assert [entry["activity"] for entry in item["activity_trace"]] == [
        "Reading context",
        "Using read_file",
        "read_file completed",
        "Completed",
    ]


def test_unified_stream_run_work_events_query_is_bounded():
    from brain.app.api.routers.cortex._idea_ops import (
        _RUN_WORK_EVENT_LIMIT_PER_RUN,
        _run_work_events_stmt,
    )

    compiled = str(_run_work_events_stmt([7, 8]).compile(compile_kwargs={"literal_binds": True}))

    assert "row_number() OVER" in compiled
    assert "agent_run_events.run_id IN (7, 8)" in compiled
    assert f"event_rank <= {_RUN_WORK_EVENT_LIMIT_PER_RUN}" in compiled

def test_cortex_exports_tenant_guard_helpers():
    import brain.app.api.routers.cortex as cortex

    assert callable(cortex._require_idea_for_user)
    assert callable(cortex._require_worker_principal)


def test_cortex_rest_routes_do_not_use_global_ws_broadcast():
    router_dir = Path(__file__).resolve().parents[1] / "brain" / "app" / "api" / "routers" / "cortex"
    offenders = [
        path.name
        for path in sorted(router_dir.glob("*.py"))
        if "ws_manager.broadcast(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_notify_route_uses_scoped_idea_guard():
    import brain.app.api.routers.cortex._ideas as ideas_mod

    source = inspect.getsource(ideas_mod.notify_illo)
    assert "_require_idea_for_user" in source


async def test_slash_commands_materializes_builtin_skills():
    from brain.app.api.routers.cortex import _analytics as analytics_mod

    skill = SimpleNamespace(
        name="develop",
        description="Implementation skill",
        model_tier="medium",
        maturity="stable",
        use_count=0,
        success_count=0,
    )
    mock_uow = MagicMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    mock_uow.session = MagicMock()
    mock_uow.skills = MagicMock()
    mock_uow.skills.list_command_summaries = AsyncMock(return_value=[skill])

    async def run_sync(fn):
        return fn(MagicMock())

    mock_uow.session.run_sync = AsyncMock(side_effect=run_sync)

    with (
        patch("brain.systems.skills.builtin.ensure_builtin_skills_cached", new=AsyncMock()) as ensure_builtin,
        patch.object(analytics_mod, "UnitOfWork", return_value=mock_uow),
    ):
        result = await analytics_mod.api_slash_commands(user={"id": "user-1"})

    ensure_builtin.assert_awaited_once_with()
    assert result[0]["name"] == "develop"


@pytest_asyncio.fixture
async def client():
    from brain.app.api.auth import get_current_user
    from brain.app.api.main import app

    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "user-1",
        "org_id": "test-org",
        "role": "owner",
        "principal_type": "human",
        "permissions": ["run:manage"],
    }
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


@pytest.mark.asyncio
async def test_legacy_agent_status_endpoint_is_retired(client):
    resp = await client.post(
        "/api/cortex/ideas/idea-1/agent-status",
        json={"action": "started", "label": "legacy-worker"},
    )

    assert resp.status_code == 410
    assert "AgentRun events" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_ideas(client, mock_session_factory):
    idea = _make_idea()
    with patch(
        "brain.app.api.routers.cortex._ideas.IdeaRepository"
    ) as MockRepo:
        MockRepo.return_value.a_list_active_for_org = AsyncMock(return_value=[idea])
        resp = await client.get("/api/cortex/ideas")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["title"] == "Test Idea"


@pytest.mark.asyncio
async def test_cortex_bootstrap_core_preserves_existing_payload_shapes(client):
    from brain.app.api.routers.cortex import _bootstrap as bootstrap_mod
    from brain.app.api.schemas.ideas import IdeaRead

    idea = IdeaRead.model_validate(_make_idea(id="idea-1", title="Bootstrap Idea"))
    member = SimpleNamespace(
        id="user-1",
        name="Bench User",
        email="bench@example.test",
        role="owner",
        color="#57cfa0",
        cortex_color="#57cfa0",
        attribution_enabled=True,
        approved=True,
        created_at=datetime.now(timezone.utc),
    )
    connection = {
        "id": "conn-1",
        "source_id": "idea-1",
        "target_id": "idea-2",
        "type": "manual",
        "weight": 1.0,
    }

    with (
        patch.object(bootstrap_mod, "list_ideas_payload", AsyncMock(return_value=[idea])),
        patch.object(bootstrap_mod, "list_connections_payload", AsyncMock(return_value=[connection])),
        patch.object(bootstrap_mod, "list_members_payload", AsyncMock(return_value=[member])),
    ):
        resp = await client.get("/api/cortex/bootstrap")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ideas"][0]["id"] == "idea-1"
    assert data["ideas"][0]["title"] == "Bootstrap Idea"
    assert data["connections"] == [connection]
    assert data["team_members"][0]["id"] == "user-1"
    assert data["workspace_apps"] is None
    assert data["workspace_pins"] is None
    assert "direct_thread" not in data


@pytest.mark.asyncio
async def test_cortex_bootstrap_can_skip_team_members_for_direct_threads(client):
    from brain.app.api.routers.cortex import _bootstrap as bootstrap_mod

    with (
        patch.object(bootstrap_mod, "list_ideas_payload", AsyncMock(return_value=[])),
        patch.object(bootstrap_mod, "list_connections_payload", AsyncMock(return_value=[])),
        patch.object(bootstrap_mod, "list_members_payload") as list_members,
    ):
        resp = await client.get("/api/cortex/bootstrap?include=ideas,connections")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ideas"] == []
    assert data["connections"] == []
    assert data["team_members"] is None
    list_members.assert_not_called()


@pytest.mark.asyncio
async def test_cortex_bootstrap_direct_thread_reuses_stream_payload(client):
    from brain.app.api.routers.cortex import _bootstrap as bootstrap_mod

    stream = [
        {
            "type": "message",
            "timestamp": "2026-05-01T12:00:00Z",
            "id": "1",
            "role": "user",
            "content": "Hello",
        }
    ]

    with (
        patch.object(bootstrap_mod, "list_ideas_payload", AsyncMock(return_value=[])),
        patch.object(bootstrap_mod, "list_connections_payload", AsyncMock(return_value=[])),
        patch.object(bootstrap_mod, "unified_stream_payload", return_value=stream) as stream_payload,
    ):
        resp = await client.get("/api/cortex/bootstrap?include=ideas,connections,direct_thread&idea_id=idea-1")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ideas"] == []
    assert data["connections"] == []
    assert data["direct_thread"] == {"idea_id": "idea-1", "stream": stream}
    stream_payload.assert_called_once()
    assert stream_payload.call_args.kwargs["idea_id"] == "idea-1"
    assert stream_payload.call_args.kwargs["include_debug"] is False


@pytest.mark.asyncio
async def test_cortex_bootstrap_direct_thread_can_return_selected_idea_without_graph(client):
    from brain.app.api.routers.cortex import _bootstrap as bootstrap_mod

    idea = _make_idea(id="idea-1", title="Selected Thread")
    stream = [
        {
            "type": "message",
            "timestamp": "2026-05-01T12:00:00Z",
            "id": "1",
            "role": "user",
            "content": "Hello",
        }
    ]

    with (
        patch.object(bootstrap_mod, "_require_idea_for_user", AsyncMock(return_value=idea)) as require_idea,
        patch.object(bootstrap_mod, "list_ideas_payload") as list_ideas,
        patch.object(bootstrap_mod, "list_connections_payload") as list_connections,
        patch.object(bootstrap_mod, "unified_stream_payload", return_value=stream),
    ):
        resp = await client.get("/api/cortex/bootstrap?include=selected_idea,direct_thread&idea_id=idea-1")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ideas"] is None
    assert data["connections"] is None
    assert data["selected_idea"]["id"] == "idea-1"
    assert data["selected_idea"]["title"] == "Selected Thread"
    assert data["direct_thread"] == {"idea_id": "idea-1", "stream": stream}
    require_idea.assert_awaited_once()
    list_ideas.assert_not_called()
    list_connections.assert_not_called()


@pytest.mark.asyncio
async def test_list_ideas_with_status_filter(client, mock_session_factory):
    idea = _make_idea(status="working")
    with patch(
        "brain.app.api.routers.cortex._ideas.IdeaRepository"
    ) as MockRepo:
        MockRepo.return_value.a_list_by_status_for_org = AsyncMock(return_value=[idea])
        resp = await client.get("/api/cortex/ideas?status=working")
    assert resp.status_code == 200
    MockRepo.return_value.a_list_by_status_for_org.assert_awaited_once_with("working", ANY)


@pytest.mark.asyncio
async def test_list_archived_ideas(client, mock_session_factory):
    archived_at = datetime.now(timezone.utc)
    idea = _make_idea(status="archived", archived_at=archived_at)
    with patch(
        "brain.app.api.routers.cortex._ideas.IdeaRepository"
    ) as MockRepo:
        MockRepo.return_value.a_list_archived_for_org = AsyncMock(return_value=[idea])
        resp = await client.get("/api/cortex/ideas/archived?limit=5")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload[0]["archived_at"] is not None
    MockRepo.return_value.a_list_archived_for_org.assert_awaited_once_with(ANY, limit=5)


@pytest.mark.asyncio
async def test_empty_archived_ideas(client, mock_session_factory):
    broadcasts = []

    async def _broadcast(event_type, data, **kwargs):
        broadcasts.append((event_type, data, kwargs))

    with patch(
        "brain.app.api.routers.cortex._ideas.IdeaRepository"
    ) as MockRepo, patch(
        "brain.app.api.routers.cortex._ideas.ws_manager.broadcast_product_event",
        side_effect=_broadcast,
    ):
        MockRepo.return_value.a_hard_delete_archived_for_org = AsyncMock(return_value=3)
        resp = await client.delete("/api/cortex/ideas/archived")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 3}
    MockRepo.return_value.a_hard_delete_archived_for_org.assert_awaited_once_with("test-org")
    mock_session_factory.commit.assert_called()
    assert broadcasts == [
        ("idea_archive_emptied", {"deleted": 3}, {"org_id": "test-org"}),
    ]


@pytest.mark.asyncio
async def test_create_idea(client, mock_session_factory):
    idea = _make_idea(title="New Idea")
    broadcasts = []

    async def _broadcast(event_type, data, **kwargs):
        broadcasts.append((event_type, data, kwargs))

    with patch(
        "brain.app.api.routers.cortex._ideas.IdeaRepository"
    ) as MockRepo, patch(
        "brain.app.api.routers.cortex._ideas.ws_manager.broadcast_product_event",
        side_effect=_broadcast,
    ), patch(
        "brain.app.api.routers.cortex._ideas.generate_and_store_idea_display_title"
    ) as mock_generate_title:
        MockRepo.return_value.a_create = AsyncMock(return_value=idea)
        resp = await client.post(
            "/api/cortex/ideas",
            json={"title": "New Idea"},
        )
    assert resp.status_code == 201
    assert resp.json()["title"] == "New Idea"
    assert broadcasts == [
        (
            "idea_created",
            {"idea_id": idea.id, "title": "New Idea"},
            {"org_id": "test-org"},
        )
    ]
    mock_generate_title.assert_called_once_with(
        idea_id=str(idea.id),
        raw_title="New Idea",
        user_id="user-1",
        org_id="test-org",
    )


@pytest.mark.asyncio
async def test_create_idea_empty_title_422(client, mock_session_factory):
    resp = await client.post(
        "/api/cortex/ideas",
        json={"title": ""},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_thread_message(client, mock_session_factory):
    _assign_thread_defaults_on_add(mock_session_factory)

    resp = await client.post(
        "/api/cortex/ideas/some-id/threads",
        json={"content": "Hello", "role": "user"},
    )

    assert resp.status_code == 201
    assert resp.json()["content"] == "Hello"


@pytest.mark.asyncio
async def test_create_thread_message_commits_before_broadcast(client, mock_session_factory):
    idea = _make_idea(id="some-id", org_id="test-org")
    order: list[str] = []
    _assign_thread_defaults_on_add(mock_session_factory)
    mock_session_factory.commit.side_effect = lambda: order.append("commit")

    async def _broadcast(*_args, **_kwargs):
        order.append("broadcast")

    with patch("brain.app.api.routers.cortex._ideas._require_idea_for_user", AsyncMock(return_value=idea)), \
         patch("brain.app.api.routers.cortex._ideas.ws_manager.broadcast_product_event", side_effect=_broadcast):
        resp = await client.post(
            "/api/cortex/ideas/some-id/threads",
            json={"content": "Hello", "role": "user"},
        )

    assert resp.status_code == 201
    assert "commit" in order
    assert "broadcast" in order
    assert order.index("commit") < order.index("broadcast")


@pytest.mark.asyncio
async def test_update_idea_status_commits_before_broadcast(client, mock_session_factory):
    idea = _make_idea(id="status-idea", status="emerged", org_id="test-org")
    order: list[str] = []
    mock_session_factory.commit.side_effect = lambda: order.append("commit")

    async def _broadcast(*_args, **_kwargs):
        order.append("broadcast")

    with patch("brain.app.api.routers.cortex._ideas._require_idea_for_user", AsyncMock(return_value=idea)), \
         patch("brain.app.api.routers.cortex._ideas.ws_manager.broadcast_product_event", side_effect=_broadcast):
        resp = await client.patch(
            "/api/cortex/ideas/status-idea/status",
            json={"status": "queued"},
        )

    assert resp.status_code == 200, resp.text
    assert "commit" in order
    assert "broadcast" in order
    assert order.index("commit") < order.index("broadcast")


@pytest.mark.asyncio
async def test_restore_archived_idea(client, mock_session_factory):
    archived_at = datetime.now(timezone.utc)
    idea = _make_idea(id="restore-id", status="archived", archived_at=archived_at)

    with patch("brain.app.api.routers.cortex._ideas._require_idea_for_user", AsyncMock(return_value=idea)):
        resp = await client.post(f"/api/cortex/ideas/{idea.id}/restore")

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["archived_at"] is None
    assert payload["status"] == "emerged"
    assert idea.archived_at is None
    assert idea.status == "emerged"


@pytest.mark.asyncio
async def test_update_idea_user_id_handoff_same_org(client, mock_session_factory):
    """Changing user_id is a handoff: ownership changes without moving orbit anchors."""
    from unittest.mock import MagicMock, patch

    owner_id = "owner-user"
    target_id = "target-user"
    org_id = "test-org"
    idea = _make_idea(
        id="handoff-id",
        user_id=owner_id,
        org_id=org_id,
        orbit_anchor_type="pin",
        orbit_anchor_id="marketing-pin",
    )
    target_user = MagicMock(id=target_id, org_id=org_id, name="Target", color="#22c55e")

    mock_session_factory.scalar.return_value = target_user
    mock_session_factory.execute.return_value.one_or_none.return_value = None

    with (
        patch("brain.app.api.routers.cortex._ideas._require_idea_for_user", AsyncMock(return_value=idea)),
        patch("brain.app.api.routers.cortex._ideas.require_org_context", return_value=org_id),
    ):
        resp = await client.put(f"/api/cortex/ideas/{idea.id}", json={"user_id": target_id})

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["user_id"] == target_id
    assert payload["archived_at"] is None
    assert idea.user_id == target_id
    assert idea.orbit_anchor_type == "pin"
    assert idea.orbit_anchor_id == "marketing-pin"
    assert idea.archived_at is None
    assert idea.status != "archived"


@pytest.mark.asyncio
async def test_update_idea_user_id_handoff_rejects_cross_org(client, mock_session_factory):
    """Thread owner handoffs cannot move a thread to a user outside the caller org."""
    from unittest.mock import MagicMock, patch

    owner_id = "owner-user"
    target_id = "target-user"
    org_id = "test-org"
    idea = _make_idea(id="handoff-id", user_id=owner_id, org_id=org_id)
    target_user = MagicMock(id=target_id, org_id="other-org", name="Other", color="#ef4444")
    mock_session_factory.scalar.return_value = target_user

    with (
        patch("brain.app.api.routers.cortex._ideas._require_idea_for_user", AsyncMock(return_value=idea)),
        patch("brain.app.api.routers.cortex._ideas.require_org_context", return_value=org_id),
    ):
        resp = await client.patch(f"/api/cortex/ideas/{idea.id}", json={"user_id": target_id})

    assert resp.status_code == 403
    assert idea.user_id == owner_id
    assert idea.archived_at is None


@pytest.mark.asyncio
async def test_update_idea_user_id_handoff_preserves_previous_owner_color_without_thread_author(client, mock_session_factory):
    """If no one has written in the thread yet, handoff keeps the previous owner as display author."""
    from unittest.mock import MagicMock, patch

    owner_id = "owner-user"
    target_id = "target-user"
    org_id = "test-org"
    idea = _make_idea(id="handoff-id", user_id=owner_id, org_id=org_id, agent_details=None)
    target_user = MagicMock(id=target_id, org_id=org_id, name="Target", color="#22c55e")
    previous_owner = MagicMock()
    previous_owner.name = "Previous Owner"
    previous_owner.color = "#d18262"

    mock_session_factory.scalar.return_value = target_user
    mock_session_factory.execute.return_value.one_or_none.side_effect = [
        None,  # no thread author before the handoff
        None,  # no thread author when serializing the response
        previous_owner,  # display author fallback frozen before user_id changed
    ]

    with (
        patch("brain.app.api.routers.cortex._ideas._require_idea_for_user", AsyncMock(return_value=idea)),
        patch("brain.app.api.routers.cortex._ideas.require_org_context", return_value=org_id),
    ):
        resp = await client.put(f"/api/cortex/ideas/{idea.id}", json={"user_id": target_id})

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["user_id"] == target_id
    assert payload["author_name"] == "Previous Owner"
    assert payload["author_color"] == "#d18262"
    assert idea.agent_details["display_author_user_id"] == owner_id


@pytest.mark.asyncio
async def test_update_idea_orbit_anchor_pin_keeps_owner(client, mock_session_factory):
    """Pin orbiting is independent from the thread owner."""
    from unittest.mock import MagicMock, patch

    owner_id = "owner-user"
    org_id = "test-org"
    pin_id = "marketing-pin"
    idea = _make_idea(id="orbit-id", user_id=owner_id, org_id=org_id)
    pin = MagicMock(id=pin_id, org_id=org_id, archived_at=None)

    mock_session_factory.scalar.return_value = pin
    mock_session_factory.execute.return_value.one_or_none.return_value = None

    with (
        patch("brain.app.api.routers.cortex._ideas._require_idea_for_user", AsyncMock(return_value=idea)),
        patch("brain.app.api.routers.cortex._ideas.require_org_context", return_value=org_id),
    ):
        resp = await client.patch(
            f"/api/cortex/ideas/{idea.id}",
            json={"orbit_anchor_type": "pin", "orbit_anchor_id": pin_id},
        )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["user_id"] == owner_id
    assert payload["orbit_anchor_type"] == "pin"
    assert payload["orbit_anchor_id"] == pin_id
    assert idea.user_id == owner_id
    assert idea.orbit_anchor_type == "pin"
    assert idea.orbit_anchor_id == pin_id


@pytest.mark.asyncio
async def test_list_ideas_uses_last_human_thread_author_color(client, mock_session_factory):
    """Blob color follows the last human thread author, not the owner/orbit user."""
    from unittest.mock import MagicMock, patch

    owner_id = "owner-user"
    org_id = "test-org"
    idea = _make_idea(id="color-id", user_id=owner_id, org_id=org_id)
    last_author = MagicMock()
    last_author.name = "Last Human"
    last_author.color = "#abcdef"

    with patch("brain.app.api.routers.cortex._ideas.IdeaRepository") as MockRepo:
        MockRepo.return_value.a_list_active_for_org = AsyncMock(return_value=[idea])
        mock_session_factory.execute.return_value.all.return_value = [
            SimpleNamespace(
                idea_id="color-id",
                author_name=last_author.name,
                author_color=last_author.color,
            )
        ]
        resp = await client.get("/api/cortex/ideas")

    assert resp.status_code == 200, resp.text
    payload = resp.json()[0]
    assert payload["user_id"] == owner_id
    assert payload["author_name"] == "Last Human"
    assert payload["author_color"] == "#abcdef"



def test_project_context_permission_scope_rejects_escape_and_forbidden_paths():
    from brain.systems.cortex.project_context.permissions import validate_path_permission
    from brain.systems.cortex.project_context.snapshot import build_project_context_snapshot

    snapshot = build_project_context_snapshot(
        {
            "project_context": {
                "name": "Brain",
                "resources": [
                    {
                        "type": "repo",
                        "path": "/work/brain",
                        "allowed_paths": ["brain", "tests"],
                        "forbidden_paths": [".env"],
                        "mode": "read_write",
                    }
                ],
            }
        }
    )

    assert snapshot["status"] == "validated"
    assert snapshot["permission_scope"]["allowed_paths"] == ["/work/brain/brain", "/work/brain/tests"]
    allowed, reason, _scope = validate_path_permission("/work/brain/brain/app/api/main.py", snapshot, operation="write")
    assert allowed is True
    assert reason is None
    allowed, reason, _scope = validate_path_permission("/work/brain/secrets.txt", snapshot, operation="write")
    assert allowed is False
    assert "outside allowed" in str(reason)
    allowed, reason, _scope = validate_path_permission("/work/brain/.env", snapshot, operation="read")
    assert allowed is False
    assert "forbidden" in str(reason)
    allowed, reason, _scope = validate_path_permission("/work/other/file.py", snapshot, operation="read")
    assert allowed is False
    assert "outside allowed" in str(reason)
    allowed, reason, _scope = validate_path_permission("../../etc/passwd", snapshot, operation="read")
    assert allowed is False
    assert "escapes" in str(reason)


def test_project_context_validation_rejects_bad_permission_mode():
    from brain.systems.cortex.project_context.snapshot import build_project_context_snapshot

    snapshot = build_project_context_snapshot(
        {
            "project_context": {
                "resources": [
                    {"type": "folder", "path": "brain", "mode": "superuser"},
                ],
            }
        }
    )

    assert snapshot["status"] == "invalid"
    assert any("mode" in error for error in snapshot["validation_errors"])


def test_project_context_validation_rejects_missing_local_path_when_enforced(tmp_path):
    from brain.systems.cortex.project_context.snapshot import build_project_context_snapshot

    snapshot = build_project_context_snapshot(
        {
            "project_context": {
                "resources": [
                    {"type": "folder", "path": str(tmp_path / "missing")},
                ],
            }
        },
        validate_local_paths=True,
    )

    assert snapshot["status"] == "invalid"
    assert any("does not exist" in error for error in snapshot["validation_errors"])


def test_project_context_snapshot_attachment_revalidates_existing_status():
    from brain.systems.cortex.project_context.snapshot import attach_project_context_snapshot

    payload = attach_project_context_snapshot(
        {},
        {
            "project_context_snapshot": {
                "status": "validated",
                "resources": [],
            },
        },
    )

    snapshot = payload["project_context_snapshot"]
    assert snapshot["status"] == "invalid"
    assert snapshot["validation_errors"] == [
        "project_context_snapshot.resources must contain at least one resource."
    ]


@pytest.mark.asyncio
async def test_create_project_profile_rejects_empty_project_context(client, mock_session_factory):
    from brain.app.api.routers.cortex import _project_context as pc_mod

    with (
        patch("brain.app.api.routers.cortex._project_context.get_current_user", return_value={"id": "user-1", "org_id": "org-1", "role": "owner"}),
        patch("brain.app.api.routers.cortex._project_context.require_org_context", return_value="org-1"),
        patch.object(pc_mod, "get_db", lambda: mock_session_factory),
    ):
        mock_session_factory.scalar.return_value = None
        mock_session_factory.refresh.side_effect = lambda obj: None

        def add(obj):
            obj.id = "empty-profile-1"
            obj.created_at = datetime.now(timezone.utc)

        mock_session_factory.add.side_effect = add
        response = await client.post(
            "/api/cortex/project-context/profiles",
            json={"slug": "empty-project", "name": "Empty Project", "project_context": {"name": "Empty Project", "resources": []}},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["validation_errors"] == [
        "project_context_snapshot.resources must contain at least one resource."
    ]


async def test_create_project_profile_validates_project_context(client, mock_session_factory):
    from brain.app.api.routers.cortex import _project_context as pc_mod

    with (
        patch("brain.app.api.routers.cortex._project_context.get_current_user", return_value={"id": "user-1", "org_id": "org-1", "role": "owner"}),
        patch("brain.app.api.routers.cortex._project_context.require_org_context", return_value="org-1"),
    ):
        mock_session_factory.scalar.return_value = None
        profile = MagicMock(
            id="profile-1",
            org_id="org-1",
            user_id="user-1",
            slug="brain",
            name="Brain",
            description=None,
            project_context={"resources": [{"type": "folder", "path": "brain"}]},
            default_environment_binding_id=None,
            active=True,
            metadata_={},
            created_at=datetime.now(timezone.utc),
        )
        mock_session_factory.refresh.side_effect = lambda obj: None
        # Let the route serialize the object it constructed after refresh.
        def add(obj):
            obj.id = profile.id
            obj.created_at = profile.created_at
        mock_session_factory.add.side_effect = add
        resp = await client.post(
            "/api/cortex/project-context/profiles",
            json={"slug": "brain", "name": "Brain", "project_context": {"resources": [{"type": "folder", "path": "brain"}]}},
        )

    assert resp.status_code == 201
    assert resp.json()["slug"] == "brain"
    assert mock_session_factory.add.called


@pytest.mark.asyncio
async def test_create_project_profile_rejects_invalid_context(client, mock_session_factory):
    with (
        patch("brain.app.api.routers.cortex._project_context.get_current_user", return_value={"id": "user-1", "org_id": "org-1", "role": "owner"}),
        patch("brain.app.api.routers.cortex._project_context.require_org_context", return_value="org-1"),
    ):
        resp = await client.post(
            "/api/cortex/project-context/profiles",
            json={"slug": "bad", "name": "Bad", "project_context": {"resources": [{"type": "folder"}]}},
        )

    assert resp.status_code == 422
    assert "validation_errors" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_project_context_github_connect_uses_server_side_vault_token(client):
    with (
        patch("brain.systems.cortex.project_context.vault.async_github_token_from_vault", AsyncMock(return_value="ghp_secret")) as token_from_vault,
        patch("brain.app.api.routers.cortex._project_context.async_connect_with_token", AsyncMock(return_value={
            "login": "alex",
            "repos": [
                {
                    "full_name": "example-org/example-repo",
                    "html_url": "https://github.com/example-org/example-repo",
                    "private": True,
                    "permissions": {"push": True},
                }
            ],
        })) as connect,
    ):
        resp = await client.post(
            "/api/cortex/project-context/github/connect",
            json={"vault_key": "GITHUB_TOKEN"},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["login"] == "alex"
    assert payload["repos"][0]["full_name"] == "example-org/example-repo"
    assert "ghp_secret" not in str(payload)
    token_from_vault.assert_awaited_once()
    connect.assert_awaited_once_with("ghp_secret")


@pytest.mark.asyncio
async def test_project_context_github_connect_logs_vault_read_as_api_actor(client):
    with (
        patch("brain.systems.cortex.project_context.vault.async_has_pin", AsyncMock(return_value=False)),
        patch("brain.systems.cortex.project_context.vault.async_get_secret", AsyncMock(return_value="ghp_secret")) as get_secret,
        patch("brain.app.api.routers.cortex._project_context.async_connect_with_token", AsyncMock(return_value={
            "login": "alex",
            "repos": [],
        })),
    ):
        resp = await client.post(
            "/api/cortex/project-context/github/connect",
            json={"vault_key": "GITHUB_TOKEN"},
        )

    assert resp.status_code == 200
    get_secret.assert_awaited_once_with(
        "GITHUB_TOKEN",
        user_id="user-1",
        org_id="test-org",
        accessed_by="api",
    )


@pytest.mark.asyncio
async def test_project_context_github_search_supports_public_without_vault(client):
    with patch("brain.app.api.routers.cortex._project_context.async_search_repos", AsyncMock(return_value={
        "matched_exact": True,
        "repos": [
            {
                "full_name": "rtk-ai/rtk",
                "html_url": "https://github.com/rtk-ai/rtk",
                "private": False,
                "permissions": {},
            }
        ],
    })) as search:
        resp = await client.post(
            "/api/cortex/project-context/github/search",
            json={"query": "rtk-ai/rtk"},
        )

    assert resp.status_code == 200
    assert resp.json()["matched_exact"] is True
    search.assert_awaited_once_with("rtk-ai/rtk", token=None)


@pytest.mark.asyncio
async def test_project_context_github_bind_token_verifies_repo_and_binds_owned_vault_token(client):
    binding = {
        "id": 12,
        "secret_id": 5,
        "key_name": "GITHUB_TOKEN",
        "user_id": "user-1",
        "org_id": "test-org",
        "project_slug": "example-org/example-repo",
        "env_name": "GH_TOKEN",
        "active": True,
    }
    repo = {
        "full_name": "example-org/example-repo",
        "html_url": "https://github.com/example-org/example-repo",
        "private": True,
        "permissions": {"push": True},
    }
    with (
        patch("brain.systems.cortex.project_context.vault.async_github_token_from_vault", AsyncMock(return_value="ghp_secret")) as token_from_vault,
        patch("brain.app.api.routers.cortex._project_context.async_get_repo_by_slug", AsyncMock(return_value=repo)) as get_repo,
        patch("brain.systems.vault.async_bind_project_secret_by_key", AsyncMock(return_value=binding)) as bind,
    ):
        resp = await client.post(
            "/api/cortex/project-context/github/bind-token",
            json={"vault_key": "GITHUB_TOKEN", "repo": "https://github.com/example-org/example-repo"},
        )

    assert resp.status_code == 201
    payload = resp.json()
    assert payload["project_slug"] == "example-org/example-repo"
    assert payload["env_name"] == "GH_TOKEN"
    assert payload["write_access"] is True
    assert "ghp_secret" not in str(payload)
    token_from_vault.assert_awaited_once_with(
        "GITHUB_TOKEN",
        user=ANY,
        unlock_token=None,
        allow_shared=False,
    )
    get_repo.assert_awaited_once_with("example-org/example-repo", token="ghp_secret")
    bind.assert_awaited_once_with(
        "GITHUB_TOKEN",
        user_id="user-1",
        org_id="test-org",
        project_slug="example-org/example-repo",
        env_name="GH_TOKEN",
    )


@pytest.mark.asyncio
async def test_project_context_github_bind_token_rejects_repos_not_visible_to_token(client):
    with (
        patch("brain.systems.cortex.project_context.vault.async_github_token_from_vault", AsyncMock(return_value="ghp_secret")),
        patch("brain.app.api.routers.cortex._project_context.async_get_repo_by_slug", AsyncMock(return_value=None)),
        patch("brain.systems.vault.async_bind_project_secret_by_key", AsyncMock()) as bind,
    ):
        resp = await client.post(
            "/api/cortex/project-context/github/bind-token",
            json={"vault_key": "GITHUB_TOKEN", "repo": "example-org/private-app"},
        )

    assert resp.status_code == 404
    bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_context_local_file_upload_returns_backend_readable_path(client, tmp_path, monkeypatch):
    from brain.app.api.routers.cortex import _project_context as pc_mod

    monkeypatch.setattr(pc_mod, "UPLOAD_DIR", tmp_path)
    resp = await client.post(
        "/api/cortex/project-context/local-files",
        data={"relative_paths": "src/App.svelte"},
        files={"files": ("App.svelte", b"<script>export let name;</script>", "text/plain")},
    )

    assert resp.status_code == 200
    uploaded = resp.json()["files"][0]
    assert uploaded["relative_path"] == "src/App.svelte"
    assert uploaded["storage_path"].endswith("src/App.svelte")
    assert uploaded["uri"].startswith("/static/uploads/project-context/")
    assert (tmp_path / "project-context").exists()


@pytest.mark.asyncio
async def test_project_context_local_upload_keeps_duplicate_names_distinct(client, tmp_path, monkeypatch):
    from brain.app.api.routers.cortex import _project_context as pc_mod

    monkeypatch.setattr(pc_mod, "UPLOAD_DIR", tmp_path)
    resp = await client.post(
        "/api/cortex/project-context/local-files",
        files=[
            ("relative_paths", (None, "src/App.svelte")),
            ("relative_paths", (None, "src/App.svelte")),
            ("files", ("App.svelte", b"first", "text/plain")),
            ("files", ("App.svelte", b"second", "text/plain")),
        ],
    )

    assert resp.status_code == 200
    uploaded = resp.json()["files"]
    assert [item["relative_path"] for item in uploaded] == ["src/App.svelte", "src/App-2.svelte"]
    assert Path(uploaded[0]["storage_path"]).read_text() == "first"
    assert Path(uploaded[1]["storage_path"]).read_text() == "second"


@pytest.mark.asyncio
async def test_project_context_local_upload_accepts_binary_documents(client, tmp_path, monkeypatch):
    from brain.app.api.routers.cortex import _project_context as pc_mod

    monkeypatch.setattr(pc_mod, "UPLOAD_DIR", tmp_path)
    content = b"%PDF-1.7\x00binary project context"
    resp = await client.post(
        "/api/cortex/project-context/local-files",
        data={"relative_paths": "docs/karoid_ai.pdf"},
        files={"files": ("karoid_ai.pdf", content, "application/pdf")},
    )

    assert resp.status_code == 200
    uploaded = resp.json()["files"][0]
    assert uploaded["relative_path"] == "docs/karoid_ai.pdf"
    assert uploaded["mime"] == "application/pdf"
    assert Path(uploaded["storage_path"]).read_bytes() == content


@pytest.mark.asyncio
async def test_attach_idea_project_context_persists_snapshot_scope_and_env_binding(client, mock_session_factory):
    idea = _make_idea(id="idea-1", org_id="org-1")
    mock_session_factory.get.return_value = idea
    mock_session_factory.scalars.return_value.first.return_value = idea
    mock_session_factory.scalars.return_value.all.return_value = []
    with (
        patch("brain.app.api.routers.cortex._project_context.get_current_user", return_value={"id": "user-1", "org_id": "org-1", "role": "owner"}),
        patch("brain.app.api.routers.cortex._project_context.require_org_context", return_value="org-1"),
    ):
        def add(obj):
            obj.id = 7
            obj.created_at = datetime.now(timezone.utc)
        mock_session_factory.add.side_effect = add
        resp = await client.post(
            "/api/cortex/ideas/idea-1/project-context",
            json={
                "environment_binding_id": 123,
                "project_context": {"resources": [{"type": "folder", "path": "brain", "allowed_paths": ["app/api"]}]},
            },
        )

    assert resp.status_code == 201
    payload = resp.json()
    assert payload["idea_id"] == "idea-1"
    assert payload["environment_binding_id"] == 123
    assert payload["status"] == "validated"
    assert "brain/app/api" in payload["permission_scope"]["allowed_paths"]
    assert idea.agent_details["project_context"]["resources"][0]["path"] == "brain"
