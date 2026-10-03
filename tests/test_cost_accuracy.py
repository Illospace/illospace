"""Tests for cost calculation accuracy."""

import pytest
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


def test_sol_pricing_boundary_cache_and_output_multiplier():
    from brain.platform.providers.model_policy import calculate_model_cost

    assert calculate_model_cost("gpt-6.1-sol", 272_000, 1000, cache_read=200_000) == 0.174
    assert calculate_model_cost("gpt-6.1-sol", 272_001, 1000, cache_read=200_000) == 0.343004
    assert calculate_model_cost("gpt-6.1-sol", 100_000, 1000, cache_read=100_000) == 0.02
    assert calculate_model_cost("gpt-6.1-sol", 100_000, 0, cache_write=100_000) == 0.25
    # Aggregated short requests must stay short even when their total is >272K.
    assert calculate_model_cost("gpt-6.1-sol", 400_000, 2000, long_context=False) == 0.82


@pytest.mark.asyncio
async def test_request_pricing_band_survives_database_aggregation():
    from datetime import datetime, timezone
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from brain.systems.runs.token_usage import _run_usage_call_stmt, _member_cost_stmt, _model_cost

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE agent_runs (id INTEGER PRIMARY KEY, org_id TEXT, user_id TEXT)"))
        await conn.execute(text("CREATE TABLE agent_api_calls (id INTEGER PRIMARY KEY, run_id INTEGER, model TEXT, effort TEXT, tokens_input INTEGER, tokens_output INTEGER, cache_read INTEGER, cache_write INTEGER, created_at TIMESTAMP)"))
        await conn.execute(text("INSERT INTO agent_runs VALUES (1, 'org', 'user')"))
        for call_id, tokens in enumerate((200_000, 200_000, 300_000), start=1):
            await conn.execute(text("INSERT INTO agent_api_calls VALUES (:id, 1, 'openai/gpt-6.1-sol', 'medium', :tokens, 1000, 100000, 0, '2026-10-03 00:00:00')"), {"id": call_id, "tokens": tokens})
        for stmt in (_run_usage_call_stmt([1]), _member_cost_stmt(org_id='org', since=datetime(2026, 1, 1, tzinfo=timezone.utc))):
            rows = (await conn.execute(stmt)).all()
            assert len(rows) == 2
            assert sum(_model_cost(row) for row in rows) == 1.275

    await engine.dispose()


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_postgres_can_group_per_request_price_bands(db_engine):
    from sqlalchemy import text
    from brain.systems.runs.token_usage import _run_usage_call_stmt, _model_cost

    async with db_engine.begin() as conn:
        # Temporary table shadows the real table on this connection only.
        await conn.execute(text("CREATE TEMP TABLE agent_api_calls (id BIGINT, run_id INTEGER, model TEXT, effort TEXT, tokens_input INTEGER, tokens_output INTEGER, cache_read INTEGER, cache_write INTEGER, created_at TIMESTAMPTZ) ON COMMIT DROP"))
        await conn.execute(text("INSERT INTO agent_api_calls VALUES (1, 1, 'openai/gpt-6.1-sol', 'medium', 200000, 1000, 100000, 0, now()), (2, 1, 'openai/gpt-6.1-sol', 'medium', 300000, 1000, 100000, 0, now())"))
        rows = (await conn.execute(_run_usage_call_stmt([1]))).all()
        assert len(rows) == 2
        assert sum(_model_cost(row) for row in rows) == 1.055
