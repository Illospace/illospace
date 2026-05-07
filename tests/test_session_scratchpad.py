"""Tests for AgentRun scratchpad tool handlers and repository."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── Repository Tests (unit, no DB) ──────────────────────────


class TestScratchpadRepository:
    """Test the repository layer with a mock session."""

    def test_write_creates_entry(self):
        from brain.platform.db.repositories.scratchpad import ScratchpadRepository
        from brain.platform.db.models.scratchpad import SessionScratchpad

        mock_session = MagicMock()
        repo = ScratchpadRepository(mock_session)
        entry = repo.write(
            run_id="abc123", section="findings",
            value="Found a bug", key="bug-1", worker_name="develop",
        )
        mock_session.add.assert_called_once()
        mock_session.flush.assert_called_once()
        assert isinstance(entry, SessionScratchpad)
        assert entry.run_id == "abc123"
        assert entry.section == "findings"
        assert entry.key == "bug-1"

    def test_write_without_key(self):
        from brain.platform.db.repositories.scratchpad import ScratchpadRepository

        mock_session = MagicMock()
        repo = ScratchpadRepository(mock_session)
        entry = repo.write(
            run_id="abc123", section="decisions",
            value="Use approach A",
        )
        assert entry.key is None


# ── Tool Handler Tests ───────────────────────────────────────


class TestSessionToolHandlers:
    """Test tool handlers with mocked context and DB."""

    def _set_agent_context(self, run_id: str = "test-run-123", skill: str = "develop"):
        """Set up a fake AgentRun context."""
        from brain.systems.runs.tool_handlers import _agent_context

        _agent_context.run = SimpleNamespace(run_id=run_id, skill_used=skill)

    def _clear_agent_context(self):
        from brain.systems.runs.tool_handlers import _agent_context
        _agent_context.run = None

    def test_session_write_no_context(self):
        from brain.systems.runs.tool_handlers import _handle_session_write
        self._clear_agent_context()
        result = json.loads(_handle_session_write(section="findings", value="test"))
        assert "error" in result
        assert "No active AgentRun" in result["error"]

    def test_session_write_invalid_section(self):
        from brain.systems.runs.tool_handlers import _handle_session_write
        self._set_agent_context()
        result = json.loads(_handle_session_write(section="invalid", value="test"))
        assert "error" in result
        assert "Invalid section" in result["error"]
        self._clear_agent_context()

    def test_session_write_success(self):
        from brain.systems.runs.tool_handlers import _handle_session_write
        from brain.platform.db.models.scratchpad import SessionScratchpad

        self._set_agent_context()

        mock_entry = SessionScratchpad(
            run_id="test-run-123", section="findings",
            value="test value", key="k1", worker_name="develop",
        )
        mock_entry.id = 42

        mock_uow = MagicMock()
        mock_uow.scratchpad.write.return_value = mock_entry

        with patch("brain.platform.db.repositories.unit_of_work.UnitOfWork.__enter__", return_value=mock_uow), \
             patch("brain.platform.db.repositories.unit_of_work.UnitOfWork.__exit__", return_value=False), \
             patch("brain.platform.db.repositories.unit_of_work.UnitOfWork.__init__", return_value=None):
            result = json.loads(_handle_session_write(section="findings", value="test value", key="k1"))

        assert result["written"] is True
        assert result["id"] == 42
        assert result["section"] == "findings"
        self._clear_agent_context()

    def test_session_read_no_context(self):
        from brain.systems.runs.tool_handlers import _handle_session_read
        self._clear_agent_context()
        result = json.loads(_handle_session_read())
        assert "error" in result

    def test_session_append_delegates_to_write(self):
        from brain.systems.runs.tool_handlers import _handle_session_append
        self._clear_agent_context()
        # Without context, should still return error (same as write)
        result = json.loads(_handle_session_append(section="findings", value="test"))
        assert "error" in result

    def test_session_list_no_context(self):
        from brain.systems.runs.tool_handlers import _handle_session_list
        self._clear_agent_context()
        result = json.loads(_handle_session_list())
        assert "error" in result


# ── Tool Definition Tests ────────────────────────────────────


class TestSessionToolDefinitions:
    """Verify session tools are registered in tool lists."""

    def test_session_tools_in_worker_tools(self):
        from brain.systems.runs.tool_definitions import WORKER_TOOLS
        names = {t["name"] for t in WORKER_TOOLS}
        assert "session_write" in names
        assert "session_read" in names
        assert "read_thread_messages" in names
        assert "session_append" in names
        assert "session_list" in names

    def test_session_tools_in_coordinator_tools(self):
        from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS
        names = {t["name"] for t in COORDINATOR_TOOLS}
        assert "session_write" in names
        assert "session_read" in names
        assert "read_thread_messages" in names
        assert "session_append" in names
        assert "session_list" in names

    def test_session_write_schema_has_required_fields(self):
        from brain.systems.runs.tool_definitions import SESSION_TOOLS
        write_tool = next(t for t in SESSION_TOOLS if t["name"] == "session_write")
        assert "section" in write_tool["input_schema"]["required"]
        assert "value" in write_tool["input_schema"]["required"]

    def test_session_tools_count(self):
        from brain.systems.runs.tool_definitions import SESSION_TOOLS
        assert len(SESSION_TOOLS) == 4


# ── Model Tests ──────────────────────────────────────────────


class TestSessionScratchpadModel:
    """Verify the SQLAlchemy model is properly defined."""

    def test_model_tablename(self):
        from brain.platform.db.models.scratchpad import SessionScratchpad
        assert SessionScratchpad.__tablename__ == "session_scratchpad"

    def test_model_columns(self):
        from brain.platform.db.models.scratchpad import SessionScratchpad
        cols = {c.name for c in SessionScratchpad.__table__.columns}
        assert cols >= {"id", "run_id", "worker_name", "section", "key", "value", "created_at"}

    def test_model_registered_in_base(self):
        from brain.platform.db.base import Base
        assert "session_scratchpad" in Base.metadata.tables


# ── Handler Registration Tests ───────────────────────────────


class TestHandlerRegistration:
    """Verify handlers are wired up in the run map."""

    @patch("brain.systems.runs.tool_handlers.tool_brain_recall", create=True)
    @patch("brain.systems.runs.tool_handlers.tool_brain_guardrails", create=True)
    @patch("brain.systems.runs.tool_handlers.tool_brain_skills", create=True)
    @patch("brain.systems.runs.tool_handlers.tool_brain_encode", create=True)
    @patch("brain.systems.runs.tool_handlers.tool_brain_vault", create=True)
    def test_session_handlers_in_map(self, *mocks):
        from brain.systems.runs.tool_handlers import _get_tool_handlers
        handlers = _get_tool_handlers()
        assert "session_write" in handlers
        assert "session_read" in handlers
        assert "read_thread_messages" in handlers
        assert "session_append" in handlers
        assert "session_list" in handlers
        assert callable(handlers["session_write"])
