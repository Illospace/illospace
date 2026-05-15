"""Tests for brain_context.py — context injection for the brain-context hook.

Covers:
- format_system_message() with various input combinations
- get_context() with mocked DB (unit tests)
- get_context() with real DB (integration test)

Closes #33
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from contextlib import contextmanager

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.app.hooks.brain_context import get_context, format_system_message


# ─── format_system_message tests ───────────────────────────────────────


class TestFormatSystemMessage:
    """Test format_system_message with various context dicts."""

    def test_empty_context_returns_empty_string(self):
        ctx = {"memories": [], "guardrails": [], "warnings": []}
        assert format_system_message(ctx) == ""

    def test_empty_dict_returns_empty_string(self):
        assert format_system_message({}) == ""

    def test_warnings_formatted(self):
        ctx = {"warnings": ["Never deploy on Friday"], "memories": [], "guardrails": []}
        result = format_system_message(ctx)
        assert "⚠️ BRAIN WARNINGS:" in result
        assert "Never deploy on Friday" in result

    def test_guardrails_formatted(self):
        ctx = {
            "warnings": [],
            "guardrails": [{"skill": "deploy", "failure": "timeout on staging"}],
            "memories": [],
        }
        result = format_system_message(ctx)
        assert "🛡️ RECENT FAILURES:" in result
        assert "[deploy]" in result
        assert "timeout on staging" in result

    def test_memories_only_above_05_similarity(self):
        ctx = {
            "warnings": [],
            "guardrails": [],
            "memories": [
                {"type": "lesson", "salience": 8, "similarity": 0.6, "content": "High sim memory"},
                {"type": "lesson", "salience": 5, "similarity": 0.46, "content": "Low sim memory"},
            ],
        }
        result = format_system_message(ctx)
        assert "High sim memory" in result
        assert "Low sim memory" not in result

    def test_memories_limited_to_3(self):
        memories = [
            {"type": "lesson", "salience": 8, "similarity": 0.7, "content": f"Memory {i}"}
            for i in range(5)
        ]
        ctx = {"warnings": [], "guardrails": [], "memories": memories}
        result = format_system_message(ctx)
        # Should only show 3
        assert result.count("🧠 RELEVANT CONTEXT:") == 1
        count = sum(1 for line in result.split("\n") if line.strip().startswith("• [lesson"))
        assert count == 3

    def test_all_sections_present(self):
        ctx = {
            "warnings": ["Watch out"],
            "guardrails": [{"skill": "test", "failure": "flaky"}],
            "memories": [{"type": "pattern", "salience": 9, "similarity": 0.8, "content": "Always verify"}],
        }
        result = format_system_message(ctx)
        assert "[Brain Context]" in result
        assert "⚠️ BRAIN WARNINGS:" in result
        assert "🛡️ RECENT FAILURES:" in result
        assert "🧠 RELEVANT CONTEXT:" in result

    def test_content_truncated_to_150_chars(self):
        long_content = "A" * 300
        ctx = {
            "warnings": [],
            "guardrails": [],
            "memories": [{"type": "lesson", "salience": 5, "similarity": 0.8, "content": long_content}],
        }
        result = format_system_message(ctx)
        # The format truncates to [:150]
        assert "A" * 150 in result
        assert "A" * 151 not in result


# ─── get_context unit tests (mocked DB) ───────────────────────────────


class TestGetContextMocked:
    """Test get_context with mocked DB and embeddings."""

    def _make_uow(self, *execute_results):
        """Create a mock UnitOfWork with sequential execute results.

        Each result is a list of dicts (rows). The UoW session.execute()
        calls return these in order via .mappings().all().
        """
        uow = MagicMock()
        uow.__enter__ = MagicMock(return_value=uow)
        uow.__exit__ = MagicMock(return_value=False)
        uow.__aenter__ = AsyncMock(return_value=uow)
        uow.__aexit__ = AsyncMock(return_value=False)

        call_idx = [0]
        def execute_side_effect(*args, **kwargs):
            result = MagicMock()
            idx = call_idx[0]
            call_idx[0] += 1
            if idx < len(execute_results):
                result.mappings.return_value.all.return_value = execute_results[idx]
            else:
                result.mappings.return_value.all.return_value = []
            return result

        uow.session.execute.side_effect = execute_side_effect
        return uow

    def test_returns_memories_above_threshold(self, mock_embeddings):
        """Memories returned must have similarity > 0.45 (enforced by SQL)."""
        uow = self._make_uow(
            [{"id": 1, "content": "Test memory", "memory_type": "lesson",
              "salience": 8, "emotion_label": "neutral", "similarity": 0.65}],
            [],  # guardrails
            [],  # warnings
        )
        with patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=uow):
            result = get_context("test message")

        assert len(result["memories"]) == 1
        assert result["memories"][0]["similarity"] == 0.65
        assert result["memories"][0]["content"] == "Test memory"

    def test_empty_results(self, mock_embeddings):
        uow = self._make_uow([], [], [])
        with patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=uow), \
             patch("brain.systems.vault.list_secrets", return_value=[]), \
             patch("brain.systems.vault.get_missing_requests", return_value=[]):
            result = get_context("nothing relevant")

        assert result["memories"] == []
        assert result["guardrails"] == []
        assert result["warnings"] == []
        assert format_system_message(result) == ""

    def test_error_captured(self):
        """DB errors should be captured in result, not raised."""
        with patch("brain.systems.memory.embeddings.embed_query", side_effect=Exception("DB down")):
            result = get_context("test")
        assert "error" in result
        assert "DB down" in result["error"]

    def test_guardrails_from_recent_failures(self, mock_embeddings):
        from datetime import datetime
        uow = self._make_uow(
            [],  # memories
            [{"name": "deploy", "outcome_details": "timeout", "error_analysis": "server unreachable", "started_at": datetime.now()}],
            [],  # warnings
        )
        with patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=uow):
            result = get_context("deploying now")

        assert len(result["guardrails"]) == 1
        assert result["guardrails"][0]["skill"] == "deploy"


# ─── Integration test (real DB) ────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("BRAIN_DB_URL") or not os.environ.get("EMBEDDING_API_KEY"),
    reason="No DB config or embedding API key available",
)
class TestGetContextIntegration:
    """Integration tests using the real brain database."""

    def test_real_query_returns_valid_structure(self):
        """Query the real DB and verify result structure."""
        result = get_context("How should I handle deployment failures?")
        assert isinstance(result, dict)
        assert "memories" in result
        assert "guardrails" in result
        assert "warnings" in result
        # Should not have an error if DB is up
        assert "error" not in result, f"DB error: {result.get('error')}"

    def test_real_query_memories_have_required_fields(self):
        result = get_context("testing and code quality")
        for mem in result["memories"]:
            assert "id" in mem
            assert "content" in mem
            assert "type" in mem
            assert "salience" in mem
            assert "similarity" in mem
            assert mem["similarity"] > 0.45

    def test_real_format_produces_string(self):
        result = get_context("what are the engineering principles")
        msg = format_system_message(result)
        assert isinstance(msg, str)
        # If there are results, should have the header
        if result["memories"] or result["warnings"] or result["guardrails"]:
            assert "[Brain Context]" in msg
