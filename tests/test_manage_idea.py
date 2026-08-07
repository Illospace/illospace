"""Focused manage_idea validation and persistence tests."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def install_unit_of_work(monkeypatch):
    """Install one async unit-of-work shape around the session under test."""

    def install(session, *, flush_on_exit=False):
        class TestUnitOfWork:
            async def __aenter__(self):
                self.session = session
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                if flush_on_exit and exc_type is None:
                    await session.flush()
                return False

        monkeypatch.setattr(
            "brain.platform.db.repositories.unit_of_work.UnitOfWork",
            TestUnitOfWork,
        )

    return install


@pytest.fixture
def fake_manage_idea_uow(install_unit_of_work):
    session = MagicMock()
    added = []

    def add(obj):
        added.append(obj)

    async def flush():
        for obj in added:
            if obj.__class__.__name__ == "Idea" and getattr(obj, "id", None) is None:
                obj.id = "idea-created"

    session.add.side_effect = add
    session.flush = AsyncMock(side_effect=flush)
    install_unit_of_work(session)
    return SimpleNamespace(session=session, added=added)


@pytest.mark.parametrize(
    "parent_id",
    [None, "", "   ", "null", "none", "00000000-0000-0000-0000-000000000000"],
)
async def test_manage_idea_create_normalizes_emptyish_parent_id_to_null(
    monkeypatch,
    fake_manage_idea_uow,
    parent_id,
):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import ideas as idea_tools

    monkeypatch.setattr("brain.platform.events.publish_safe", lambda *_: None)
    monkeypatch.setattr(idea_tools, "_seed_created_idea_thread", AsyncMock(return_value=None))
    monkeypatch.setattr(
        idea_tools,
        "_serialize_idea",
        AsyncMock(return_value={"id": "idea-created", "parent_id": None}),
    )

    with bind_agent_context({"org_id": "org-1", "user_id": "user-1"}):
        payload = json.loads(
            await idea_tools._handle_manage_idea(
                action="create",
                title="Replay run 2327 input",
                parent_id=parent_id,
                origin_ref=" null ",
                orbit_anchor_id="00000000-0000-0000-0000-000000000000",
            )
        )

    idea_rows = [
        obj for obj in fake_manage_idea_uow.added
        if obj.__class__.__name__ == "Idea"
    ]
    assert payload["created"] is True
    assert len(idea_rows) == 1
    assert idea_rows[0].parent_id is None
    assert idea_rows[0].origin_ref is None


@pytest.mark.parametrize(
    "parent_id",
    [None, "", "00000000-0000-0000-0000-000000000000"],
)
async def test_manage_idea_create_persists_emptyish_parent_as_null(
    monkeypatch,
    install_unit_of_work,
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
    parent_id,
):
    del sqlite_postgres_ddl_patch

    from brain.platform.db.models.idea import Idea
    from brain.platform.db.models.org import Org, User
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import ideas as idea_tools

    org_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    user_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
    session = await async_sqlite_session_factory(
        [Org.__table__, User.__table__, Idea.__table__]
    )
    session.add(Org(id=org_id, name="Issue 419 Org", slug=f"issue-419-{parent_id or 'omitted'}"))
    session.add(User(
        id=user_id,
        org_id=org_id,
        name="Issue 419 User",
        email=f"{uuid.uuid4()}@test.com",
    ))
    await session.flush()

    async def serialize_idea(idea, _session):
        return {"id": str(idea.id), "parent_id": idea.parent_id}

    install_unit_of_work(session, flush_on_exit=True)
    monkeypatch.setattr("brain.platform.events.publish_safe", lambda *_: None)
    monkeypatch.setattr(idea_tools, "_seed_created_idea_thread", AsyncMock(return_value=None))
    monkeypatch.setattr(idea_tools, "_serialize_idea", serialize_idea)

    with bind_agent_context({"org_id": org_id, "user_id": user_id}):
        payload = json.loads(
            await idea_tools._handle_manage_idea(
                action="create",
                title="Persist normalized parent",
                parent_id=parent_id,
            )
        )

    session.expire_all()
    persisted = await session.get(Idea, payload["idea"]["id"])
    assert persisted is not None
    assert persisted.parent_id is None


async def test_manage_idea_create_rejects_missing_parent_before_insert(
    monkeypatch,
    fake_manage_idea_uow,
):
    from fastapi import HTTPException

    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import ideas as idea_tools
    from brain.systems.runs.tool_outcomes import ToolHandlerResult

    missing_parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    async def missing_parent(_session, idea_id, _actor):
        assert idea_id == missing_parent_id
        raise HTTPException(status_code=404, detail="Idea not found")

    monkeypatch.setattr(idea_tools, "_require_idea_for_actor", missing_parent)

    with bind_agent_context({"org_id": "org-1", "user_id": "user-1"}):
        result = await idea_tools._handle_manage_idea(
            action="create",
            title="Child of missing idea",
            parent_id=missing_parent_id,
        )
    assert isinstance(result, ToolHandlerResult)
    payload = json.loads(result.value)

    assert payload == {
        "error": "parent_id must be an existing idea id or omitted",
    }
    failure = result.outcome.failure
    assert failure is not None
    assert failure.message == payload["error"]
    assert failure.category == "ToolValidationError"
    fake_manage_idea_uow.session.add.assert_not_called()
    fake_manage_idea_uow.session.flush.assert_not_called()


async def test_manage_idea_rejects_bogus_uuid_with_typed_validation_error(
    fake_manage_idea_uow,
):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import ideas as idea_tools
    from brain.systems.runs.tool_outcomes import ToolHandlerResult

    with bind_agent_context({"org_id": "org-1", "user_id": "user-1"}):
        result = await idea_tools._handle_manage_idea(
            action="create",
            title="Invalid parent",
            parent_id="not-a-uuid",
        )

    assert isinstance(result, ToolHandlerResult)
    assert json.loads(result.value) == {
        "error": "parent_id must be an existing idea id or omitted",
    }
    failure = result.outcome.failure
    assert failure is not None
    assert failure.category == "ToolValidationError"
    fake_manage_idea_uow.session.add.assert_not_called()
