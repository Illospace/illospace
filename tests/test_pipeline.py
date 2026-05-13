"""AgentRun graph persistence tests for Deep graph-shaped work."""

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


async def test_deep_graph_shape_is_agent_run_children_and_artifacts(session_factory):
    from brain.systems.runs.domain import AgentRunArtifact, AgentRunRequest, RunProfile, RunRecipe
    from brain.systems.runs.store import AsyncAgentRunStore

    session = session_factory()
    store = AsyncAgentRunStore(session)
    parent = await store.create_run(
        AgentRunRequest(
            thread_id="idea-1",
            message="Investigate, implement, and verify the cleanup.",
            profile=RunProfile.DEEP,
            recipe=RunRecipe.DEEP,
        )
    )

    investigate = await store.create_child_run(
        parent,
        recipe=RunRecipe.WORKER,
        profile=RunProfile.DEEP,
        step_key="worker:1:investigate",
        message="Gather context and evidence.",
        metadata={
            "worker_role": "investigate",
            "worker_scope": {
                "role": "investigate",
                "objective": "Gather context and evidence.",
                "expected_artifacts": ["file_observation", "worker_result"],
                "risk_level": "low",
            },
        },
    )
    same_investigate = await store.create_child_run(
        parent,
        recipe=RunRecipe.WORKER,
        profile=RunProfile.DEEP,
        step_key="worker:1:investigate",
        message="Gather context and evidence.",
    )
    execute = await store.create_child_run(
        parent,
        recipe=RunRecipe.WORKER,
        profile=RunProfile.DEEP,
        step_key="worker:2:execute",
        message="Apply the scoped change.",
        metadata={
            "worker_role": "execute",
            "worker_scope": {
                "role": "execute",
                "objective": "Apply the scoped change.",
                "expected_artifacts": ["worker_result"],
                "risk_level": "medium",
            },
        },
    )

    await store.append_artifact(
        AgentRunArtifact(
            run_id=parent.id,
            root_run_id=parent.root_run_id,
            artifact_type="deep_plan",
            title="Deep plan",
            payload={
                "workers": [
                    {"role": "investigate", "child_run_id": investigate.id},
                    {"role": "execute", "child_run_id": execute.id},
                ]
            },
        )
    )
    await store.append_artifact(
        AgentRunArtifact(
            run_id=investigate.id,
            root_run_id=parent.root_run_id,
            artifact_type="worker_result",
            title="Investigate result",
            text="Found the canonical AgentRun surface.",
            payload={"status": "completed", "evidence": {"artifact_types": ["file_observation"]}},
        )
    )
    await session.commit()

    assert same_investigate.id == investigate.id
    children = await store.child_runs(parent.id)
    assert [child.id for child in children] == [investigate.id, execute.id]
    assert {child.parent_run_id for child in children} == {parent.id}
    assert {child.root_run_id for child in children} == {parent.id}
    assert [child.recipe for child in children] == ["worker", "worker"]
    assert children[0].metadata_["parent_step_key"] == "worker:1:investigate"
    assert children[0].metadata_["worker_scope"]["role"] == "investigate"

    child_created_events = (await session.scalars(
        select(AgentRunEventRow)
        .where(AgentRunEventRow.run_id == parent.id, AgentRunEventRow.event_type == "run.child_created")
        .order_by(AgentRunEventRow.sequence_no.asc())
    )).all()
    assert [event.payload["child_run_id"] for event in child_created_events] == [investigate.id, execute.id]

    parent_artifacts = (await session.scalars(
        select(AgentRunArtifactRow)
        .where(AgentRunArtifactRow.run_id == parent.id)
        .order_by(AgentRunArtifactRow.id.asc())
    )).all()
    assert [artifact.artifact_type for artifact in parent_artifacts] == ["deep_plan"]
    assert parent_artifacts[0].payload["workers"][0]["child_run_id"] == investigate.id

    worker_artifacts = (await session.scalars(
        select(AgentRunArtifactRow)
        .where(AgentRunArtifactRow.run_id == investigate.id)
        .order_by(AgentRunArtifactRow.id.asc())
    )).all()
    assert [artifact.artifact_type for artifact in worker_artifacts] == ["worker_result"]
    assert worker_artifacts[0].text == "Found the canonical AgentRun surface."


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
