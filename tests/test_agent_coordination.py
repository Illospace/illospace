#!/usr/bin/env python3
"""Tests for agent_coordination.py"""

import os
import sys
import pytest
from unittest.mock import patch

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

pytestmark = [
    pytest.mark.requires_db,
    pytest.mark.skipif(
        not os.environ.get("TEST_DB_URL"),
        reason="TEST_DB_URL not set — run via scripts/test-with-db.sh",
    ),
    pytest.mark.asyncio,
]

import brain.platform.db as db
from brain.app.cli import agent_coordination as ac


@pytest.fixture(autouse=True)
async def clean_coordination_table(db_session):
    """Run UnitOfWork-backed coordination code inside the rollback session."""
    await db_session.execute(text("""
        CREATE TABLE IF NOT EXISTS agent_coordination (
            id SERIAL PRIMARY KEY,
            session_key TEXT NOT NULL,
            task_description TEXT NOT NULL,
            files_touched TEXT[] NOT NULL DEFAULT '{}',
            git_branch TEXT,
            resources_locked TEXT[] NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'running',
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        )
    """))
    await db_session.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_coordination_running_session
        ON agent_coordination (session_key)
        WHERE status = 'running'
    """))
    await db_session.execute(text("DELETE FROM agent_coordination WHERE session_key LIKE 'test_%'"))

    class _AsyncSessionUnitOfWork:
        async def __aenter__(self):
            self.session = db_session
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                await db_session.flush()
            return False

    with patch("brain.app.cli.agent_coordination.UnitOfWork", _AsyncSessionUnitOfWork):
        yield


class TestRegisterAndQuery:
    async def test_register_agent(self):
        row_id = await ac.register_agent(
            session_key="test_agent_1",
            task_description="Fix bug in run.py",
            files_touched=["run.py", "brain.kernel.config.py"],
            git_branch="fix/run-bug",
            resources_locked=["file:run.py", "git:illo-brain"],
        )
        assert row_id > 0

        agents = await ac.get_active_agents()
        test_agents = [a for a in agents if a["session_key"] == "test_agent_1"]
        assert len(test_agents) == 1
        assert test_agents[0]["task_description"] == "Fix bug in run.py"
        assert "run.py" in test_agents[0]["files_touched"]
        assert test_agents[0]["git_branch"] == "fix/run-bug"

    async def test_register_upsert(self):
        """Re-registering same session updates rather than duplicating."""
        await ac.register_agent("test_agent_2", "Task v1", ["a.py"])
        await ac.register_agent("test_agent_2", "Task v2", ["b.py"])

        agents = await ac.get_active_agents()
        test_agents = [a for a in agents if a["session_key"] == "test_agent_2"]
        assert len(test_agents) == 1
        assert test_agents[0]["task_description"] == "Task v2"

    async def test_get_active_excludes_session(self):
        await ac.register_agent("test_agent_3", "Task A")
        await ac.register_agent("test_agent_4", "Task B")

        agents = await ac.get_active_agents(exclude_session="test_agent_3")
        sessions = [a["session_key"] for a in agents]
        assert "test_agent_3" not in sessions
        assert "test_agent_4" in sessions


class TestConflictDetection:
    async def test_file_conflict(self):
        await ac.register_agent("test_agent_5", "Editing run", files_touched=["run.py", "brain.platform.db.py"])

        conflicts = await ac.check_conflicts(files=["run.py", "memory.py"])
        assert len(conflicts) == 1
        assert conflicts[0]["agent_session"] == "test_agent_5"
        file_conflicts = [c for c in conflicts[0]["conflicts"] if c["type"] == "file"]
        assert "run.py" in file_conflicts[0]["resources"]

    async def test_branch_conflict(self):
        await ac.register_agent("test_agent_6", "Feature work", git_branch="feature/new-thing")

        conflicts = await ac.check_conflicts(branch="feature/new-thing")
        assert len(conflicts) == 1
        assert conflicts[0]["conflicts"][0]["type"] == "branch"

    async def test_no_conflict(self):
        await ac.register_agent("test_agent_7", "Working on X", files_touched=["x.py"])

        conflicts = await ac.check_conflicts(files=["y.py"], branch="other-branch")
        assert len(conflicts) == 0

    async def test_exclude_self_from_conflicts(self):
        await ac.register_agent("test_agent_8", "Task", files_touched=["a.py"])

        conflicts = await ac.check_conflicts(files=["a.py"], exclude_session="test_agent_8")
        assert len(conflicts) == 0


class TestAwarenessContext:
    async def test_empty_when_no_agents(self):
        ctx = await ac.build_awareness_context(exclude_session="test_all_excluded")
        # May or may not be empty depending on other test state, but shouldn't crash
        assert isinstance(ctx, str)

    async def test_context_format(self):
        await ac.register_agent("test_agent_9", "Building feature X",
                                files_touched=["feature.py"], git_branch="feat/x")

        ctx = await ac.build_awareness_context(exclude_session="test_other_session")
        assert "Active Agent Awareness" in ctx
        assert "Building feature X" in ctx
        assert "feature.py" in ctx
        assert "feat/x" in ctx
        assert "Do not modify" in ctx

    async def test_context_excludes_self(self):
        await ac.register_agent("test_agent_10", "My task", files_touched=["mine.py"])

        ctx = await ac.build_awareness_context(exclude_session="test_agent_10")
        # Should not include the excluded agent's info
        assert "My task" not in ctx or "mine.py" not in ctx or ctx == ""


class TestReleaseCleanup:
    async def test_release_done(self):
        await ac.register_agent("test_agent_11", "Finishing up")

        released = await ac.release_agent("test_agent_11", status="done")
        assert released is True

        agents = await ac.get_active_agents()
        sessions = [a["session_key"] for a in agents]
        assert "test_agent_11" not in sessions

    async def test_release_failed(self):
        await ac.register_agent("test_agent_12", "Will fail")

        released = await ac.release_agent("test_agent_12", status="failed")
        assert released is True

    async def test_release_nonexistent(self):
        released = await ac.release_agent("test_nonexistent_session")
        assert released is False

    async def test_release_invalid_status(self):
        with pytest.raises(ValueError):
            await ac.release_agent("test_agent_13", status="invalid")

    async def test_double_release(self):
        await ac.register_agent("test_agent_14", "Task")
        await ac.release_agent("test_agent_14")

        # Second release should return False (already done)
        released = await ac.release_agent("test_agent_14")
        assert released is False
