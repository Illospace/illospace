"""Tests for token derivation and token metrics CLI.

The _capture_session_tokens / dashboard.agent_runs path was removed.
Only the formula unit test and the CLI report test remain.
"""
from unittest.mock import AsyncMock, patch


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

    async def test_report_returns_expected_keys(self):
        """Verify report structure."""
        from brain.app.cli.token_metrics import report

        runs = [
            {
                "id": index,
                "thread_id": f"thread-{index}",
                "skill_used": "code",
                "model_used": "anthropic/claude-opus-4-6",
                "tokens_total": 25000,
                "tokens_input": 23750,
                "tokens_output": 1250,
                "cache_read": 12500,
                "cache_write": 11250,
                "estimated_cost": 0.25,
                "status": "completed" if index < 7 else "failed",
                "created_at": None,
            }
            for index in range(8)
        ] + [
            {
                "id": 8 + index,
                "thread_id": f"thread-missing-{index}",
                "skill_used": "code",
                "model_used": "anthropic/claude-opus-4-6",
                "tokens_total": 0,
                "tokens_input": 0,
                "tokens_output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "estimated_cost": 0.0,
                "status": "completed",
                "created_at": None,
            }
            for index in range(2)
        ]

        with patch("brain.app.cli.token_metrics._runs_for_period", new=AsyncMock(return_value=runs)):
            result = await report(days=7)

        assert "overall" in result
        assert "token_tracking_coverage_pct" in result
        assert "by_model" in result
        assert "by_skill" in result
        assert "cache_efficiency" in result
        assert result["token_tracking_coverage_pct"] == 80.0
