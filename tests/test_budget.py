"""Tests for core.budget — Budget Guardian (circuit breaker pattern)."""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from brain.systems.budget import (
    check_budget,
    estimate_run_tokens,
    get_budget_status,
    BudgetDecision,
    CIRCUIT_BREAKER_PER_IDEA_HOUR,
    CIRCUIT_BREAKER_PER_DAY,
    WARN_PER_IDEA_HOUR,
    WARN_PER_DAY,
)


def _make_uow(*fetchone_results):
    """Return a mock UnitOfWork whose session.execute chains return mapping results.

    Each call to uow.session.execute(...).mappings().first() returns the next
    item from *fetchone_results*.
    """
    mock_session = MagicMock()

    # Build a side_effect list: each execute() call returns a mock whose
    # .mappings().first() returns the next result dict.
    execute_results = []
    for row in fetchone_results:
        mapping_mock = MagicMock()
        mapping_mock.first.return_value = row
        mapping_mock.__getitem__ = lambda self, key, _r=row: _r[key]
        exec_mock = MagicMock()
        exec_mock.mappings.return_value = mapping_mock
        execute_results.append(exec_mock)

    mock_session.execute.side_effect = execute_results

    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_uow.session = mock_session
    return mock_uow


class TestBudgetDecision:
    def test_allowed(self):
        d = BudgetDecision(allowed=True)
        assert d.allowed
        assert d.reason == ""
        assert d.downgrade_model is None

    def test_blocked(self):
        d = BudgetDecision(allowed=False, reason="circuit breaker")
        assert not d.allowed
        assert "circuit breaker" in d.reason

    def test_warning(self):
        d = BudgetDecision(allowed=True, warning="High usage: 400K/hr")
        assert d.allowed
        assert d.warning is not None
        assert not d.closure_mode

    def test_repr(self):
        d = BudgetDecision(allowed=True, reason="ok")
        assert "allowed=True" in repr(d)

    def test_closure_mode(self):
        d = BudgetDecision(allowed=True, warning="closure", closure_mode=True)
        assert d.allowed
        assert d.closure_mode


class TestEstimateTokens:
    def test_short_message(self):
        tokens = estimate_run_tokens("Hello world")
        # ~3 chars = 0 tokens from content + 5000 overhead
        assert tokens >= 5000

    def test_long_message(self):
        msg = "x" * 40000  # 10K tokens from content
        tokens = estimate_run_tokens(msg)
        assert tokens >= 15000  # content + overhead


class TestCheckBudget:
    def test_within_budget(self):
        """Normal run well within circuit breaker thresholds."""
        mock_uow = _make_uow(
            {"tokens_used": 10000},   # hourly: 10K used
            {"tokens_used": 50000},   # daily: 50K used
        )
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow):
            result = check_budget("test-idea-id", 10000, model="anthropic/claude-opus-4-6")
        assert result.allowed
        assert result.downgrade_model is None
        assert result.warning is None

    def test_warning_on_high_hourly_usage(self):
        """Logs warning on high hourly usage but still allows run."""
        mock_uow = _make_uow(
            {"tokens_used": WARN_PER_IDEA_HOUR + 1000},  # above warning
            {"tokens_used": 50000},                        # daily fine
        )
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow):
            result = check_budget("test-idea-id", 10000)
        assert result.allowed  # never blocks on warning
        assert result.warning is not None

    def test_circuit_breaker_hourly(self):
        """Circuit breaker fires on catastrophic hourly token usage."""
        mock_uow = _make_uow(
            {"tokens_used": CIRCUIT_BREAKER_PER_IDEA_HOUR + 1},  # over 1M
        )
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow):
            result = check_budget("test-idea-id", 10000)
        assert not result.allowed
        assert "circuit breaker" in result.reason.lower()

    def test_repair_task_gets_hourly_closure_grace(self):
        mock_uow = _make_uow(
            {"tokens_used": CIRCUIT_BREAKER_PER_IDEA_HOUR + 100000},
            {"tokens_used": 50000},
        )
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow):
            result = check_budget(
                "test-idea-id",
                40000,
                task_description="Fix brain DB migration and cursor recall bug",
            )
        assert result.allowed
        assert result.closure_mode
        assert "closure mode" in result.warning.lower()

    def test_circuit_breaker_daily(self):
        """Circuit breaker fires on catastrophic daily token usage."""
        mock_uow = _make_uow(
            {"tokens_used": 50000},                        # hourly fine
            {"tokens_used": CIRCUIT_BREAKER_PER_DAY + 1},  # daily over 10M
        )
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow):
            result = check_budget("test-idea-id", 10000)
        assert not result.allowed
        assert "circuit breaker" in result.reason.lower()

    def test_repair_task_gets_daily_closure_grace(self):
        mock_uow = _make_uow(
            {"tokens_used": 50000},
            {"tokens_used": CIRCUIT_BREAKER_PER_DAY + 1000000},
        )
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow):
            result = check_budget(
                "test-idea-id",
                40000,
                task_description="Investigate brain recall transaction failure",
            )
        assert result.allowed
        assert result.closure_mode

    def test_large_repair_run_still_blocked(self):
        mock_uow = _make_uow(
            {"tokens_used": CIRCUIT_BREAKER_PER_IDEA_HOUR + 100000},
            {"tokens_used": 50000},
        )
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow):
            result = check_budget(
                "test-idea-id",
                250000,
                task_description="Fix brain DB migration and cursor recall bug",
            )
        assert not result.allowed

    def test_opus_never_downgraded(self):
        """Opus model is NEVER downgraded — model choice is not budget's job."""
        mock_uow = _make_uow(
            {"tokens_used": WARN_PER_IDEA_HOUR + 50000},  # high but under breaker
            {"tokens_used": WARN_PER_DAY + 100000},        # high but under breaker
        )
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow):
            result = check_budget("test-idea-id", 50000, model="anthropic/claude-opus-4-6")
        assert result.allowed
        assert result.downgrade_model is None
        assert result.downgrade_thinking is None

    def test_db_error_fails_open(self):
        """On DB error, allow the run (fail-open)."""
        with patch("brain.systems.budget.UnitOfWork", side_effect=Exception("DB connection failed")):
            result = check_budget("test-idea-id", 10000)
        assert result.allowed
        assert "error" in result.reason.lower()


class TestBudgetStatus:
    def test_returns_status(self):
        mock_uow = _make_uow(
            {"daily_tokens": 100000, "daily_runs": 10, "daily_cost": 1.5},
            {"hourly_tokens": 20000, "hourly_runs": 3},
        )
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow):
            status = get_budget_status()
        assert status["daily_tokens"] == 100000
        assert status["daily_runs"] == 10
        assert "daily_pct" in status
        assert "daily_circuit_breaker" in status
        assert "daily_warn" in status
