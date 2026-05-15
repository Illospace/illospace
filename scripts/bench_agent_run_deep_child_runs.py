#!/usr/bin/env python3
"""Offline smoke bench for native Deep parent/child AgentRuns."""

from __future__ import annotations

import json
import re
import sys
import asyncio
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.systems.runs.domain import AgentRunArtifact, AgentRunRequest, ArtifactType
from brain.systems.runs.engine import AsyncAgentRunEngine, RunRecipeResult, RunRuntime
from brain.systems.runs.recipes.deep import DeepRecipe
from brain.systems.runs.recipes.scout import ScoutRecipe
from brain.systems.runs.status import RunStatus
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow


class SmokeWorkerRecipe:
    async def execute(self, runtime: RunRuntime) -> RunRecipeResult:
        await runtime.activity("Smoke worker collecting evidence")
        await runtime.text_delta("worker evidence")
        return RunRecipeResult(
            output="worker evidence",
            artifacts=(
                AgentRunArtifact(
                    run_id=runtime.run.id,
                    root_run_id=runtime.run.root_run_id,
                    artifact_type=ArtifactType.WORKER_RESULT,
                    title="Smoke worker result",
                    payload={"status": "completed", "evidence": {"summary": "worker evidence"}},
                    text="worker evidence",
                ),
            ),
        )


async def _main_async() -> int:
    _patch_sqlite()
    _patch_offline_invocations()
    engine, session_factory = await _session_factory()
    try:
        async with session_factory() as session:
            run = await AsyncAgentRunEngine(
                session,
                recipes={"deep": DeepRecipe(), "scout": ScoutRecipe(), "worker": SmokeWorkerRecipe()},
            ).run(
                AgentRunRequest(
                    thread_id="bench-deep-child-runs",
                    message="Implement a tiny smoke task and verify evidence.",
                    profile="deep",
                    metadata={"deep_workers": [{"role": "smoke", "objective": "Produce deterministic worker evidence."}]},
                )
            )
            await session.commit()
            child_result = await session.scalars(select(AgentRunRow).where(AgentRunRow.parent_run_id == run.id))
            artifact_result = await session.scalars(select(AgentRunArtifactRow).where(AgentRunArtifactRow.run_id == run.id))
            child_runs = child_result.all()
            artifacts = artifact_result.all()
    finally:
        await engine.dispose()
    result = {
        "run_id": run.id,
        "status": run.status.value,
        "child_run_count": len(child_runs),
        "child_recipes": [row.recipe for row in child_runs],
        "parent_artifacts": [row.artifact_type for row in artifacts],
        "passed": (
            run.status == RunStatus.COMPLETED
            and len(child_runs) >= 2
            and "scout" in {row.recipe for row in child_runs}
            and "worker" in {row.recipe for row in child_runs}
            and "verifier_evidence" in {row.artifact_type for row in artifacts}
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


def main() -> int:
    return asyncio.run(_main_async())


def _patch_offline_invocations() -> None:
    import brain.systems.runs.recipes.deep as deep_module
    import brain.systems.runs.recipes.phase_barrier as phase_barrier_module

    def _invoke(spec):
        session_id = str(getattr(spec, "session_id", "") or "")
        if "phase-review" in session_id:
            return SimpleNamespace(
                output=json.dumps({"summary": "Offline phase review passed.", "revisions": []}),
                success=True,
            )
        return SimpleNamespace(output="Deep completed using native AgentRun workers.", success=True)

    deep_module.invoke_direct_agent = _invoke
    phase_barrier_module.invoke_direct_agent = _invoke


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        for table in (AgentRunRow.__table__, AgentRunEventRow.__table__, AgentRunArtifactRow.__table__):
            await conn.execute(CreateTable(table, if_not_exists=True))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _patch_sqlite() -> None:
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"
    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_agent_run_smoke_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result).replace("NOW()", "CURRENT_TIMESTAMP")
        return result

    patched._agent_run_smoke_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


if __name__ == "__main__":
    raise SystemExit(main())
