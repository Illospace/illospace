"""Tests for services/completion_reviewer.py — issue #70."""

import pytest
from unittest.mock import patch, MagicMock
from brain.systems.quality.completion import review_completion, _build_follow_up


class TestReviewCompletion:
    @patch("brain.systems.quality.completion.review_output")
    def test_passing_review(self, mock_review):
        mock_review.return_value = MagicMock(passed=True, score=0.9, concerns=[], brain_context_used=False)
        result = review_completion("Fix the bug", "Fixed the bug by adding null check. All tests pass.", files_changed=["test_fix.py", "fix.py"])
        assert result["passed"] is True
        assert result["score"] == 0.9
        assert result["follow_up_prompt"] is None

    @patch("brain.systems.quality.completion.review_output")
    def test_failing_review(self, mock_review):
        mock_review.return_value = MagicMock(passed=False, score=0.3, concerns=["Phantom tests"], brain_context_used=False)
        result = review_completion("Fix the bug", "Done", files_changed=[])
        assert result["passed"] is False
        assert result["follow_up_prompt"] is not None
        assert "Phantom tests" in result["follow_up_prompt"]

    @patch("brain.systems.quality.completion.review_output")
    def test_gap_analysis_populated(self, mock_review):
        mock_review.return_value = MagicMock(passed=False, score=0.4, concerns=["Issue A", "Issue B"], brain_context_used=False)
        result = review_completion("task", "output")
        assert "Issue A" in result["gap_analysis"]
        assert "Issue B" in result["gap_analysis"]


class TestBuildFollowUp:
    def test_includes_original_ask(self):
        prompt = _build_follow_up("Fix login", "output", ["concern1"])
        assert "Fix login" in prompt

    def test_includes_concerns(self):
        prompt = _build_follow_up("task", "output", ["Missing tests", "Shallow output"])
        assert "Missing tests" in prompt
        assert "Shallow output" in prompt
