#!/usr/bin/env python3
"""Offline smoke bench for native Deep parent/child AgentRuns."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.systems.runs.domain import AgentRunArtifact, AgentRunRequest, ArtifactType
from brain.systems.runs.engine import AgentRunEngine, RunRecipeResult, RunRuntime
from brain.systems.runs.recipes.deep import DeepRecipe
from brain.systems.runs.recipes.scout import ScoutRecipe
from brain.systems.runs.status import RunStatus
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow


class SmokeWorkerRecipe:
    def execute(self, runtime: RunRuntime) -> RunRecipeResult:
        runtime.activity("Smoke worker collecting evidence")
        runtime.text_delta("worker evidence")
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


def main() -> int:
    _patch_sqlite()
    session = _session()
    engine = AgentRunEngine(
        session,
        recipes={"deep": DeepRecipe(), "scout": ScoutRecipe(), "worker": SmokeWorkerRecipe()},
    )
    run = engine.run(
        AgentRunRequest(
            thread_id="bench-deep-child-runs",
            message="Implement a tiny smoke task and verify evidence.",
            profile="deep",
            metadata={"deep_workers": [{"role": "smoke", "objective": "Produce deterministic worker evidence."}]},
        )
    )
    session.commit()
    child_runs = session.scalars(select(AgentRunRow).where(AgentRunRow.parent_run_id == run.id)).all()
    artifacts = session.scalars(select(AgentRunArtifactRow).where(AgentRunArtifactRow.run_id == run.id)).all()
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


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for table in (AgentRunRow.__table__, AgentRunEventRow.__table__, AgentRunArtifactRow.__table__):
        table.create(engine, checkfirst=True)
    return sessionmaker(bind=engine, expire_on_commit=False)()


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
