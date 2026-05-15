"""Tests for core.budget — Budget Guardian (circuit breaker pattern)."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

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


def _make_uow():
    """Return a mock UnitOfWork; token totals are supplied by helper patches."""
    mock_uow = MagicMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    mock_uow.session = MagicMock()
    return mock_uow


def _token_usage(tokens_total: int, *, runs: int = 1, estimated_cost: float = 0.0) -> dict:
    return {
        "runs": runs,
        "api_calls": runs,
        "tokens_input": tokens_total,
        "tokens_output": 0,
        "tokens_total": tokens_total,
        "cache_read": 0,
        "cache_write": 0,
        "estimated_cost": estimated_cost,
        "last_used_at": None,
    }


def _patch_budget_usage(*token_totals: int):
    return patch(
        "brain.systems.budget._async_summarize_token_totals",
        new=AsyncMock(side_effect=[_token_usage(total) for total in token_totals]),
    )


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


@pytest.mark.asyncio
class TestCheckBudget:
    async def test_within_budget(self):
        """Normal run well within circuit breaker thresholds."""
        mock_uow = _make_uow()
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow), \
             _patch_budget_usage(10000, 50000):
            result = await check_budget("test-idea-id", 10000, model="anthropic/claude-opus-4-6")
        assert result.allowed
        assert result.reason == ""
        assert result.downgrade_model is None
        assert result.warning is None

    async def test_warning_on_high_hourly_usage(self):
        """Logs warning on high hourly usage but still allows run."""
        mock_uow = _make_uow()
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow), \
             _patch_budget_usage(WARN_PER_IDEA_HOUR + 1000, 50000):
            result = await check_budget("test-idea-id", 10000)
        assert result.allowed  # never blocks on warning
        assert result.warning is not None

    async def test_circuit_breaker_hourly(self):
        """Circuit breaker fires on catastrophic hourly token usage."""
        mock_uow = _make_uow()
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow), \
             _patch_budget_usage(CIRCUIT_BREAKER_PER_IDEA_HOUR + 1):
            result = await check_budget("test-idea-id", 10000)
        assert not result.allowed
        assert "circuit breaker" in result.reason.lower()

    async def test_repair_task_gets_hourly_closure_grace(self):
        mock_uow = _make_uow()
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow), \
             _patch_budget_usage(CIRCUIT_BREAKER_PER_IDEA_HOUR + 100000):
            result = await check_budget(
                "test-idea-id",
                40000,
                task_description="Fix brain DB migration and cursor recall bug",
            )
        assert result.allowed
        assert result.closure_mode
        assert "closure mode" in result.warning.lower()

    async def test_circuit_breaker_daily(self):
        """Circuit breaker fires on catastrophic daily token usage."""
        mock_uow = _make_uow()
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow), \
             _patch_budget_usage(50000, CIRCUIT_BREAKER_PER_DAY + 1):
            result = await check_budget("test-idea-id", 10000)
        assert not result.allowed
        assert "circuit breaker" in result.reason.lower()

    async def test_repair_task_gets_daily_closure_grace(self):
        mock_uow = _make_uow()
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow), \
             _patch_budget_usage(50000, CIRCUIT_BREAKER_PER_DAY + 1000000):
            result = await check_budget(
                "test-idea-id",
                40000,
                task_description="Investigate brain recall transaction failure",
            )
        assert result.allowed
        assert result.closure_mode

    async def test_large_repair_run_still_blocked(self):
        mock_uow = _make_uow()
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow), \
             _patch_budget_usage(CIRCUIT_BREAKER_PER_IDEA_HOUR + 100000):
            result = await check_budget(
                "test-idea-id",
                250000,
                task_description="Fix brain DB migration and cursor recall bug",
            )
        assert not result.allowed

    async def test_opus_never_downgraded(self):
        """Opus model is NEVER downgraded — model choice is not budget's job."""
        mock_uow = _make_uow()
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow), \
             _patch_budget_usage(WARN_PER_IDEA_HOUR + 50000, WARN_PER_DAY + 100000):
            result = await check_budget("test-idea-id", 50000, model="anthropic/claude-opus-4-6")
        assert result.allowed
        assert result.downgrade_model is None
        assert result.downgrade_thinking is None

    async def test_db_error_fails_open(self):
        """On DB error, allow the run (fail-open)."""
        with patch("brain.systems.budget.UnitOfWork", side_effect=Exception("DB connection failed")):
            result = await check_budget("test-idea-id", 10000)
        assert result.allowed
        assert "error" in result.reason.lower()


@pytest.mark.asyncio
class TestBudgetStatus:
    async def test_returns_status(self):
        mock_uow = _make_uow()
        with patch("brain.systems.budget.UnitOfWork", return_value=mock_uow), \
             patch(
                 "brain.systems.budget._async_summarize_token_totals",
                 new=AsyncMock(side_effect=[
                     _token_usage(100000, runs=10, estimated_cost=1.5),
                     _token_usage(20000, runs=3),
                 ]),
             ):
            status = await get_budget_status()
        assert status["daily_tokens"] == 100000
        assert status["daily_runs"] == 10
        assert "daily_pct" in status
        assert "daily_circuit_breaker" in status
        assert "daily_warn" in status
