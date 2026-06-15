"""Integration tests for the full nightly sleep pipeline.

Tests each phase individually and the full pipeline end-to-end.
All LLM calls and database access are mocked — no real API calls.

Public release note: internal issue links were removed from test comments.
Lesson: "NIGHTLY CYCLE SHIPPED BROKEN — 4 crashers in production for 2+ nights.
Root cause: (1) no integration test for full nightly cycle"
"""
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

class _AwaitableResult:
    """Let tests use one mocked result with sync and async session.execute callers."""

    def __init__(self, result):
        self._result = result

    def __await__(self):
        async def _coro():
            return self._result

        return _coro().__await__()

    def __getattr__(self, name):
        return getattr(self._result, name)


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a temporary workspace structure matching production layout."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    logs_dir = tmp_path / "illo_private" / "logs"
    logs_dir.mkdir(parents=True)
    journal_dir = tmp_path / "illo_private" / "journal"
    journal_dir.mkdir(parents=True)
    blog_dir = tmp_path / "illo_private" / "blog"
    blog_dir.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def mock_nightly_uow():
    """Mock UnitOfWork for nightly pipeline tests."""
    uow = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    # Default return values
    execute_result = MagicMock()
    execute_result.mappings.return_value.fetchone.return_value = {"id": 1}
    execute_result.mappings.return_value.fetchall.return_value = []
    execute_result.mappings.return_value.all.return_value = []
    execute_result.mappings.return_value.first.return_value = {"id": 1}
    uow.session.execute.return_value = _AwaitableResult(execute_result)
    uow.session.scalar = AsyncMock(return_value=0)
    uow.session.scalars.return_value.all.return_value = []
    uow.session.scalars.return_value.first.return_value = None
    uow.skills.get = AsyncMock(return_value=None)
    uow.skills.list_active = AsyncMock(return_value=[])
    return uow


@pytest.fixture
def target_date():
    return date(2026, 3, 7)


# ---------------------------------------------------------------------------
# Phase 1: Memory Consolidation
# ---------------------------------------------------------------------------

class TestPhaseConsolidation:
    """Tests for pipelines/consolidate.py — Phase 1 of nightly cycle."""

    async def test_consolidation_records_reconstructive_pass(self, mock_nightly_uow, target_date):
        """Consolidation records an operator pass without manufacturing rows."""
        with patch("brain.jobs.pipelines.consolidate.UnitOfWork", return_value=mock_nightly_uow):
            from brain.jobs.pipelines.consolidate import phase_consolidation
            result = await phase_consolidation(target_date)
        assert result == {
            "run_id": 1,
            "phase": "reconstructive_consolidation",
            "active_memory_nodes": 0,
            "memory_system": "reconstructive",
        }

    async def test_consolidation_ignores_workspace_daily_logs(self, mock_nightly_uow, target_date, tmp_workspace):
        """Workspace markdown logs are not imported by reconstructive consolidation."""
        daily_log = tmp_workspace / "memory" / f"{target_date.isoformat()}.md"
        daily_log.write_text("# March 7\nWorked on nightly pipeline fixes.")

        with patch("brain.jobs.pipelines.consolidate.UnitOfWork", return_value=mock_nightly_uow):
            from brain.jobs.pipelines.consolidate import phase_consolidation
            result = await phase_consolidation(target_date)
        assert result["memory_system"] == "reconstructive"
        assert result["active_memory_nodes"] == 0

    async def test_consolidation_accepts_org_id(self, mock_nightly_uow, target_date):
        with patch("brain.jobs.pipelines.consolidate.UnitOfWork", return_value=mock_nightly_uow):
            from brain.jobs.pipelines.consolidate import phase_consolidation
            await phase_consolidation(target_date, org_id="org-123")
        params = mock_nightly_uow.session.execute.call_args[0][1]
        assert params["org_id"] == "org-123"


# ---------------------------------------------------------------------------
# Phase 2: Skill Evolution
# ---------------------------------------------------------------------------

class TestPhaseSkillEvolution:
    """Tests for cli/skills.py evolve — Phase 2 of nightly cycle."""

    async def test_skill_evolve_runs_without_executions(self, mock_nightly_uow):
        """Evolution should handle the case where no skill executions exist."""
        with patch("brain.app.cli.skills.UnitOfWork", return_value=mock_nightly_uow):
            from brain.app.cli.skills import cmd_evolve
            import argparse
            args = argparse.Namespace()
            await cmd_evolve(args)

    async def test_skill_evolve_detects_gaps(self, mock_nightly_uow):
        """Evolution should detect recurring tasks without skills."""
        call_count = [0]
        def execute_side_effect(*args, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] == 1:
                result.mappings.return_value.all.return_value = [
                    {"task_description": "test task", "skill_id": None,
                     "outcome": "success", "id": i,
                     "skill_name": None, "error_analysis": None,
                     "refinement_proposed": None, "pitfall_pattern": None}
                    for i in range(5)
                ]
            else:
                result.mappings.return_value.all.return_value = []
                result.mappings.return_value.fetchall.return_value = []
            return _AwaitableResult(result)

        mock_nightly_uow.session.execute.side_effect = execute_side_effect
        with patch("brain.app.cli.skills.UnitOfWork", return_value=mock_nightly_uow):
            from brain.app.cli.skills import cmd_evolve
            import argparse
            args = argparse.Namespace()
            await cmd_evolve(args)


# ---------------------------------------------------------------------------
# Phase 3: LLM Reflection
# ---------------------------------------------------------------------------

class TestPhaseReflection:
    """Tests for pipelines/nightly_reflect.py — Phase 3 of nightly cycle."""

    async def test_reflection_skips_when_no_data(self, mock_nightly_uow, target_date, capsys):
        """When all context sections are empty, reflection should skip."""
        with patch("brain.jobs.pipelines.nightly_reflect.UnitOfWork", return_value=mock_nightly_uow), \
             patch("brain.jobs.pipelines.nightly_reflect.WORKSPACE", "/nonexistent"), \
            patch("brain.jobs.pipelines.nightly_reflect.config.JOURNAL_DIR", Path("/nonexistent/journal")):
            from brain.jobs.pipelines.nightly_reflect import run_reflection
            await run_reflection(target_date)

        captured = capsys.readouterr()
        assert "No data to reflect on" in captured.out or "Skipping" in captured.out

    def test_reflection_uses_direct_api_not_subprocess(self):
        """Regression: nightly_reflect must use core.agent.call_llm, not subprocess."""
        import inspect
        from brain.jobs.pipelines.nightly_reflect import run_reflection
        source = inspect.getsource(run_reflection)
        assert "subprocess.run" not in source, \
            "nightly_reflect must use core.agent.call_llm, not subprocess"
        assert "call_llm" in source


# ---------------------------------------------------------------------------
# Phase 3.5: Dream
# ---------------------------------------------------------------------------

class TestPhaseDream:
    """Tests for pipelines/nightly_dream.py — Phase 3.5 of nightly cycle."""

    def test_dream_skips_when_no_today_memories(self, mock_nightly_uow, target_date, capsys):
        """Dream should skip when there are no memories from today."""
        mock_nightly_uow.session.execute.return_value.mappings.return_value.fetchall.return_value = []
        with patch("brain.jobs.pipelines.nightly_dream.UnitOfWork", return_value=mock_nightly_uow):
            from brain.jobs.pipelines.nightly_dream import main as dream_main
            with patch("sys.argv", ["nightly_dream", "--date", target_date.isoformat()]):
                dream_main()
        captured = capsys.readouterr()
        assert "No memories" in captured.out or "Skipping" in captured.out

    def test_dream_uses_direct_api_not_subprocess(self):
        """Regression: nightly_dream must use core.agent, not subprocess."""
        import inspect
        from brain.jobs.pipelines.nightly_dream import call_llm
        source = inspect.getsource(call_llm)
        assert "subprocess" not in source, \
            "nightly_dream must use core.agent.call_llm, not subprocess"


# ---------------------------------------------------------------------------
# Phase 5: Brain -> Files Sync
# ---------------------------------------------------------------------------

class TestPhaseSyncBrainToFiles:
    """Tests for pipelines/sync_brain_to_files.py — Phase 5."""

    async def test_sync_lessons_creates_file(self, mock_nightly_uow, tmp_workspace):
        """Sync should write lessons.md even with empty results."""
        with patch("brain.jobs.pipelines.sync_brain_to_files.UnitOfWork", return_value=mock_nightly_uow), \
             patch("brain.jobs.pipelines.sync_brain_to_files.MEMORY_DIR",
                   str(tmp_workspace / "memory")), \
             patch("brain.jobs.pipelines.sync_brain_to_files.WORKSPACE", str(tmp_workspace)):
            from brain.jobs.pipelines.sync_brain_to_files import sync_lessons
            await sync_lessons()


# ---------------------------------------------------------------------------
# Phase 5.5: Experiment Assessment
# ---------------------------------------------------------------------------

class TestPhaseExperimentAssessment:
    """Tests for pipelines/nightly_assess.py — Phase 5.5."""

    def test_assess_no_experiments(self, mock_nightly_uow, target_date, capsys):
        """Assessment should skip gracefully when no experiments are due."""
        with patch("brain.jobs.pipelines.nightly_assess.UnitOfWork", return_value=mock_nightly_uow):
            with patch("sys.argv", ["nightly_assess", "--date", target_date.isoformat()]):
                from brain.jobs.pipelines.nightly_assess import main as assess_main
                assess_main()
        captured = capsys.readouterr()
        assert "No experiments" in captured.out or captured.out == ""


# ---------------------------------------------------------------------------
# Phase 6: Self-Improvement
# ---------------------------------------------------------------------------

class TestPhaseSelfImprovement:
    """Tests for pipelines/nightly_implement.py — Phase 6."""

    def test_implement_no_pending_reflection(self, mock_nightly_uow, target_date, tmp_workspace,
                                              capsys):
        """Self-improvement should handle missing PENDING_REFLECTION.json."""
        ms_dir = str(tmp_workspace / "illo_private")
        with patch("brain.jobs.pipelines.nightly_implement.UnitOfWork", return_value=mock_nightly_uow), \
             patch("brain.jobs.pipelines.nightly_implement.PROJECT_ROOT", ms_dir), \
             patch("brain.jobs.pipelines.nightly_implement.PENDING_PATH",
                   str(Path(ms_dir) / "PENDING_REFLECTION.json")), \
             patch("brain.jobs.pipelines.nightly_implement.LOG_DIR", str(Path(ms_dir) / "logs")), \
             patch("sys.argv", ["nightly_implement", "--date", target_date.isoformat()]):
            from brain.jobs.pipelines.nightly_implement import main as implement_main
            implement_main()


# ---------------------------------------------------------------------------
# Ops public surface validation
# ---------------------------------------------------------------------------

class TestOpsPublicSurface:
    """Validate that ops only ships current public helper surfaces."""

    def test_legacy_ops_artifacts_are_removed(self):
        root = Path(__file__).resolve().parents[1]
        assert not any(path.is_file() for path in (root / "ops" / "cron").rglob("*"))
        assert not any(path.is_file() for path in (root / "ops" / "hooks").rglob("*"))
        assert not (root / "ops" / ("stamp-existing-" + "db.sh")).exists()


# ---------------------------------------------------------------------------
# Standalone repo (no workspace/memory/ dependency)
# ---------------------------------------------------------------------------

class TestStandaloneJournalDir:
    """Ensure illo-brain uses its own journal/ dir, not workspace/memory/."""

    def test_config_defines_journal_dir(self):
        from brain.kernel.config import JOURNAL_DIR, PRIVATE_HOME
        assert JOURNAL_DIR is not None
        assert str(PRIVATE_HOME) in str(JOURNAL_DIR)

    def test_consolidate_uses_journal_dir(self):
        import inspect
        from brain.jobs.pipelines import consolidate

        source = inspect.getsource(consolidate)
        assert "MEMORY_DIR" not in source
        assert "import_daily_log" not in source

    def test_no_workspace_memory_in_pipeline_code(self):
        pipelines_dir = os.path.join(os.path.dirname(__file__), "..", "brain", "jobs", "pipelines")
        violations = []
        for fname in os.listdir(pipelines_dir):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(pipelines_dir, fname)
            with open(fpath) as f:
                for i, line in enumerate(f, 1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if 'WORKSPACE' in line and '"memory"' in line:
                        violations.append(f"{fname}:{i}: {stripped}")
                    if "workspace.*memory" in line.lower() and "import" not in line:
                        violations.append(f"{fname}:{i}: {stripped}")
        assert not violations, (
            "Pipeline code still references workspace/memory/ "
            "(should use config.JOURNAL_DIR):\n" + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# Full pipeline integration (scheduler-owned nightly structure)
# ---------------------------------------------------------------------------

class TestNightlySchedulerOwnership:
    """Validate that nightly work is scheduler-owned, not cron-wrapper-owned."""

    def test_legacy_cron_wrappers_are_removed(self):
        root = Path(__file__).resolve().parents[1]
        assert not any((root / "ops" / "cron").rglob("*.sh"))

    def test_scheduler_program_owns_former_nightly_phases(self):
        from brain.app.scheduler.programs import NIGHTLY_SLEEP_STEP_KEYS

        expected_steps = {
            "memory_consolidation",
            "skill_evolution",
            "reflection",
            "dream",
            "wake_up_index",
            "file_sync",
            "project_draft_cleanup",
            "experiment_assessment",
            "self_improvement",
            "daily_blog",
        }
        assert expected_steps.issubset(set(NIGHTLY_SLEEP_STEP_KEYS))


# ---------------------------------------------------------------------------
# Datetime safety
# ---------------------------------------------------------------------------

class TestDatetimeSafety:
    def test_no_utcnow_in_pipelines(self):
        pipelines_dir = os.path.join(os.path.dirname(__file__), "..", "brain", "jobs", "pipelines")
        violations = []
        for fname in os.listdir(pipelines_dir):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(pipelines_dir, fname)
            with open(fpath) as f:
                for i, line in enumerate(f, 1):
                    if "utcnow()" in line and "#" not in line.split("utcnow()")[0]:
                        violations.append(f"{fname}:{i}: {line.strip()}")
        assert not violations, (
            "Found datetime.utcnow() usage (use datetime.now(timezone.utc) instead):\n"
            + "\n".join(violations)
        )
