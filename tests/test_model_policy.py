"""Tests for shared model/provider policy."""

from unittest.mock import MagicMock, patch

import pytest


class _AsyncMappingResult:
    def __init__(self, *, first=None, all=None):
        self._first = first
        self._all = list(all or [])

    def mappings(self):
        return self

    def first(self):
        return self._first

    def all(self):
        return list(self._all)


class _AsyncPolicySession:
    def __init__(self, execute):
        self._execute = execute

    async def execute(self, stmt, params=None):
        return self._execute(stmt, params or {})


class TestModelPolicy:
    def test_default_model_tracks_active_provider(self):
        from brain.platform.providers.model_policy import get_default_model

        with patch("brain.platform.providers.model_policy.get_active_provider", return_value="openai"):
            assert get_default_model() == "openai/gpt-5.4"

    def test_openai_tier_map_uses_native_defaults(self):
        from brain.platform.providers.model_policy import get_provider_model_map

        model_map = get_provider_model_map("openai")
        assert model_map["high"] == "gpt-5.5"
        assert model_map["medium"] == "gpt-5.4"
        assert model_map["low"] == "gpt-5-mini"

    @pytest.mark.asyncio
    async def test_org_provider_model_map_overrides_defaults(self):
        from brain.platform.providers.model_policy import async_get_provider_model_map

        def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            if "SELECT org_id FROM users" in sql:
                return _AsyncMappingResult(first={"org_id": "org-1"})
            if "FROM org_provider_model_mappings" in sql:
                return _AsyncMappingResult(all=[
                    {"intelligence_level": "medium", "model_name": "gpt-5.5"},
                    {"intelligence_level": "low", "model_name": "gpt-5.5-mini"},
                ])
            raise AssertionError(f"Unexpected SQL: {sql}")

        model_map = await async_get_provider_model_map(
            _AsyncPolicySession(execute_side_effect),
            "openai",
            user_id="user-1",
        )

        assert model_map["medium"] == "gpt-5.5"
        assert model_map["low"] == "gpt-5.5-mini"
        assert model_map["high"] == "gpt-5.5"

    def test_infer_provider_recognizes_new_openai_models(self):
        from brain.platform.providers.model_policy import infer_provider_from_model

        assert infer_provider_from_model("gpt-5.4") == "openai"
        assert infer_provider_from_model("gpt-5.4-mini") == "openai"
        assert infer_provider_from_model("openai:gpt-5.4") == "openai"
        assert infer_provider_from_model("gpt-5.4-pro") == "openai"
        assert infer_provider_from_model("gpt-5.5") == "openai"

    def test_infer_provider_recognizes_claude_family_names(self):
        from brain.platform.providers.model_policy import infer_provider_from_model

        assert infer_provider_from_model("claude-haiku-4-20250414", default="openai") == "anthropic"
        assert infer_provider_from_model("claude-sonnet-4-6") == "anthropic"

    def test_cost_normalizes_openai_models(self):
        from brain.platform.providers.model_policy import calculate_model_cost, normalize_model_name

        cost = calculate_model_cost("gpt-4o-mini", 1_000_000, 1_000_000)
        assert abs(cost - 3.0) < 0.001
        assert normalize_model_name("gpt-5.4-mini") == "openai/gpt-5.4-mini"
        assert normalize_model_name("gpt-5.4-nano") == "openai/gpt-5.4-nano"
        assert normalize_model_name("gpt-5.5") == "openai/gpt-5.5"

    def test_cost_uses_native_pricing_for_default_openai_models(self):
        from brain.platform.providers.model_policy import calculate_model_cost

        assert abs(calculate_model_cost("gpt-5.4", 1_000_000, 1_000_000) - 17.5) < 0.001
        assert abs(calculate_model_cost("gpt-5.5", 1_000_000, 1_000_000) - 35.0) < 0.001
        assert abs(calculate_model_cost("gpt-5.4-pro", 1_000_000, 1_000_000) - 210.0) < 0.001
        assert abs(calculate_model_cost("gpt-5.4-mini", 1_000_000, 1_000_000) - 5.25) < 0.001
        assert abs(calculate_model_cost("gpt-5-mini", 1_000_000, 1_000_000) - 2.25) < 0.001
        assert abs(calculate_model_cost("gpt-5-nano", 1_000_000, 1_000_000) - 0.45) < 0.001

    def test_cost_applies_cached_input_discount_to_cached_subset(self):
        from brain.platform.providers.model_policy import calculate_model_cost

        cost = calculate_model_cost(
            "gpt-5.5",
            449_475,
            12_846,
            cache_read=191_488,
        )

        assert abs(cost - 1.771059) < 0.000001

    def test_unknown_model_still_defaults_to_medium_pricing(self):
        from brain.platform.providers.model_policy import calculate_model_cost

        cost = calculate_model_cost("unknown-model", 1_000_000, 1_000_000)
        assert abs(cost - 17.5) < 0.001

    def test_infer_provider_from_prefixed_model(self):
        from brain.platform.providers.model_policy import infer_provider_from_model

        assert infer_provider_from_model("openai/gpt-4o-mini") == "openai"
        assert infer_provider_from_model("anthropic/claude-sonnet-4-6") == "anthropic"

    @pytest.mark.asyncio
    async def test_resolve_skill_runtime_uses_tiers_with_selected_provider(self):
        from brain.platform.providers.model_policy import async_resolve_skill_model, async_resolve_skill_runtime

        row = {
            "model_tier": "high",
            "thinking_tier": "xhigh",
        }

        def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            if "SELECT model_tier, thinking_tier" in sql:
                return _AsyncMappingResult(first=row)
            if "SELECT org_id FROM users" in sql:
                return _AsyncMappingResult(first={"org_id": "org-1"})
            if "FROM org_provider_model_mappings" in sql:
                return _AsyncMappingResult(all=[])
            raise AssertionError(f"Unexpected SQL: {sql}")

        session = _AsyncPolicySession(execute_side_effect)
        runtime = await async_resolve_skill_runtime(session, "deploy", user_id="user-1")
        model, thinking = await async_resolve_skill_model(session, "deploy", user_id="user-1")

        assert runtime.provider == "openai"
        assert runtime.model_name == "gpt-5.5"
        assert runtime.reasoning_effort == "xhigh"
        assert model == "openai/gpt-5.5"
        assert thinking == "xhigh"

    def test_run_resolve_model_fallback_uses_selected_provider(self):
        from brain.systems.runs.modeling import resolve_model

        with patch("brain.systems.runs.modeling.resolve_skill_model", side_effect=RuntimeError("boom")), \
             patch("brain.systems.runs.modeling.get_default_model", return_value="openai/gpt-5.4"):
            model, thinking = resolve_model("missing-skill", user_id="user-1", preferred_provider="openai")

        assert model == "openai/gpt-5.4"
        assert thinking == "medium"

    @pytest.mark.asyncio
    async def test_resolve_default_provider_uses_org_default(self):
        from brain.platform.providers.model_policy import async_resolve_default_provider

        results = [
            _AsyncMappingResult(first={"org_id": "org-1"}),
            _AsyncMappingResult(first={"memory_model_config": {"default_provider": "openai"}}),
        ]
        session = _AsyncPolicySession(lambda stmt, params=None: results.pop(0))
        assert await async_resolve_default_provider(session, user_id="user-1") == "openai"

    @pytest.mark.asyncio
    async def test_resolve_default_provider_coerces_legacy_org_anthropic_to_openai(self):
        from brain.platform.providers.model_policy import async_resolve_default_provider

        results = [
            _AsyncMappingResult(first={"org_id": "org-1"}),
            _AsyncMappingResult(first={
                "memory_model_config": {"default_provider": "anthropic"},
            }),
        ]
        session = _AsyncPolicySession(lambda stmt, params=None: results.pop(0))
        assert await async_resolve_default_provider(session, user_id="user-1") == "openai"

    @pytest.mark.asyncio
    async def test_resolve_default_provider_uses_preferred_provider_as_fallback(self):
        from brain.platform.providers.model_policy import async_resolve_default_provider

        results = [
            _AsyncMappingResult(first={"org_id": "org-1"}),
            _AsyncMappingResult(first={"memory_model_config": {}}),
        ]
        session = _AsyncPolicySession(lambda stmt, params=None: results.pop(0))
        assert await async_resolve_default_provider(
            session,
            user_id="user-1",
            preferred_provider="openai",
        ) == "openai"

    def test_resolve_default_provider_preserves_explicit_preferred_anthropic(self):
        from brain.platform.providers.model_policy import resolve_default_provider

        assert resolve_default_provider(preferred_provider="anthropic") == "anthropic"

    def test_resolve_default_provider_coerces_env_anthropic_to_openai(self):
        from brain.platform.providers.model_policy import resolve_default_provider

        with patch("brain.platform.providers.model_policy.get_active_provider", return_value="anthropic"):
            assert resolve_default_provider() == "openai"

    @pytest.mark.asyncio
    async def test_get_provider_model_maps_returns_all_providers(self):
        from brain.platform.providers.model_policy import async_get_provider_model_maps

        def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            params = params or {}
            if "SELECT org_id FROM users" in sql:
                return _AsyncMappingResult(first={"org_id": "org-1"})
            if "FROM org_provider_model_mappings" in sql:
                return _AsyncMappingResult(all=(
                    [{"intelligence_level": "high", "model_name": "gpt-5.5-pro"}]
                    if params.get("provider") == "openai"
                    else []
                ))
            raise AssertionError(f"Unexpected SQL: {sql}")

        mappings = await async_get_provider_model_maps(
            _AsyncPolicySession(execute_side_effect),
            user_id="user-1",
        )

        assert mappings["openai"]["high"] == "gpt-5.5-pro"
        assert mappings["anthropic"]["high"] == "claude-opus-4-6"

    @pytest.mark.asyncio
    async def test_org_provider_model_map_strips_legacy_provider_prefixes(self):
        from brain.platform.providers.model_policy import async_get_provider_model_map

        def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            if "SELECT org_id FROM users" in sql:
                return _AsyncMappingResult(first={"org_id": "org-1"})
            if "FROM org_provider_model_mappings" in sql:
                return _AsyncMappingResult(all=[
                    {"intelligence_level": "medium", "model_name": "openai:gpt-5.4"},
                    {"intelligence_level": "low", "model_name": "openai/gpt-5.4-mini"},
                ])
            raise AssertionError(f"Unexpected SQL: {sql}")

        model_map = await async_get_provider_model_map(
            _AsyncPolicySession(execute_side_effect),
            "openai",
            user_id="user-1",
        )

        assert model_map["medium"] == "gpt-5.4"
        assert model_map["low"] == "gpt-5.4-mini"

    def test_llm_request_normalized_model_strips_colon_prefix(self):
        from brain.platform.integrations.providers import LLMRequest

        request = LLMRequest(model="openai:gpt-5.4", messages=[])

        assert request.normalized_model == "gpt-5.4"

    def test_provider_degradation_policy_defines_operation_fallbacks(self):
        from brain.platform.provider_health import get_degradation_policy

        scout = get_degradation_policy("scout")
        verifier = get_degradation_policy("verifier")
        memory = get_degradation_policy("memory_extraction")

        assert scout.fail_open is True
        assert "full_pipeline" in scout.fallback_tiers
        assert verifier.fail_closed_for_high_risk is True
        assert "fail_closed_high_risk" in verifier.fallback_tiers
        assert "llm_retry_later" in memory.fallback_tiers
        assert all("regex" not in tier for tier in memory.fallback_tiers)
