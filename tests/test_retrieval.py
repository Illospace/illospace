"""Tests for retrieval service.

Uses rollback_db fixture — all writes are rolled back after each test.
Zero test data leaks to production DB.
"""
import pytest
import sys
import os
from unittest.mock import patch

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

pytestmark = [
    pytest.mark.requires_db,
    pytest.mark.skipif(
        not os.environ.get("TEST_DB_URL"),
        reason="TEST_DB_URL not set — run via scripts/test-with-db.sh",
    ),
]

from brain.systems.memory.retrieval import mark_relevant, get_retrieval_stats, preprocess_query


class TestMarkRelevant:
    """Test feedback marking on retrieval log entries."""

    async def test_mark_existing_entry_relevant(self, db_session, unit_of_work_for_session):
        """Should update an existing retrieval log entry."""
        result = await db_session.execute(text("""
            INSERT INTO retrieval_log (query_text, results_returned, top_result_id, top_score)
            VALUES ('test query', 1, NULL, 0.85) RETURNING id
        """))
        row = result.mappings().first()
        log_id = row["id"]

        with patch("brain.systems.memory.retrieval.UnitOfWork", unit_of_work_for_session):
            result = await mark_relevant(log_id, True)
        assert result is True

        result = await db_session.execute(
            text("SELECT feedback, was_relevant FROM retrieval_log WHERE id = :id"),
            {"id": log_id},
        )
        updated = result.mappings().first()
        assert updated["feedback"] == "hit"
        assert updated["was_relevant"] is True

    async def test_mark_existing_entry_not_relevant(self, db_session, unit_of_work_for_session):
        result = await db_session.execute(text("""
            INSERT INTO retrieval_log (query_text, results_returned, top_result_id, top_score)
            VALUES ('test query 2', 1, NULL, 0.75) RETURNING id
        """))
        row = result.mappings().first()
        log_id = row["id"]

        with patch("brain.systems.memory.retrieval.UnitOfWork", unit_of_work_for_session):
            result = await mark_relevant(log_id, False)
        assert result is True

        result = await db_session.execute(
            text("SELECT feedback FROM retrieval_log WHERE id = :id"),
            {"id": log_id},
        )
        row = result.mappings().first()
        assert row["feedback"] == "miss"

    async def test_mark_nonexistent_returns_false(self, db_session, unit_of_work_for_session):
        with patch("brain.systems.memory.retrieval.UnitOfWork", unit_of_work_for_session):
            result = await mark_relevant(999999, True)
        assert result is False


class TestGetRetrievalStats:
    """Test retrieval statistics."""

    async def test_returns_expected_keys(self, db_session, unit_of_work_for_session):
        with patch("brain.systems.memory.retrieval.UnitOfWork", unit_of_work_for_session):
            stats = await get_retrieval_stats()
        assert "total" in stats
        assert "with_feedback" in stats
        assert "hits" in stats
        assert "misses" in stats
        assert "hit_rate" in stats
        assert "avg_top_score" in stats

    async def test_stats_are_numeric(self, db_session, unit_of_work_for_session):
        with patch("brain.systems.memory.retrieval.UnitOfWork", unit_of_work_for_session):
            stats = await get_retrieval_stats()
        assert isinstance(stats["total"], int)
        assert isinstance(stats["hit_rate"], float)
        assert 0 <= stats["hit_rate"] <= 1


class TestPreprocessQuery:
    """Test query preprocessing with LLM."""

    def test_preprocesses_emotional_message(self):
        result = preprocess_query("this is still broken, I told you already")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_short_message_still_works(self):
        result = preprocess_query("what happened yesterday")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_string_on_fallback(self):
        """Even if LLM fails, should return the raw message."""
        result = preprocess_query("test query")
        assert isinstance(result, str)
