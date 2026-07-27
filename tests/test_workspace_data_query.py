from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
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


async def test_workspace_tool_calls_project_legacy_structured_failures_safely():
    from brain.systems.runs.failures import DEFAULT_FAILED_RUN_MESSAGE
    from brain.systems.runs.tool_catalog.handlers import workspace_data

    raw_diagnostic = "legacy handler traceback token=workspace-tool-secret"
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    event = SimpleNamespace(
        id=11,
        run_id=7,
        event_type="run.tool_completed",
        payload={
            "tool_name": "manage_idea",
            "args": {"action": "update"},
            "result": json.dumps({"status": "error", "error": raw_diagnostic}),
        },
        created_at=now,
    )
    run = SimpleNamespace(thread_id="idea-1", status="completed")

    class Result:
        def all(self):
            return [(event, run, None)]

    class StubSession:
        def execute(self, _stmt):
            return Result()

    payload = {"sources": {}}
    await workspace_data._query_tool_calls(
        StubSession(),
        payload,
        start=None,
        end=None,
        org_id=None,
        user_id=None,
        person_ids=[],
        idea_id=None,
        search=None,
        limit=10,
    )

    serialized = json.dumps(payload)
    assert raw_diagnostic not in serialized
    assert payload["sources"]["tool_calls"][0]["result"] == DEFAULT_FAILED_RUN_MESSAGE
    assert payload["sources"]["tool_calls"][0]["failure"]["status"] == "failed"


async def test_workspace_tool_calls_keep_team_scope_and_report_current_run_summary(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import workspace_data

    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    teammate_event = SimpleNamespace(
        id=12,
        run_id=8,
        event_type="run.tool_completed",
        payload={
            "tool_name": "read_file",
            "args": {"path": "README.md"},
            "result": "read",
            "side_effect": "read",
        },
        created_at=now - timedelta(seconds=5),
    )
    current_run_write = SimpleNamespace(
        id=11,
        run_id=7,
        event_type="run.tool_completed",
        payload={
            "tool_name": "write_file",
            "args": {"path": "README.md"},
            "result": "wrote",
            "side_effect": "file_write",
            "is_write": True,
        },
        created_at=now - timedelta(seconds=80),
    )
    teammate_run = SimpleNamespace(
        id=8,
        thread_id="idea-team",
        status="completed",
    )
    current_run = SimpleNamespace(id=7, thread_id="idea-1", status="running")

    class Result:
        def all(self):
            return [
                (teammate_event, teammate_run, None),
                (current_run_write, current_run, None),
            ]

    captured = {}

    class StubSession:
        def execute(self, stmt):
            captured["listing_sql"] = str(
                stmt.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
            return Result()

        def scalar(self, stmt):
            captured["summary_sql"] = str(
                stmt.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
            return current_run_write

    monkeypatch.setattr(workspace_data, "_now_utc", lambda: now)
    payload = {"sources": {}}
    await workspace_data._query_tool_calls(
        StubSession(),
        payload,
        start=None,
        end=None,
        org_id=None,
        user_id=None,
        person_ids=[],
        idea_id=None,
        search=None,
        limit=10,
        run_id=7,
    )

    assert [row["run_id"] for row in payload["sources"]["tool_calls"]] == [8, 7]
    assert payload["sources"]["tool_calls"][0]["side_effect"] == "read"
    assert payload["sources"]["tool_calls"][0]["is_write"] is False
    assert payload["sources"]["tool_calls"][1]["side_effect"] == "file_write"
    assert payload["sources"]["tool_calls"][1]["is_write"] is True
    assert "agent_runs.id = 7" not in captured["listing_sql"]
    assert "LIMIT 10" in captured["listing_sql"]
    assert "agent_run_events.run_id = 7" in captured["summary_sql"]
    assert "run.tool_completed" in captured["summary_sql"]
    assert "run.tool_failed" in captured["summary_sql"]
    assert "read" in captured["summary_sql"]
    assert (
        "ORDER BY agent_run_events.created_at DESC, "
        "agent_run_events.id DESC" in captured["summary_sql"]
    )
    assert "LIMIT 1" in captured["summary_sql"]
    assert payload["tool_call_summary"] == {
        "run_id": 7,
        "last_write_tool_call_at": (now - timedelta(seconds=80)).isoformat(),
        "seconds_since_last_write_tool_call": 80,
    }


async def test_workspace_tool_call_source_forwards_current_run_for_summary(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import workspace_data

    captured = {}

    async def query_tool_calls(_session, _payload, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(workspace_data, "_query_tool_calls", query_tool_calls)
    ctx = workspace_data.WorkspaceDataQueryContext(
        start=None,
        end=None,
        org_id="org-1",
        user_id="user-1",
        person_ids=[],
        idea_id=None,
        run_id=507,
        domain_id=None,
        cycle_id=None,
        object_key=None,
        query=None,
        search=None,
        include_archived=False,
        limit=10,
        offset=0,
    )

    await workspace_data._run_tool_calls(object(), {"sources": {}}, ctx)

    assert captured["run_id"] == 507


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


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return self._rows


def _select_page(rows, stmt):
    offset_clause = getattr(stmt, "_offset_clause", None)
    limit_clause = getattr(stmt, "_limit_clause", None)
    offset = int(getattr(offset_clause, "value", 0) or 0)
    limit = int(getattr(limit_clause, "value", len(rows)) or len(rows))
    return list(rows)[offset : offset + limit]


async def test_read_cycles_pages_to_complete_history_and_watermark_is_one_bounded_query(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import workspace_data

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    cycle = SimpleNamespace(
        id=7,
        name="GitHub Reflex",
        user_id="user-1",
        org_id="org-1",
        deleted_at=None,
    )
    user = SimpleNamespace(name="Reflex owner")
    runs = []
    for run_id in range(5, 0, -1):
        completed_at = now - timedelta(minutes=5 - run_id)
        run = SimpleNamespace(
            id=run_id,
            cycle_id=cycle.id,
            revision_id=None,
            created_at=completed_at,
            scheduled_for=completed_at,
            started_at=completed_at,
            completed_at=completed_at,
            status="completed",
            error=None,
            skip_reason=None,
            idea_id=None,
            run_id=100 + run_id,
            prompt_snapshot="Check GitHub events",
            guidance_snapshot=[],
            output_targets_snapshot=[],
            context_snapshot={},
            self_review_summary="Healthy evidence",
        )
        runs.append((run, cycle, user, None))

    class CycleHistorySession(_FakeSession):
        def __init__(self):
            super().__init__()
            self.statements = []

        def execute(self, stmt):
            self.statements.append(stmt)
            return _RowsResult(_select_page(runs, stmt))

    history_session = CycleHistorySession()
    _patch_uow(monkeypatch, history_session)
    monkeypatch.setattr(
        workspace_data,
        "_SOURCE_ADAPTERS",
        {
            "cycle_runs": workspace_data.WorkspaceDataSource(
                name="cycle_runs",
                description="Cycle history",
                groups=("cycles",),
                handler=workspace_data._run_cycle_runs,
            )
        },
    )

    seen_ids = []
    cursor = None
    pages = []
    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}):
        while True:
            page = json.loads(
                await workspace_data._handle_read_cycles(
                    cycle_id=cycle.id,
                    limit=2,
                    cursor=cursor,
                )
            )
            pages.append(page)
            seen_ids.extend(item["id"] for item in page["sources"]["cycle_runs"])
            cursor = page["next_page"]
            if cursor is None:
                break

    assert seen_ids == [5, 4, 3, 2, 1]
    assert pages[0]["truncated"] is True
    assert pages[0]["next_page"]
    assert pages[-1]["truncated"] is False
    assert pages[-1]["evidence_health"] == {"status": "ok", "completeness": "complete"}

    class WatermarkSession(_FakeSession):
        def __init__(self):
            super().__init__()
            self.statements = []

        def execute(self, stmt):
            self.statements.append(stmt)
            return _RowsResult([(runs[0][0], cycle)])

    watermark_session = WatermarkSession()
    _patch_uow(monkeypatch, watermark_session)
    with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}):
        watermark = json.loads(
            await workspace_data._handle_read_cycles(
                cycle_id=cycle.id,
                last_completed_run=True,
            )
        )

    assert len(watermark_session.statements) == 1
    statement = watermark_session.statements[0]
    assert int(statement._limit_clause.value) == 1
    assert "cycle_runs.completed_at IS NOT NULL" in str(statement)
    assert watermark["last_completed_run"]["completed_at"] == runs[0][0].completed_at.isoformat()
    assert watermark["evidence_health"] == {"status": "ok", "completeness": "complete"}
    assert "truncated" not in watermark


async def test_domain_tracker_and_event_feed_page_to_evidence_health_ok(monkeypatch):
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers import workspace_data

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    domain = SimpleNamespace(id=1, name="Tracker", archived_at=None)
    object_type = SimpleNamespace(key="ticket", name="Ticket")
    record_rows = [
        (
            SimpleNamespace(
                id=record_id,
                domain_id=1,
                created_at=now - timedelta(minutes=record_id),
                updated_at=now - timedelta(minutes=record_id),
                title=f"Ticket {record_id}",
                data={"status": "open"},
                version=1,
            ),
            domain,
            object_type,
        )
        for record_id in range(5, 0, -1)
    ]
    event_domain = SimpleNamespace(id=38, name="Event feed")
    event_rows = [
        (
            SimpleNamespace(
                id=event_id,
                created_at=now - timedelta(minutes=event_id),
                event_type="github.issue.updated",
                domain_id=38,
                record_id=None,
                relation_id=None,
                actor_kind="agent",
                actor_id="reflex",
                run_id=200 + event_id,
                idea_id=None,
                reason="Observed GitHub update",
            ),
            event_domain,
        )
        for event_id in range(5, 0, -1)
    ]

    class DomainPagingSession(_FakeSession):
        def __init__(self, rows):
            super().__init__()
            self.rows = rows

        def execute(self, stmt):
            return _RowsResult(_select_page(self.rows, stmt))

    async def read_all(source, handler, rows, domain_id):
        _patch_uow(monkeypatch, DomainPagingSession(rows))
        monkeypatch.setattr(
            workspace_data,
            "_SOURCE_ADAPTERS",
            {
                source: workspace_data.WorkspaceDataSource(
                    name=source,
                    description=source,
                    groups=("records",),
                    handler=handler,
                )
            },
        )
        seen = []
        cursor = None
        final_page = None
        with bind_agent_context({"user_id": "user-1", "org_id": "org-1"}):
            while True:
                final_page = json.loads(
                    await workspace_data._handle_read_workspace_records(
                        domain_id=domain_id,
                        limit=2,
                        cursor=cursor,
                    )
                )
                seen.extend(item["id"] for item in final_page["sources"][source])
                cursor = final_page["next_page"]
                if cursor is None:
                    return seen, final_page

    record_ids, record_final = await read_all(
        "domain_records",
        workspace_data._run_domain_records,
        record_rows,
        1,
    )
    event_ids, event_final = await read_all(
        "domain_events",
        workspace_data._run_domain_events,
        event_rows,
        38,
    )

    assert record_ids == [5, 4, 3, 2, 1]
    assert event_ids == [5, 4, 3, 2, 1]
    assert record_final["evidence_health"] == {"status": "ok", "completeness": "complete"}
    assert event_final["evidence_health"] == {"status": "ok", "completeness": "complete"}
