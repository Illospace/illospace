# tests/test_skill_gap_tool.py
"""Tests for the flag_skill_gap coordinator tool."""
import pytest


def test_flag_skill_gap_handler_returns_action():
    """Handler should return structured action response."""
    from brain.systems.runs.skill_gap import handle_flag_skill_gap as _handle_flag_skill_gap

    result = _handle_flag_skill_gap(
        task_domain="mobile development",
        closest_skill="frontend-design (doesn't cover native mobile)",
        suggested_skill_name="mobile-development",
    )
    assert result["gap_acknowledged"] is True
    assert "mobile-development" in result["action"]


def test_flag_skill_gap_handler_logs():
    """Handler should log the gap for observability."""
    from unittest.mock import patch
    from brain.systems.runs.skill_gap import handle_flag_skill_gap as _handle_flag_skill_gap

    with patch("brain.systems.runs.skill_gap.logger") as mock_logger:
        _handle_flag_skill_gap("data-science", "debugging", "data-analysis")
        mock_logger.info.assert_called_once()
        assert "skill_gap_flagged" in str(mock_logger.info.call_args)
