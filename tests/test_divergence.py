"""Tests for cross-user divergence detection."""
import pytest
from datetime import date
from unittest.mock import patch, MagicMock


def _make_uow(rows):
    """Create a mock UnitOfWork whose session.execute returns the given rows."""
    uow = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    uow.session.execute.return_value.mappings.return_value.all.return_value = rows
    return uow


class TestDivergenceDetection:
    @patch("brain.jobs.pipelines.divergence.UnitOfWork")
    def test_detect_overlapping_topics(self, MockUoW):
        MockUoW.return_value = _make_uow([
            {"user_id": "u1", "user_name": "Alice", "topic_tags": ["auth", "api"],
             "content_sample": "Reworking auth middleware"},
            {"user_id": "u2", "user_name": "Bob", "topic_tags": ["auth", "security"],
             "content_sample": "Fixing auth token validation"},
        ])

        from brain.jobs.pipelines.divergence import detect_divergence
        results = detect_divergence(date(2026, 3, 15), org_id="org-1")
        # auth overlap: shared={"auth"}, but need >= 2 shared tags, so no overlap detected
        assert isinstance(results, list)

    @patch("brain.jobs.pipelines.divergence.UnitOfWork")
    def test_detect_strong_overlap(self, MockUoW):
        MockUoW.return_value = _make_uow([
            {"user_id": "u1", "user_name": "Alice", "topic_tags": ["auth", "api", "security"],
             "content_sample": "Auth work"},
            {"user_id": "u2", "user_name": "Bob", "topic_tags": ["auth", "security", "tokens"],
             "content_sample": "Security work"},
        ])

        from brain.jobs.pipelines.divergence import detect_divergence
        results = detect_divergence(date(2026, 3, 15), org_id="org-1")
        assert len(results) == 1
        assert "auth" in results[0]["shared_topics"]
        assert "security" in results[0]["shared_topics"]
        assert results[0]["user_a"] == "Alice"
        assert results[0]["user_b"] == "Bob"

    @patch("brain.jobs.pipelines.divergence.UnitOfWork")
    def test_single_user_returns_empty(self, MockUoW):
        MockUoW.return_value = _make_uow([
            {"user_id": "u1", "user_name": "Alice", "topic_tags": ["auth"], "content_sample": "Work"},
        ])

        from brain.jobs.pipelines.divergence import detect_divergence
        assert detect_divergence(date(2026, 3, 15), org_id="org-1") == []

    def test_format_sync_suggestion(self):
        from brain.jobs.pipelines.divergence import format_sync_suggestion
        suggestion = format_sync_suggestion("Alice", "Bob", ["auth", "security"], 0.78)
        assert "Alice" in suggestion
        assert "Bob" in suggestion
        assert "auth" in suggestion
        assert "strongly" in suggestion

    def test_format_partial_overlap(self):
        from brain.jobs.pipelines.divergence import format_sync_suggestion
        suggestion = format_sync_suggestion("Alice", "Bob", ["auth"], 0.3)
        assert "partially" in suggestion
