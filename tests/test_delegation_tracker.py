"""Tests for services/delegation_tracker.py — issue #72 (ORM-based)."""

import pytest
from unittest.mock import patch, MagicMock


def _make_uow():
    """Create a fresh mock UnitOfWork."""
    uow = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    return uow


class TestDelegationTracker:
    def test_ensure_table(self):
        mock_uow = _make_uow()
        with patch("brain.systems.runs.delegation_tracker.UnitOfWork", return_value=mock_uow):
            from brain.systems.runs.delegation_tracker import ensure_table
            ensure_table()
        assert mock_uow.session.execute.call_count == 3  # CREATE TABLE + 2 indexes

    def test_log_delegation(self):
        uow_calls = [0]
        def make_uow_side_effect():
            uow_calls[0] += 1
            uow = _make_uow()
            if uow_calls[0] == 1:
                # ensure_table UoW
                pass
            else:
                # log_delegation UoW
                uow.session.execute.return_value.mappings.return_value.first.return_value = {"id": 42}
            return uow

        with patch("brain.systems.runs.delegation_tracker.UnitOfWork", side_effect=make_uow_side_effect):
            from brain.systems.runs.delegation_tracker import log_delegation
            result = log_delegation("sess1", "fix bug", "auth fix", "done", 0.8, 1)
        assert result == 42

    def test_get_delegation_stats(self):
        stats_row = {"total": 10, "avg_score": 0.75, "avg_rounds": 1.2, "first_pass_success": 8}
        recent_rows = [{"session_key": "s1", "task_delegated": "t", "quality_score": 0.9,
                         "rounds_needed": 1, "created_at": "2025-01-01"}]

        uow_calls = [0]
        def make_uow_side_effect():
            uow_calls[0] += 1
            uow = _make_uow()
            if uow_calls[0] == 1:
                # ensure_table UoW
                pass
            else:
                # stats UoW: first execute -> stats, second execute -> recent
                exec_count = [0]
                def exec_side_effect(*args, **kwargs):
                    exec_count[0] += 1
                    result = MagicMock()
                    if exec_count[0] == 1:
                        result.mappings.return_value.first.return_value = stats_row
                    elif exec_count[0] == 2:
                        result.mappings.return_value.all.return_value = recent_rows
                    return result
                uow.session.execute.side_effect = exec_side_effect
            return uow

        with patch("brain.systems.runs.delegation_tracker.UnitOfWork", side_effect=make_uow_side_effect):
            from brain.systems.runs.delegation_tracker import get_delegation_stats
            stats = get_delegation_stats(days=30)
        assert stats["total_delegations"] == 10
        assert stats["avg_score"] == 0.75
        assert stats["first_pass_success_rate"] == 80.0

    def test_stats_zero_division(self):
        """Verify no ZeroDivisionError when total is 0."""
        uow_calls = [0]
        def make_uow_side_effect():
            uow_calls[0] += 1
            uow = _make_uow()
            if uow_calls[0] == 1:
                pass  # ensure_table
            else:
                exec_count = [0]
                def exec_side_effect(*args, **kwargs):
                    exec_count[0] += 1
                    result = MagicMock()
                    if exec_count[0] == 1:
                        result.mappings.return_value.first.return_value = {
                            "total": 0, "avg_score": 0, "avg_rounds": 0, "first_pass_success": 0
                        }
                    elif exec_count[0] == 2:
                        result.mappings.return_value.all.return_value = []
                    return result
                uow.session.execute.side_effect = exec_side_effect
            return uow

        with patch("brain.systems.runs.delegation_tracker.UnitOfWork", side_effect=make_uow_side_effect):
            from brain.systems.runs.delegation_tracker import get_delegation_stats
            stats = get_delegation_stats(days=30)
        assert stats["total_delegations"] == 0
        assert stats["first_pass_success_rate"] == 0.0
