#!/usr/bin/env python3
"""Offline smoke bench for a Fast AgentRun README lookup."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path
from time import perf_counter

from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.systems.runs.domain import AgentRunRequest
from brain.systems.runs.engine import AsyncAgentRunEngine, RunRecipeResult, RunRuntime
from brain.systems.runs.status import RunStatus
from brain.systems.runs.stream import RunStream
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow


class RecordingStream(RunStream):
    def __init__(self):
        self.started_at = perf_counter()
        self.first_activity_at: float | None = None
        self.first_delta_at: float | None = None

    def publish(self, event_type: str, payload: dict) -> None:
        now = perf_counter()
        if event_type == "run.activity" and self.first_activity_at is None:
            self.first_activity_at = now - self.started_at
        if event_type == "run.text_delta" and self.first_delta_at is None:
            self.first_delta_at = now - self.started_at


class ReadmeRecipe:
    async def execute(self, runtime: RunRuntime) -> RunRecipeResult:
        await runtime.activity("Reading README", skill_loaded=False, blocking_verification=False)
        root = Path(str(runtime.request.workspace_ref["workspace_root"]))
        text = (root / "README.md").read_text(encoding="utf-8")
        await runtime.text_delta(text[:160])
        return RunRecipeResult(output=text)


async def _main_async() -> int:
    _patch_sqlite()
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        (workspace / "README.md").write_text("# Demo\n\nFast path README smoke.\n", encoding="utf-8")
        engine, session_factory = await _session_factory()
        stream = RecordingStream()
        try:
            async with session_factory() as session:
                run = await AsyncAgentRunEngine(session, recipes={"fast": ReadmeRecipe()}, stream=stream).run(
                    AgentRunRequest(
                        org_id="bench",
                        thread_id="bench-fast-readme",
                        message="What is in the README?",
                        profile="fast",
                        workspace_ref={"workspace_root": str(workspace)},
                        model_policy={"tier": "high", "thinking": "high"},
                    )
                )
                await session.commit()
        finally:
            await engine.dispose()
    result = {
        "run_id": run.id,
        "status": run.status.value,
        "first_activity_sec": stream.first_activity_at,
        "first_text_delta_sec": stream.first_delta_at,
        "passed": (
            run.status == RunStatus.COMPLETED
            and (stream.first_activity_at or 999) < 2
            and (stream.first_delta_at or 999) < 8
        ),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


def main() -> int:
    return asyncio.run(_main_async())


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
