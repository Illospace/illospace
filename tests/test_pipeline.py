"""AgentRun persistence tests."""

from __future__ import annotations

import re
from collections.abc import Callable, AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)


@pytest.fixture
async def session_factory() -> AsyncIterator[Callable[[], AsyncSession]]:
    pytest.importorskip("aiosqlite")
    _patch_sqlite_for_agent_run_tables()
    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as connection:
        for table in [
            AgentRunRow.__table__,
            AgentRunEventRow.__table__,
            AgentRunArtifactRow.__table__,
        ]:
            await connection.execute(CreateTable(table, if_not_exists=True))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_historical_deep_profile_and_scout_recipe_rows_remain_readable(session_factory):
    from brain.systems.runs.domain import RunProfile, RunRecipe
    from brain.systems.runs.store import AsyncAgentRunStore, to_domain

    session = session_factory()
    rows = [
        AgentRunRow(
            thread_id="historical-deep-worker",
            profile="deep",
            recipe="worker",
            status="completed",
            input_message="Historical deep worker run.",
        ),
        AgentRunRow(
            thread_id="historical-deep-recipe",
            profile="deep",
            recipe="deep",
            status="completed",
            input_message="Historical deep recipe run.",
        ),
        AgentRunRow(
            thread_id="historical-fast-scout",
            profile="fast",
            recipe="scout",
            status="completed",
            input_message="Historical scout run.",
        ),
    ]
    session.add_all(rows)
    await session.commit()
    row_ids = [row.id for row in rows]
    await session.close()

    async with session_factory() as stored_session:
        store = AsyncAgentRunStore(stored_session)
        loaded = [to_domain(await store.require_run(row_id)) for row_id in row_ids]

    assert [(run.profile, run.recipe) for run in loaded] == [
        (RunProfile.DEEP, RunRecipe.WORKER),
        (RunProfile.DEEP, RunRecipe.DEEP),
        (RunProfile.FAST, RunRecipe.SCOUT),
    ]


async def test_scout_metadata_is_admitted_as_fast_and_executes_registered_recipe(
    session_factory,
    monkeypatch,
):
    from brain.systems.runs.domain import RunProfile, RunRecipe
    from brain.systems.runs.engine import AsyncAgentRunEngine, RunRecipeResult
    from brain.systems.runs.recipes import default_recipes
    from brain.systems.runs.recipes.fast import FastRecipe
    from brain.systems.runs.status import RunStatus
    from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work

    executed_recipes = []

    async def execute_fast(_self, runtime):
        executed_recipes.append(runtime.request.normalized_recipe)
        return RunRecipeResult(output="Scout request reached fast.")

    monkeypatch.setattr(FastRecipe, "execute", execute_fast)
    session = session_factory()
    admission = await admit_work(
        session,
        WorkIntakeEvent(
            source="chat",
            event_type="chat.room_message_mention",
            org_id="org-1",
            actor={"id": "user-1", "org_id": "org-1", "internal": False},
            target={"conversation_id": "conv-1"},
            payload={
                "message": "Handle this stale scout request.",
                "metadata": {
                    "recipe": "scout",
                    "chat_trigger": {
                        "conversation_id": "conv-1",
                        "message_id": 22,
                    },
                },
            },
        ),
    )

    assert admission.ok is True
    row = await session.get(AgentRunRow, admission.run_id)
    assert row is not None
    assert (row.profile, row.recipe) == (
        RunProfile.FAST.value,
        RunRecipe.FAST.value,
    )

    completed = await AsyncAgentRunEngine(
        session,
        recipes=default_recipes(),
    ).run_existing(admission.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert executed_recipes == [RunRecipe.FAST]


async def test_child_run_can_use_headless_thread_without_bypassing_store(session_factory):
    from brain.systems.runs.domain import AgentRunRequest, RunProfile, RunRecipe
    from brain.systems.runs.store import AsyncAgentRunStore

    session = session_factory()
    store = AsyncAgentRunStore(session)
    parent = await store.create_run(
        AgentRunRequest(
            org_id="org-1",
            thread_id="idea-1",
            message="Keep working.",
            profile=RunProfile.FAST,
            recipe=RunRecipe.FAST,
        )
    )

    child = await store.create_child_run(
        parent,
        recipe=RunRecipe.WORKER,
        profile=RunProfile.FAST,
        step_key="spawn_worker:report",
        thread_id="headless-worker:1:report",
        message="Report the blocker.",
        metadata={"headless": True},
    )
    same_child = await store.create_child_run(
        parent,
        recipe=RunRecipe.WORKER,
        profile=RunProfile.FAST,
        step_key="spawn_worker:report",
        thread_id="headless-worker:1:report",
        message="Report the blocker again.",
    )
    await session.commit()

    assert same_child.id == child.id
    assert child.root_run_id == parent.id
    rows = (await session.scalars(select(AgentRunRow).order_by(AgentRunRow.id.asc()))).all()
    assert [row.thread_id for row in rows] == ["idea-1", "headless-worker:1:report"]
    assert rows[1].metadata_["headless"] is True
    assert rows[1].metadata_["parent_step_key"] == "spawn_worker:report"

    child_created_events = (await session.scalars(
        select(AgentRunEventRow)
        .where(AgentRunEventRow.run_id == parent.id, AgentRunEventRow.event_type == "run.child_created")
        .order_by(AgentRunEventRow.sequence_no.asc())
    )).all()
    assert [event.payload["child_run_id"] for event in child_created_events] == [child.id]


async def test_visible_run_fetch_overfetches_when_headless_rows_are_newer(session_factory):
    from brain.systems.runs.domain import AgentRunRequest, RunProfile, RunRecipe
    from brain.systems.runs.store import AsyncAgentRunStore
    from brain.systems.runs.visibility import fetch_visible_run_rows

    session = session_factory()
    store = AsyncAgentRunStore(session)
    visible = await store.create_run(
        AgentRunRequest(
            org_id="org-1",
            thread_id="idea-1",
            message="Visible run.",
            profile=RunProfile.FAST,
            recipe=RunRecipe.FAST,
        )
    )
    await store.create_run(
        AgentRunRequest(
            org_id="org-1",
            thread_id="headless-worker:1:report",
            message="Hidden report.",
            profile=RunProfile.FAST,
            recipe=RunRecipe.WORKER,
            metadata={"headless": True},
        )
    )

    rows = await fetch_visible_run_rows(
        session,
        select(AgentRunRow).order_by(AgentRunRow.id.desc()),
        limit=1,
        batch_size=1,
    )

    assert [row.id for row in rows] == [visible.id]


def _patch_sqlite_for_agent_run_tables() -> None:
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"
    original = SQLiteDDLCompiler.get_column_default_string

    if getattr(original, "_agent_run_graph_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
        return result

    patched._agent_run_graph_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched
