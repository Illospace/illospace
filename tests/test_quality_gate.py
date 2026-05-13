"""Tests for memory quality gate."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from brain.systems.quality.gate import check_quality, _adjust_salience, QualityResult


class TestRejectsTooShort:
    async def test_rejects_very_short_default_salience(self):
        result = await check_quality("hi", salience=5.0, skip_duplicate_check=True)
        assert not result.passed
        assert "too short" in result.reason

    async def test_rejects_empty(self):
        result = await check_quality("", salience=5.0, skip_duplicate_check=True)
        assert not result.passed

    async def test_allows_short_with_high_salience(self):
        result = await check_quality("critical note", salience=9.0, skip_duplicate_check=True)
        assert result.passed

    async def test_rejects_short_with_low_salience(self):
        result = await check_quality("some words", salience=3.0, skip_duplicate_check=True)
        assert not result.passed
        assert "too short" in result.reason


class TestRejectsTestArtifacts:
    async def test_rejects_cap_test(self):
        result = await check_quality("cap test", salience=10.0, skip_duplicate_check=True)
        assert not result.passed
        assert "test/debug artifact" in result.reason

    async def test_rejects_test_encode(self):
        result = await check_quality("test encode", salience=5.0, skip_duplicate_check=True)
        assert not result.passed

    async def test_rejects_hello_world(self):
        result = await check_quality("hello world", salience=5.0, skip_duplicate_check=True)
        assert not result.passed


class TestRejectsNearDuplicate:
    @patch("brain.systems.quality.gate._check_near_duplicate", new_callable=AsyncMock)
    async def test_rejects_when_duplicate_found(self, mock_dup):
        mock_dup.return_value = {"id": 42, "similarity": 0.95, "content": "existing memory"}
        result = await check_quality(
            "This is a sufficiently long memory content for testing",
            salience=7.0,
        )
        assert not result.passed
        assert "near-duplicate" in result.reason
        assert "#42" in result.reason

    @patch("brain.systems.quality.gate._check_near_duplicate", new_callable=AsyncMock)
    async def test_passes_when_no_duplicate(self, mock_dup):
        mock_dup.return_value = None
        result = await check_quality(
            "This is a unique memory with enough content to pass length check",
            salience=7.0,
        )
        assert result.passed


class TestAcceptsValidMemory:
    async def test_accepts_good_content(self):
        result = await check_quality(
            "Learned that the backend API should validate input before processing because Alex found a bug in production",
            salience=8.0,
            skip_duplicate_check=True,
        )
        assert result.passed

    async def test_accepts_medium_content(self):
        result = await check_quality(
            "Refactored the database connection pool to use context managers",
            salience=6.0,
            skip_duplicate_check=True,
        )
        assert result.passed


class TestAutoAdjustsSalience:
    def test_short_content_caps_salience(self):
        # 30 chars, salience 8 -> should cap at 5
        adjusted = _adjust_salience("Short but above minimum len!!", 8.0, "fact")
        assert adjusted == 5.0

    def test_long_content_preserves_salience(self):
        adjusted = _adjust_salience("x" * 60, 8.0, "fact")
        assert adjusted == 8.0

    def test_rich_content_gets_boost(self):
        content = "Learned that when we deploy the backend API, Alex found a bug because the database config was wrong"
        adjusted = _adjust_salience(content, 6.0, "lesson")
        assert adjusted == 7.0  # boosted by 1

    def test_boost_caps_at_10(self):
        content = "Learned that when we deploy the backend API, Alex found a bug because the database config was wrong"
        adjusted = _adjust_salience(content, 10.0, "lesson")
        assert adjusted == 10.0

    async def test_quality_gate_returns_adjusted(self):
        result = await check_quality(
            "Short content here!!!!!!!!!!!", salience=9.0, skip_duplicate_check=True
        )
        assert result.passed
        assert result.adjusted_salience == 5.0
