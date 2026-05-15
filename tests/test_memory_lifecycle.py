"""Tests for memory lifecycle — promotion, closing, and cleanup."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Repository Tests ──────────────────────────────────────────


class TestScratchpadPromote:
    """Test the promote method on ScratchpadRepository."""

    async def test_promote_empty_run(self):
        from brain.platform.db.repositories.scratchpad import ScratchpadRepository

        mock_session = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=result)
        repo = ScratchpadRepository(mock_session)
        result = await repo.promote(run_id="empty-run")
        assert result["run_id"] == "empty-run"
        assert result["sections"] == {}
        assert result["total_entries"] == 0

    async def test_promote_groups_by_section(self):
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
        result = MagicMock()
        result.scalars.return_value.all.return_value = entries
        mock_session.execute = AsyncMock(return_value=result)
        repo = ScratchpadRepository(mock_session)
        result = await repo.promote(run_id="run-1")

        assert result["total_entries"] == 3
        assert len(result["sections"]["findings"]) == 2
        assert len(result["sections"]["decisions"]) == 1
        assert result["sections"]["findings"][0]["key"] == "bug-1"
        assert result["sections"]["findings"][0]["value"] == "Found null pointer"
        assert result["sections"]["findings"][0]["worker"] == "investigate"


class TestScratchpadClose:
    """Test the close method on ScratchpadRepository."""

    @pytest.mark.parametrize(("rowcount", "expected"), [(5, 5), (0, 0)])
    async def test_close_returns_count(self, rowcount, expected):
        from brain.platform.db.repositories.scratchpad import ScratchpadRepository

        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=MagicMock(rowcount=rowcount))
        mock_session.flush = AsyncMock()
        repo = ScratchpadRepository(mock_session)
        count = await repo.close(run_id="run-1")
        assert count == expected
        mock_session.flush.assert_awaited_once()


class TestScratchpadCleanupExpired:
    """Test the cleanup_expired method."""

    async def test_cleanup_returns_deleted_count(self):
        from brain.platform.db.repositories.scratchpad import ScratchpadRepository

        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=MagicMock(rowcount=3))
        mock_session.flush = AsyncMock()
        repo = ScratchpadRepository(mock_session)
        count = await repo.cleanup_expired(hours=24)
        assert count == 3
        mock_session.flush.assert_awaited_once()


# ── Tool Handler Tests ────────────────────────────────────────


class TestSessionPromoteHandler:
    """Test the session_promote tool handler."""

    async def test_promote_no_run_context(self):
        from brain.systems.runs.tool_handlers import _handle_session_promote, _agent_context

        # Clear any existing context
        if hasattr(_agent_context, "run"):
            delattr(_agent_context, "run")

        result = json.loads(await _handle_session_promote())
        assert "error" in result
        assert "No active AgentRun" in result["error"]

    @patch("brain.systems.runs.tool_handlers._get_current_run_id", return_value="test-run-1")
    async def test_promote_with_run_context(self, mock_run_id):
        from brain.systems.runs.tool_handlers import _handle_session_promote

        mock_uow = MagicMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.scratchpad.promote = AsyncMock(return_value={
            "run_id": "test-run-1",
            "sections": {"findings": [{"key": None, "value": "test", "worker": "dev"}]},
            "total_entries": 1,
        })

        with patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=mock_uow):
            result = json.loads(await _handle_session_promote())
            assert result["run_id"] == "test-run-1"
            assert result["total_entries"] == 1


class TestSessionCloseHandler:
    """Test the session_close tool handler."""

    async def test_close_no_run_context(self):
        from brain.systems.runs.tool_handlers import _handle_session_close, _agent_context

        if hasattr(_agent_context, "run"):
            delattr(_agent_context, "run")

        result = json.loads(await _handle_session_close())
        assert "error" in result

    @patch("brain.systems.runs.tool_handlers._get_current_run_id", return_value="test-run-2")
    async def test_close_with_run_context(self, mock_run_id):
        from brain.systems.runs.tool_handlers import _handle_session_close

        mock_uow = MagicMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.scratchpad.close = AsyncMock(return_value=3)

        with patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=mock_uow):
            result = json.loads(await _handle_session_close())
            assert result["closed"] is True
            assert result["entries_closed"] == 3


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


# -- Harvest Storage Contract Tests -----------------------------------------


class TestHarvestStorageContract:
    """Focused checks for scoped writes from extracted memory items."""

    @patch("brain.systems.memory.narratives.link_session_to_narratives", new_callable=AsyncMock)
    @patch("brain.app.cli.memory.add_memory", new_callable=AsyncMock)
    @patch("brain.systems.memory.harvest.extract_harvest_items")
    async def test_sensitivity_narrows_visibility_on_session_harvest(
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

        await _harvest_session(
            "session-1",
            [{"role": "user", "content": "my email is alex@example.com"}, {"role": "assistant", "content": "Noted"}],
            org_id="org-1",
            user_id="user-1",
            idea_id="idea-1",
            run_id=14,
        )

        mock_add_memory.assert_awaited_once()
        kwargs = mock_add_memory.call_args.kwargs
        assert kwargs["memory_type"] == "fact"
        assert kwargs["scope"] == "project"
        assert kwargs["memory_tier"] == "episodic"
        assert kwargs["write_context"].visibility == "private"
        assert kwargs["write_context"].org_id == "org-1"
        assert kwargs["write_context"].evidence["sensitivity"] == "high"
        mock_link.assert_not_awaited()

    @patch("brain.systems.memory.narratives.link_session_to_narratives", new_callable=AsyncMock)
    @patch("brain.app.cli.memory.add_memory", new_callable=AsyncMock)
    @patch("brain.systems.memory.harvest.extract_harvest_items")
    async def test_failed_extraction_raw_episode_uses_existing_write_context(
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

        await _harvest_session(
            "session-2",
            [{"role": "user", "content": "help me"}, {"role": "assistant", "content": "sure"}],
            org_id="org-1",
            user_id="user-1",
        )

        mock_add_memory.assert_awaited_once()
        kwargs = mock_add_memory.call_args.kwargs
        assert kwargs["memory_type"] == "episode"
        assert kwargs["salience"] == 3.0
        assert kwargs["harvest_type"] == "raw_episode"
        assert kwargs["write_context"].confidence == RAW_EPISODE_CONFIDENCE
        assert kwargs["write_context"].visibility == "private"
        mock_link.assert_not_awaited()
