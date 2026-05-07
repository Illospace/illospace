"""Tests for memory lifecycle — promotion, closing, and cleanup."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest


# ── Repository Tests ──────────────────────────────────────────


class TestScratchpadPromote:
    """Test the promote method on ScratchpadRepository."""

    def test_promote_empty_run(self):
        from brain.platform.db.repositories.scratchpad import ScratchpadRepository

        mock_session = MagicMock()
        mock_session.execute.return_value.scalars.return_value.all.return_value = []
        repo = ScratchpadRepository(mock_session)
        result = repo.promote(run_id="empty-run")
        assert result["run_id"] == "empty-run"
        assert result["sections"] == {}
        assert result["total_entries"] == 0

    def test_promote_groups_by_section(self):
        from brain.platform.db.repositories.scratchpad import ScratchpadRepository
        from brain.platform.db.models.scratchpad import SessionScratchpad

        # Build mock entries
        entries = []
        for section, key, value, worker in [
            ("findings", "bug-1", "Found null pointer", "investigate"),
            ("findings", "bug-2", "Race condition in handler", "investigate"),
            ("decisions", None, "Use mutex for sync", "develop"),
        ]:
            e = SessionScratchpad(
                id=len(entries) + 1, run_id="run-1", section=section,
                key=key, value=value, worker_name=worker,
            )
            e.created_at = datetime.now(timezone.utc)
            entries.append(e)

        mock_session = MagicMock()
        mock_session.execute.return_value.scalars.return_value.all.return_value = entries
        repo = ScratchpadRepository(mock_session)
        result = repo.promote(run_id="run-1")

        assert result["total_entries"] == 3
        assert len(result["sections"]["findings"]) == 2
        assert len(result["sections"]["decisions"]) == 1
        assert result["sections"]["findings"][0]["value"] == "Found null pointer"
        assert result["sections"]["findings"][0]["worker"] == "investigate"

    def test_promote_includes_key_and_worker(self):
        from brain.platform.db.repositories.scratchpad import ScratchpadRepository
        from brain.platform.db.models.scratchpad import SessionScratchpad

        e = SessionScratchpad(
            id=1, run_id="run-2", section="resources",
            key="doc-link", value="https://example.com", worker_name="develop",
        )
        e.created_at = datetime.now(timezone.utc)

        mock_session = MagicMock()
        mock_session.execute.return_value.scalars.return_value.all.return_value = [e]
        repo = ScratchpadRepository(mock_session)
        result = repo.promote(run_id="run-2")

        entry = result["sections"]["resources"][0]
        assert entry["key"] == "doc-link"
        assert entry["worker"] == "develop"


class TestScratchpadClose:
    """Test the close method on ScratchpadRepository."""

    def test_close_returns_count(self):
        from brain.platform.db.repositories.scratchpad import ScratchpadRepository

        mock_session = MagicMock()
        mock_session.execute.return_value.rowcount = 5
        repo = ScratchpadRepository(mock_session)
        count = repo.close(run_id="run-1")
        assert count == 5
        mock_session.flush.assert_called_once()

    def test_close_zero_entries(self):
        from brain.platform.db.repositories.scratchpad import ScratchpadRepository

        mock_session = MagicMock()
        mock_session.execute.return_value.rowcount = 0
        repo = ScratchpadRepository(mock_session)
        count = repo.close(run_id="nonexistent")
        assert count == 0


class TestScratchpadCleanupExpired:
    """Test the cleanup_expired method."""

    def test_cleanup_returns_deleted_count(self):
        from brain.platform.db.repositories.scratchpad import ScratchpadRepository

        mock_session = MagicMock()
        mock_session.execute.return_value.rowcount = 3
        repo = ScratchpadRepository(mock_session)
        count = repo.cleanup_expired(hours=24)
        assert count == 3
        mock_session.flush.assert_called_once()


# ── Tool Handler Tests ────────────────────────────────────────


class TestSessionPromoteHandler:
    """Test the session_promote tool handler."""

    def test_promote_no_run_context(self):
        from brain.systems.runs.tool_handlers import _handle_session_promote, _agent_context

        # Clear any existing context
        if hasattr(_agent_context, "run"):
            delattr(_agent_context, "run")

        result = json.loads(_handle_session_promote())
        assert "error" in result
        assert "No active AgentRun" in result["error"]

    @patch("brain.systems.runs.tool_handlers._get_current_run_id", return_value="test-run-1")
    def test_promote_with_run_context(self, mock_run_id):
        from brain.systems.runs.tool_handlers import _handle_session_promote

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.scratchpad.promote.return_value = {
            "run_id": "test-run-1",
            "sections": {"findings": [{"key": None, "value": "test", "worker": "dev"}]},
            "total_entries": 1,
        }

        with patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=mock_uow):
            result = json.loads(_handle_session_promote())
            assert result["run_id"] == "test-run-1"
            assert result["total_entries"] == 1


class TestSessionCloseHandler:
    """Test the session_close tool handler."""

    def test_close_no_run_context(self):
        from brain.systems.runs.tool_handlers import _handle_session_close, _agent_context

        if hasattr(_agent_context, "run"):
            delattr(_agent_context, "run")

        result = json.loads(_handle_session_close())
        assert "error" in result

    @patch("brain.systems.runs.tool_handlers._get_current_run_id", return_value="test-run-2")
    def test_close_with_run_context(self, mock_run_id):
        from brain.systems.runs.tool_handlers import _handle_session_close

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.scratchpad.close.return_value = 3

        with patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=mock_uow):
            result = json.loads(_handle_session_close())
            assert result["closed"] is True
            assert result["entries_closed"] == 3


# ── Tool Definition Tests ────────────────────────────────────


class TestLifecycleToolDefinitions:
    """Test that lifecycle tools are properly defined and included."""

    def test_lifecycle_tools_exist(self):
        from brain.systems.runs.tool_definitions import LIFECYCLE_TOOLS
        names = [t["name"] for t in LIFECYCLE_TOOLS]
        assert "session_promote" in names
        assert "session_close" in names

    def test_lifecycle_tools_in_coordinator(self):
        from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS
        names = [t["name"] for t in COORDINATOR_TOOLS]
        assert "session_promote" in names
        assert "session_close" in names

    def test_lifecycle_tools_not_in_worker(self):
        from brain.systems.runs.tool_definitions import WORKER_TOOLS
        names = [t["name"] for t in WORKER_TOOLS]
        assert "session_promote" not in names
        assert "session_close" not in names


# ── Model Tests ───────────────────────────────────────────────


class TestScratchpadModel:
    """Test the updated scratchpad model with closed_at."""

    def test_closed_at_defaults_to_none(self):
        from brain.platform.db.models.scratchpad import SessionScratchpad

        entry = SessionScratchpad(
            run_id="test", section="findings", value="test value",
        )
        assert entry.closed_at is None

    def test_closed_at_can_be_set(self):
        from brain.platform.db.models.scratchpad import SessionScratchpad

        now = datetime.now(timezone.utc)
        entry = SessionScratchpad(
            run_id="test", section="findings", value="test value",
            closed_at=now,
        )
        assert entry.closed_at == now


# ── Builtin Skill Tests ──────────────────────────────────────


class TestOrchestrateSkillProcedure:
    """Test that the orchestrate skill includes memory lifecycle steps."""

    def test_procedure_includes_session_promote(self):
        from brain.systems.skills.builtin import BUILTIN_SKILLS
        proc = BUILTIN_SKILLS["orchestrate"]["procedure"]
        assert "session_promote" in proc

    def test_procedure_includes_session_close(self):
        from brain.systems.skills.builtin import BUILTIN_SKILLS
        proc = BUILTIN_SKILLS["orchestrate"]["procedure"]
        assert "session_close" in proc

    def test_procedure_includes_brain_encode_at_start(self):
        from brain.systems.skills.builtin import BUILTIN_SKILLS
        proc = BUILTIN_SKILLS["orchestrate"]["procedure"]
        assert "AgentRun graph started" in proc

    def test_procedure_includes_brain_encode_at_end(self):
        from brain.systems.skills.builtin import BUILTIN_SKILLS
        proc = BUILTIN_SKILLS["orchestrate"]["procedure"]
        assert "AgentRun graph completed/failed" in proc


# -- Harvest Storage Contract Tests -----------------------------------------


class TestHarvestStorageContract:
    """Focused checks for scoped writes from extracted memory items."""

    @patch("brain.systems.memory.narratives.link_session_to_narratives")
    @patch("brain.app.cli.memory.add_memory")
    @patch("brain.systems.memory.harvest.extract_harvest_items")
    def test_sensitivity_narrows_visibility_on_session_harvest(
        self,
        mock_extract,
        mock_add_memory,
        mock_link,
    ):
        from brain.systems.memory.harvest import HarvestItem
        from brain.systems.sessions.harvest import _harvest_session

        mock_extract.return_value = [
            HarvestItem(
                content="Alex's contact email is alex@example.com",
                harvest_type="fact",
                confidence=0.82,
                sensitivity="high",
                scope="org",
                evidence=[{"message_index": 0, "role": "user", "quote": "alex@example.com"}],
            )
        ]

        _harvest_session(
            "session-1",
            [{"role": "user", "content": "my email is alex@example.com"}, {"role": "assistant", "content": "Noted"}],
            org_id="org-1",
            user_id="user-1",
            idea_id="idea-1",
            run_id=14,
        )

        mock_add_memory.assert_called_once()
        kwargs = mock_add_memory.call_args.kwargs
        assert kwargs["memory_type"] == "fact"
        assert kwargs["scope"] == "project"
        assert kwargs["memory_tier"] == "episodic"
        assert kwargs["write_context"].visibility == "private"
        assert kwargs["write_context"].org_id == "org-1"
        assert kwargs["write_context"].evidence["sensitivity"] == "high"
        mock_link.assert_not_called()

    @patch("brain.systems.memory.narratives.link_session_to_narratives")
    @patch("brain.app.cli.memory.add_memory")
    @patch("brain.systems.memory.harvest.extract_harvest_items")
    def test_failed_extraction_raw_episode_uses_existing_write_context(
        self,
        mock_extract,
        mock_add_memory,
        mock_link,
    ):
        from brain.systems.memory.harvest import HarvestItem, RAW_EPISODE_CONFIDENCE
        from brain.systems.sessions.harvest import _harvest_session

        mock_extract.return_value = [
            HarvestItem(
                content="Raw conversation episode captured because memory extraction provider_unavailable: user asked for help",
                harvest_type="raw_episode",
                confidence=RAW_EPISODE_CONFIDENCE,
                topic_tags=["raw-episode"],
                sensitivity="medium",
                scope="personal",
                evidence=[{"message_index": None, "role": "unknown", "quote": "provider unavailable"}],
                raw_episode=True,
            )
        ]

        _harvest_session(
            "session-2",
            [{"role": "user", "content": "help me"}, {"role": "assistant", "content": "sure"}],
            org_id="org-1",
            user_id="user-1",
        )

        mock_add_memory.assert_called_once()
        kwargs = mock_add_memory.call_args.kwargs
        assert kwargs["memory_type"] == "episode"
        assert kwargs["salience"] == 3.0
        assert kwargs["harvest_type"] == "raw_episode"
        assert kwargs["write_context"].confidence == RAW_EPISODE_CONFIDENCE
        assert kwargs["write_context"].visibility == "private"
        mock_link.assert_not_called()
