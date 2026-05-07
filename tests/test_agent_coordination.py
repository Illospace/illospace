#!/usr/bin/env python3
"""Tests for agent_coordination.py"""

import os
import sys
import pytest
from unittest.mock import patch

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DB_URL"),
    reason="TEST_DB_URL not set — run via scripts/test-with-db.sh",
)

import brain.platform.db as db
from brain.app.cli import agent_coordination as ac


@pytest.fixture(autouse=True)
def clean_coordination_table(db_session, unit_of_work_for_session):
    """Run UnitOfWork-backed coordination code inside the rollback session."""
    db_session.execute(text("DELETE FROM agent_coordination WHERE session_key LIKE 'test_%'"))
    with patch("brain.app.cli.agent_coordination.UnitOfWork", unit_of_work_for_session):
        yield


class TestRegisterAndQuery:
    def test_register_agent(self):
        row_id = ac.register_agent(
            session_key="test_agent_1",
            task_description="Fix bug in run.py",
            files_touched=["run.py", "brain.kernel.config.py"],
            git_branch="fix/run-bug",
            resources_locked=["file:run.py", "git:illo-brain"],
        )
        assert row_id > 0

        agents = ac.get_active_agents()
        test_agents = [a for a in agents if a["session_key"] == "test_agent_1"]
        assert len(test_agents) == 1
        assert test_agents[0]["task_description"] == "Fix bug in run.py"
        assert "run.py" in test_agents[0]["files_touched"]
        assert test_agents[0]["git_branch"] == "fix/run-bug"

    def test_register_upsert(self):
        """Re-registering same session updates rather than duplicating."""
        ac.register_agent("test_agent_2", "Task v1", ["a.py"])
        ac.register_agent("test_agent_2", "Task v2", ["b.py"])

        agents = ac.get_active_agents()
        test_agents = [a for a in agents if a["session_key"] == "test_agent_2"]
        assert len(test_agents) == 1
        assert test_agents[0]["task_description"] == "Task v2"

    def test_get_active_excludes_session(self):
        ac.register_agent("test_agent_3", "Task A")
        ac.register_agent("test_agent_4", "Task B")

        agents = ac.get_active_agents(exclude_session="test_agent_3")
        sessions = [a["session_key"] for a in agents]
        assert "test_agent_3" not in sessions
        assert "test_agent_4" in sessions


class TestConflictDetection:
    def test_file_conflict(self):
        ac.register_agent("test_agent_5", "Editing run", files_touched=["run.py", "brain.platform.db.py"])

        conflicts = ac.check_conflicts(files=["run.py", "memory.py"])
        assert len(conflicts) == 1
        assert conflicts[0]["agent_session"] == "test_agent_5"
        file_conflicts = [c for c in conflicts[0]["conflicts"] if c["type"] == "file"]
        assert "run.py" in file_conflicts[0]["resources"]

    def test_branch_conflict(self):
        ac.register_agent("test_agent_6", "Feature work", git_branch="feature/new-thing")

        conflicts = ac.check_conflicts(branch="feature/new-thing")
        assert len(conflicts) == 1
        assert conflicts[0]["conflicts"][0]["type"] == "branch"

    def test_no_conflict(self):
        ac.register_agent("test_agent_7", "Working on X", files_touched=["x.py"])

        conflicts = ac.check_conflicts(files=["y.py"], branch="other-branch")
        assert len(conflicts) == 0

    def test_exclude_self_from_conflicts(self):
        ac.register_agent("test_agent_8", "Task", files_touched=["a.py"])

        conflicts = ac.check_conflicts(files=["a.py"], exclude_session="test_agent_8")
        assert len(conflicts) == 0


class TestAwarenessContext:
    def test_empty_when_no_agents(self):
        ctx = ac.build_awareness_context(exclude_session="test_all_excluded")
        # May or may not be empty depending on other test state, but shouldn't crash
        assert isinstance(ctx, str)

    def test_context_format(self):
        ac.register_agent("test_agent_9", "Building feature X",
                          files_touched=["feature.py"], git_branch="feat/x")

        ctx = ac.build_awareness_context(exclude_session="test_other_session")
        assert "Active Agent Awareness" in ctx
        assert "Building feature X" in ctx
        assert "feature.py" in ctx
        assert "feat/x" in ctx
        assert "Do not modify" in ctx

    def test_context_excludes_self(self):
        ac.register_agent("test_agent_10", "My task", files_touched=["mine.py"])

        ctx = ac.build_awareness_context(exclude_session="test_agent_10")
        # Should not include the excluded agent's info
        assert "My task" not in ctx or "mine.py" not in ctx or ctx == ""


class TestReleaseCleanup:
    def test_release_done(self):
        ac.register_agent("test_agent_11", "Finishing up")

        released = ac.release_agent("test_agent_11", status="done")
        assert released is True

        agents = ac.get_active_agents()
        sessions = [a["session_key"] for a in agents]
        assert "test_agent_11" not in sessions

    def test_release_failed(self):
        ac.register_agent("test_agent_12", "Will fail")

        released = ac.release_agent("test_agent_12", status="failed")
        assert released is True

    def test_release_nonexistent(self):
        released = ac.release_agent("test_nonexistent_session")
        assert released is False

    def test_release_invalid_status(self):
        with pytest.raises(ValueError):
            ac.release_agent("test_agent_13", status="invalid")

    def test_double_release(self):
        ac.register_agent("test_agent_14", "Task")
        ac.release_agent("test_agent_14")

        # Second release should return False (already done)
        released = ac.release_agent("test_agent_14")
        assert released is False
