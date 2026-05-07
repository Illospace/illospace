"""Tests for token derivation and token metrics CLI.

The _capture_session_tokens / dashboard.agent_runs path was removed.
Only the formula unit test and the CLI report test remain.
"""
import pytest
from unittest.mock import patch, MagicMock


class TestTokenDerivation:
    """Test that token derivation formula is correct."""

    def test_token_derivation_formula(self):
        """Verify the derivation formula: input = total - output."""
        cases = [
            # (raw_inputTokens, outputTokens, totalTokens) -> expected_derived_input
            (3, 277, 34407, 34130),
            (3, 249, 19710, 19461),
            (3, 175, 30480, 30305),
            (3, 169, 17109, 16940),
            (0, 0, 0, 0),
            (100, 500, 10000, 9500),
        ]
        for raw_input, output, total, expected_derived in cases:
            derived = max(0, total - output)
            assert derived == expected_derived, (
                f"For total={total}, output={output}: "
                f"expected derived_input={expected_derived}, got {derived}"
            )


class TestTokenMetricsCLI:
    """Integration tests for cli/token_metrics.py."""

    @patch("brain.platform.db.repositories.unit_of_work.UnitOfWork")
    def test_report_returns_expected_keys(self, MockUoW):
        """Verify report structure."""
        uow = MagicMock()
        uow.__enter__ = MagicMock(return_value=uow)
        uow.__exit__ = MagicMock(return_value=False)

        # Set up chained results for sequential execute() calls
        overall_result = MagicMock()
        overall_result.__iter__ = MagicMock(return_value=iter([]))
        overall_mappings = MagicMock()
        overall_mappings.first.return_value = {
            "total_runs": 10, "with_token_data": 8,
            "missing_token_data": 2, "completed": 9, "failed": 1,
            "timed_out": 0, "total_tokens": 200000,
            "total_input": 190000, "total_output": 10000,
            "total_cache_read": 100000, "total_cache_write": 90000,
            "avg_tokens_total": 20000, "avg_tokens_input": 19000,
            "avg_tokens_output": 1000, "max_tokens_total": 35000,
            "min_tokens_total": 15000, "total_cost": 2.50,
            "avg_cost": 0.25,
        }

        cache_result = MagicMock()
        cache_mappings = MagicMock()
        cache_mappings.first.return_value = {
            "avg_cache_hit_pct": 52.3, "avg_cache_read": 10000, "avg_cache_write": 9000,
        }

        empty_mappings = MagicMock()
        empty_mappings.all.return_value = []

        # Create mock results for each execute call
        results = [
            MagicMock(mappings=MagicMock(return_value=overall_mappings)),
            MagicMock(mappings=MagicMock(return_value=cache_mappings)),
            MagicMock(mappings=MagicMock(return_value=empty_mappings)),   # by_model
            MagicMock(mappings=MagicMock(return_value=empty_mappings)),   # by_skill
            MagicMock(mappings=MagicMock(return_value=empty_mappings)),   # top_expensive
        ]
        uow.session.execute.side_effect = results
        MockUoW.return_value = uow

        from brain.app.cli.token_metrics import report
        result = report(days=7)
        assert "overall" in result
        assert "token_tracking_coverage_pct" in result
        assert "by_model" in result
        assert "by_skill" in result
        assert "cache_efficiency" in result
        assert result["token_tracking_coverage_pct"] == 80.0
