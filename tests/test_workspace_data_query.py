from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.dialects import postgresql


class _FakeSession:
    def __init__(self, dialect: str = "sqlite"):
        self.dialect = dialect
        self.rollbacks = 0

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    def rollback(self):
        self.rollbacks += 1


def _patch_uow(monkeypatch, session: _FakeSession) -> None:
    from brain.platform.db.repositories import unit_of_work

    class _FakeUow:
        async def __aenter__(self):
            self.session = session
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    monkeypatch.setattr(unit_of_work, "UnitOfWork", _FakeUow)


def test_workspace_data_casts_idea_uuid_for_legacy_run_thread_join():
    from brain.systems.runs.tool_catalog.handlers import workspace_data
    from brain.platform.db.models.agent_run import AgentRunRow
    from brain.platform.db.models.idea import Idea

    stmt = select(AgentRunRow.id).outerjoin(
        Idea,
        workspace_data._uuid_text_equals(Idea.id, AgentRunRow.thread_id),
    )

    compiled = str(stmt.compile(dialect=postgresql.dialect()))

    assert "CAST(ideas.id AS VARCHAR) = agent_runs.thread_id" in compiled
    assert "ideas.id = agent_runs.thread_id" not in compiled


async def test_workspace_data_run_queries_scope_on_agent_run_org():
    from brain.systems.runs.tool_catalog.handlers import workspace_data

    captured = {}

    class _Result:
        def all(self):
            return []

    class _Session:
        def execute(self, stmt):
            captured["sql"] = str(stmt.compile(dialect=postgresql.dialect()))
            return _Result()

    await workspace_data._query_runs(
        _Session(),
        {"sources": {}},
        start=None,
        end=None,
        org_id="44faf010-23ae-4aca-b6ad-2e1b574c717c",
        user_id=None,
        person_ids=[],
        idea_id=None,
        run_id=None,
        search=None,
        limit=5,
    )

    assert "WHERE agent_runs.org_id" in captured["sql"]
    assert "WHERE ideas.org_id" not in captured["sql"]


async def test_workspace_data_source_failure_rolls_back_and_continues(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import workspace_data

    session = _FakeSession()
    _patch_uow(monkeypatch, session)

    def fail_source(_session, _payload, _ctx):
        raise RuntimeError("bad source")

    def after_source(_session, payload, _ctx):
        payload["sources"]["after"] = [{"id": "ok"}]

    monkeypatch.setattr(
        workspace_data,
        "_SOURCE_ADAPTERS",
        {
            "broken": workspace_data.WorkspaceDataSource(
                name="broken",
                description="broken",
                groups=("all",),
                handler=fail_source,
            ),
            "after": workspace_data.WorkspaceDataSource(
                name="after",
                description="after",
                groups=("all",),
                handler=after_source,
            ),
        },
    )

    payload = await workspace_data.query_workspace_data(
        sources=["broken", "after"],
        org_id="org-1",
    )

    assert session.rollbacks == 1
    assert payload["sources"]["broken"] == []
    assert payload["sources"]["after"] == [{"id": "ok"}]
    assert payload["counts"] == {"broken": 0, "after": 1}
    assert any(warning["source"] == "broken" for warning in payload["warnings"])


async def test_workspace_data_invalid_postgres_idea_id_uses_empty_sentinel(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import workspace_data

    session = _FakeSession(dialect="postgresql")
    _patch_uow(monkeypatch, session)
    captured = {}

    def capture_source(_session, payload, ctx):
        captured["idea_id"] = ctx.idea_id
        captured["org_id"] = ctx.org_id
        payload["sources"]["capture"] = []

    monkeypatch.setattr(
        workspace_data,
        "_SOURCE_ADAPTERS",
        {
            "capture": workspace_data.WorkspaceDataSource(
                name="capture",
                description="capture",
                groups=("all",),
                handler=capture_source,
            )
        },
    )

    payload = await workspace_data.query_workspace_data(
        sources=["capture"],
        org_id="44faf010-23ae-4aca-b6ad-2e1b574c717c",
        idea_id="0",
    )

    assert captured["org_id"] == "44faf010-23ae-4aca-b6ad-2e1b574c717c"
    assert captured["idea_id"] == workspace_data._ZERO_UUID
    assert payload["scope"]["idea_id"] == workspace_data._ZERO_UUID
    assert any("Invalid idea_id UUID" in warning["error"] for warning in payload["warnings"])
    assert session.rollbacks == 0


async def test_workspace_data_blank_postgres_idea_id_is_unscoped(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import workspace_data

    session = _FakeSession(dialect="postgresql")
    _patch_uow(monkeypatch, session)
    captured = {}

    def capture_source(_session, payload, ctx):
        captured["idea_id"] = ctx.idea_id
        captured["object_key"] = ctx.object_key
        payload["sources"]["capture"] = []

    monkeypatch.setattr(
        workspace_data,
        "_SOURCE_ADAPTERS",
        {
            "capture": workspace_data.WorkspaceDataSource(
                name="capture",
                description="capture",
                groups=("all",),
                handler=capture_source,
            )
        },
    )

    payload = await workspace_data.query_workspace_data(
        sources=["capture"],
        org_id="44faf010-23ae-4aca-b6ad-2e1b574c717c",
        idea_id="   ",
        object_key="",
    )

    assert captured["idea_id"] is None
    assert captured["object_key"] is None
    assert payload["scope"]["idea_id"] is None
    assert not any("Invalid idea_id UUID" in warning["error"] for warning in payload["warnings"])
    assert session.rollbacks == 0


def test_workspace_query_scope_blank_idea_is_explicitly_unscoped():
    from brain.systems.runs.tool_catalog.handlers import workspace_data
    from brain.systems.runs.execution_context import bind_agent_context

    with bind_agent_context({"idea_id": "current-idea", "user_id": " user-1 ", "org_id": " org-1 "}):
        scope = workspace_data._workspace_query_scope(
            idea_id="",
            object_key="  ",
            default_current_idea=True,
        )

    assert scope["idea_id"] is None
    assert scope["object_key"] is None
    assert scope["user_id"] == "user-1"
    assert scope["org_id"] == "org-1"


def test_workspace_query_scope_omitted_idea_defaults_to_current_thread():
    from brain.systems.runs.tool_catalog.handlers import workspace_data
    from brain.systems.runs.execution_context import bind_agent_context

    with bind_agent_context({"idea_id": "current-idea", "user_id": " user-1 ", "org_id": " org-1 "}):
        scope = workspace_data._workspace_query_scope(
            object_key="  ",
            default_current_idea=True,
        )

    assert scope["idea_id"] == "current-idea"
    assert scope["object_key"] is None
    assert scope["user_id"] == "user-1"
    assert scope["org_id"] == "org-1"


def test_workspace_query_scope_accepts_thread_url():
    from brain.systems.runs.tool_catalog.handlers import workspace_data
    from brain.systems.runs.execution_context import bind_agent_context

    with bind_agent_context({"idea_id": "current-idea", "user_id": "user-1", "org_id": "org-1"}):
        scope = workspace_data._workspace_query_scope(
            thread_url="https://illo.example.com/threads/shared-thread-1",
            default_current_idea=True,
        )

    assert scope["idea_id"] == "shared-thread-1"
    assert scope["user_id"] == "user-1"
    assert scope["org_id"] == "org-1"


async def test_workspace_data_runs_include_latest_final_answer_artifact():
    from brain.systems.runs.tool_catalog.handlers import workspace_data

    now = datetime(2026, 5, 6, 18, 30, tzinfo=timezone.utc)
    run = SimpleNamespace(
        id=7,
        created_at=now,
        started_at=now,
        completed_at=now,
        status="completed",
        thread_id="idea-1",
        user_id="user-1",
        metadata_={},
        model_policy={},
        input_message="What should Alex work on next?",
        context_summary="Reviewed workspace context.",
    )
    idea = SimpleNamespace(display_title="Current planning", title="Current planning")
    user = SimpleNamespace(name="Alex")

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Session:
        def __init__(self):
            self.calls = 0

        def execute(self, _stmt):
            self.calls += 1
            if self.calls == 1:
                return _Result([(run, idea, user)])
            return _Result([(7, "Alex has been actively polishing the Cortex thread flow.")])

    payload = {"sources": {}}
    await workspace_data._query_runs(
        _Session(),
        payload,
        start=None,
        end=None,
        org_id="org-1",
        user_id=None,
        person_ids=[],
        idea_id=None,
        run_id=None,
        search=None,
        limit=5,
    )

    assert (
        payload["sources"]["runs"][0]["output"]
        == "Alex has been actively polishing the Cortex thread flow."
    )
    assert payload["sources"]["runs"][0]["thread_url"].endswith("/threads/idea-1")
    assert payload["sources"]["runs"][0]["thread_reference"]["title"] == "Current planning"


async def test_workspace_data_runs_include_headless_child_summary_for_current_parent():
    from brain.systems.runs.tool_catalog.handlers import workspace_data

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    child = SimpleNamespace(
        id=49,
        parent_run_id=42,
        created_at=now,
        started_at=now,
        completed_at=now,
        status="completed",
        thread_id="headless-worker:42:repo-reader",
        user_id="user-1",
        metadata_={"origin": "spawn_worker", "worker_role": "repo_reader"},
        model_policy={},
        input_message="Read uwear-backend",
        context_summary="Read GitHub counts and Project Context.",
    )
    user = SimpleNamespace(name="Coordinator")
    captured = {}

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Session:
        def __init__(self):
            self.calls = 0

        def execute(self, stmt):
            self.calls += 1
            if self.calls == 1:
                captured["sql"] = str(stmt.compile(dialect=postgresql.dialect()))
                return _Result([(child, None, user)])
            return _Result([(49, '{"repo":"uwear-backend","issues":17,"prs":4,"project_context":"ready"}')])

    payload = {"sources": {}}
    await workspace_data._query_runs(
        _Session(),
        payload,
        start=None,
        end=None,
        org_id="org-1",
        user_id=None,
        person_ids=[],
        idea_id="idea-1",
        run_id=42,
        search=None,
        limit=20,
    )

    assert "agent_runs.thread_id =" in captured["sql"]
    assert "agent_runs.parent_run_id =" in captured["sql"]
    assert " OR " in captured["sql"]
    assert len(payload["sources"]["runs"]) == 1
    record = payload["sources"]["runs"][0]
    assert record["id"] == 49
    assert record["status"] == "completed"
    assert record["output"] == (
        '{"repo":"uwear-backend","issues":17,"prs":4,"project_context":"ready"}'
    )


def test_workspace_data_activity_items_sort_newest_signals_first():
    from brain.systems.runs.tool_catalog.handlers import workspace_data

    payload = {
        "sources": {
            "workspace_apps": [
                {
                    "id": "app-1",
                    "type": "workspace_app",
                    "updated_at": "2026-05-03T12:00:00+00:00",
                    "name": "Old Notes",
                    "description": "Older notes app work.",
                    "created_by_user_id": "user-1",
                    "provenance": {"table": "workspace_apps", "id": "app-1"},
                }
            ],
            "threads": [
                {
                    "id": 22,
                    "type": "thread_message",
                    "created_at": "2026-05-06T18:00:00+00:00",
                    "idea_id": "idea-2",
                    "thread_id": "idea-2",
                    "idea_title": "Live Cortex polish",
                    "thread_url": "https://illo.example.com/threads/idea-2",
                    "thread_route": "/threads/idea-2",
                    "thread_reference": {
                        "type": "thread_reference",
                        "thread_id": "idea-2",
                        "title": "Live Cortex polish",
                        "thread_url": "https://illo.example.com/threads/idea-2",
                    },
                    "content": "Let's fix the current thread activity answer.",
                    "user_id": "user-1",
                    "user_name": "Alex",
                    "provenance": {"table": "idea_threads", "id": 22},
                }
            ],
        }
    }

    items = workspace_data._build_activity_items(payload, limit=5)

    assert [item["source"] for item in items] == ["threads", "workspace_apps"]
    assert items[0]["title"] == "Live Cortex polish"
    assert items[0]["summary"] == "Let's fix the current thread activity answer."
    assert items[0]["thread_url"] == "https://illo.example.com/threads/idea-2"
    assert items[0]["thread_reference"]["thread_id"] == "idea-2"
