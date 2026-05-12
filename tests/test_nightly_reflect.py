"""Tests for nightly_reflect.py — gather_context, apply_reflection, and regression tests (ORM-based)."""
import json
import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


@pytest.fixture
def mock_reflect_uow():
    """Create a mock UnitOfWork for nightly_reflect tests."""
    uow = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)

    # Track execute calls to return different results per query
    execute_results = []
    call_idx = [0]

    def execute_side_effect(*args, **kwargs):
        result = MagicMock()
        result.mappings.return_value.fetchall.return_value = []
        result.mappings.return_value.all.return_value = []
        result.mappings.return_value.fetchone.return_value = None
        result.mappings.return_value.first.return_value = None
        idx = call_idx[0]
        call_idx[0] += 1
        if idx < len(execute_results):
            return execute_results[idx]
        return result

    uow.session.execute.side_effect = execute_side_effect
    uow._execute_results = execute_results
    uow._call_idx = call_idx
    uow.session.scalars.return_value.all.return_value = []
    uow.session.scalars.return_value.first.return_value = None
    uow.skills = MagicMock()
    uow.skills.get_by_name.return_value = None
    return uow


@pytest.fixture
def patch_reflect_uow(mock_reflect_uow):
    """Patch UnitOfWork for nightly_reflect module."""
    with patch("brain.jobs.pipelines.nightly_reflect.UnitOfWork", return_value=mock_reflect_uow):
        yield mock_reflect_uow


class TestGatherContext:
    """Test gather_context() data collection."""

    def test_gathers_all_sections(self, patch_reflect_uow, tmp_path):
        target = date(2026, 3, 1)

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir(parents=True)
        (journal_dir / f"{target.isoformat()}.md").write_text("# Daily log content")

        with patch("brain.jobs.pipelines.nightly_reflect.WORKSPACE", str(tmp_path)), \
             patch("brain.kernel.config.JOURNAL_DIR", str(journal_dir)):
            from brain.jobs.pipelines.nightly_reflect import gather_context
            ctx = gather_context(target)

        expected_keys = [
            "skill_executions", "skills", "retrievals", "tasks",
            "new_memories", "previous_metrics", "agent_runses", "daily_log"
        ]
        for key in expected_keys:
            assert key in ctx, f"Missing context section: {key}"

    def test_daily_log_missing_gracefully(self, patch_reflect_uow):
        target = date(2026, 3, 1)

        with patch("brain.jobs.pipelines.nightly_reflect.WORKSPACE", "/nonexistent"), \
             patch("brain.kernel.config.JOURNAL_DIR", "/nonexistent/journal"):
            from brain.jobs.pipelines.nightly_reflect import gather_context
            ctx = gather_context(target)

        assert "daily_log" not in ctx


class TestGatherContextRegressions:
    """Regression tests for known bugs in gather_context."""

    def test_connection_uses_context_manager(self):
        """Regression: must use a UnitOfWork/db connection context manager."""
        import inspect
        from brain.jobs.pipelines.nightly_reflect import gather_context
        source = inspect.getsource(gather_context)
        assert "open_unit_of_work" in source or "with db.get_conn()" in source, \
            "gather_context must use a context manager"


class TestApplyReflection:
    """Test apply_reflection() with various reflection outputs."""

    def test_applies_skill_pitfall(self, patch_reflect_uow):
        target = date(2026, 3, 1)

        # apply_reflection uses raw SQL: SELECT id, version, pitfalls, refinements FROM skills
        skill_row = {"id": 1, "version": 3, "pitfalls": [], "refinements": []}
        exec_count = [0]
        def execute_side_effect(*args, **kwargs):
            exec_count[0] += 1
            result = MagicMock()
            if exec_count[0] == 1:
                # SELECT skill
                result.mappings.return_value.first.return_value = skill_row
            return result
        patch_reflect_uow.session.execute.side_effect = execute_side_effect

        reflection = {
            "skill_refinements": [{
                "skill_name": "debugging",
                "change_type": "add_pitfall",
                "change": "Always check connection pooling",
                "reason": "Connection leak found"
            }],
            "new_skills_proposed": [],
            "daily_metrics_update": {},
            "journal_entry": "",
        }

        from brain.jobs.pipelines.nightly_reflect import apply_reflection
        applied = apply_reflection(reflection, target)
        assert any("pitfall" in a.lower() for a in applied)

    def test_applies_procedure_refinement(self, patch_reflect_uow):
        target = date(2026, 3, 1)

        skill_row = {"id": 1, "version": 2, "pitfalls": [], "refinements": []}
        exec_count = [0]
        def execute_side_effect(*args, **kwargs):
            exec_count[0] += 1
            result = MagicMock()
            if exec_count[0] == 1:
                result.mappings.return_value.first.return_value = skill_row
            return result
        patch_reflect_uow.session.execute.side_effect = execute_side_effect

        reflection = {
            "skill_refinements": [{
                "skill_name": "debugging",
                "change_type": "refine_procedure",
                "change": "Added verification step",
                "reason": "Missing verification",
                "new_procedure": "1. Reproduce\n2. Verify\n3. Fix"
            }],
            "new_skills_proposed": [],
            "daily_metrics_update": {},
            "journal_entry": "",
        }

        from brain.jobs.pipelines.nightly_reflect import apply_reflection
        applied = apply_reflection(reflection, target)
        assert any("Refined" in a for a in applied)

    def test_creates_new_skill(self, patch_reflect_uow):
        target = date(2026, 3, 1)
        patch_reflect_uow.skills.get_by_name.return_value = None

        reflection = {
            "skill_refinements": [],
            "new_skills_proposed": [{
                "name": "new-skill",
                "description": "A new skill for handling repetitive task patterns that emerge from cortex",
                "initial_procedure": (
                    "1. Identify the recurring pattern from cortex task history\n"
                    "2. Extract the common steps and decision points\n"
                    "3. Formalize into a repeatable procedure with clear acceptance criteria\n"
                    "4. Test the procedure against recent examples to validate coverage\n"
                    "5. Submit for review and iterate based on feedback"
                ),
                "emerged_from": "pattern detection"
            }],
            "daily_metrics_update": {},
            "journal_entry": "",
        }

        from brain.jobs.pipelines.nightly_reflect import apply_reflection
        applied = apply_reflection(reflection, target)
        assert any("emerged" in a.lower() for a in applied)

    def test_skips_existing_skill(self, patch_reflect_uow):
        target = date(2026, 3, 1)
        mock_skill = MagicMock()
        mock_skill.id = 99
        patch_reflect_uow.skills.get_by_name.return_value = mock_skill

        reflection = {
            "skill_refinements": [],
            "new_skills_proposed": [{
                "name": "existing-skill",
                "description": "Already exists",
                "initial_procedure": "Steps",
            }],
            "daily_metrics_update": {},
            "journal_entry": "",
        }

        from brain.jobs.pipelines.nightly_reflect import apply_reflection
        applied = apply_reflection(reflection, target)
        assert not any("emerged" in a.lower() for a in applied)

    def test_writes_journal_entry(self, patch_reflect_uow, tmp_path):
        target = date(2026, 3, 1)

        reflection = {
            "skill_refinements": [],
            "new_skills_proposed": [],
            "daily_metrics_update": {},
            "journal_entry": "Today was productive. We fixed three bugs.",
        }

        journal_dir = tmp_path / "journal"
        with patch("brain.jobs.pipelines.nightly_reflect.config.JOURNAL_DIR", journal_dir):
            from brain.jobs.pipelines.nightly_reflect import apply_reflection
            applied = apply_reflection(reflection, target)

        assert journal_dir.exists()
        journal_files = list(journal_dir.glob("*.md"))
        assert len(journal_files) == 1
        content = journal_files[0].read_text()
        assert "productive" in content

    def test_empty_reflection_no_crash(self, patch_reflect_uow):
        target = date(2026, 3, 1)
        from brain.jobs.pipelines.nightly_reflect import apply_reflection
        applied = apply_reflection({}, target)
        assert isinstance(applied, list)


class TestApplyReflectionRegressions:
    def test_apply_reflection_uses_context_manager(self):
        import inspect
        from brain.jobs.pipelines.nightly_reflect import apply_reflection
        source = inspect.getsource(apply_reflection)
        assert "with" in source and ("open_unit_of_work" in source or "db.get_conn()" in source), \
            "apply_reflection must use a context manager"


class TestBuildReflectionPrompt:
    def test_prompt_contains_all_sections(self):
        from brain.jobs.pipelines.nightly_reflect import build_reflection_prompt
        context = {
            "skill_executions": [],
            "skills": [],
            "retrievals": [],
            "tasks": [],
            "new_memories": [],
            "previous_metrics": [],
            "agent_runses": [{"id": 1, "status": "completed"}],
            "daily_log": "test log",
        }
        prompt = build_reflection_prompt(context, date(2026, 3, 1))
        assert "Skill Executions" in prompt
        assert "Retrieval Log" in prompt
        assert "Cortex Runs" in prompt
        assert "Output Format" in prompt
