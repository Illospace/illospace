"""Tests for cost calculation accuracy."""
import pytest


class TestCostCalculation:
    """Test that calculate_cost properly handles pricing."""

    def test_basic_cost_backward_compatible(self):
        """Without cache args, cost = input_rate * input + output_rate * output."""
        from brain.systems.runs.modeling import calculate_cost

        cost = calculate_cost("anthropic/claude-opus-5", 1_000_000, 100_000)
        # opus: $5/M input + $25/M output
        expected = 5.0 + 2.5
        assert abs(cost - expected) < 0.001

    def test_local_models_free(self):
        """Local/gpu_server models should always return 0 cost."""
        from brain.systems.runs.modeling import calculate_cost

        assert calculate_cost("local/my-model", 1_000_000, 500_000) == 0.0
        assert calculate_cost("gpu_server/whatever", 1_000_000, 500_000) == 0.0

    def test_sonnet_pricing(self):
        """Sonnet pricing should be $3/M input, $15/M output."""
        from brain.systems.runs.modeling import calculate_cost

        cost = calculate_cost("anthropic/claude-sonnet-5", 1_000_000, 1_000_000)
        expected = 3.0 + 15.0
        assert abs(cost - expected) < 0.001

    def test_haiku_pricing(self):
        """Haiku pricing should be $1/M input, $5/M output."""
        from brain.systems.runs.modeling import calculate_cost

        cost = calculate_cost("anthropic/claude-haiku-4-5", 1_000_000, 1_000_000)
        expected = 1.0 + 5.0
        assert abs(cost - expected) < 0.001

    def test_zero_tokens(self):
        """Zero tokens should produce zero cost."""
        from brain.systems.runs.modeling import calculate_cost

        cost = calculate_cost("anthropic/claude-opus-5", 0, 0)
        assert cost == 0.0

    def test_unknown_model_defaults_to_default_openai_pricing(self):
        """Unknown models should default to the configured OpenAI default pricing baseline."""
        from brain.systems.runs.modeling import calculate_cost

        cost = calculate_cost("unknown-model", 1_000_000, 1_000_000)
        expected = 5.0 + 30.0
        assert abs(cost - expected) < 0.001

    def test_local_model_keyword_free(self):
        """Models with 'local' in name should be free."""
        from brain.systems.runs.modeling import calculate_cost

        assert calculate_cost("local/my-model", 1_000_000, 500_000) == 0.0
