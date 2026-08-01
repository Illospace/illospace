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
            assert get_default_model() == "openai/gpt-5.6-sol"

    def test_openai_model_options_use_native_defaults(self):
        from brain.platform.providers.model_policy import get_provider_model_options

        options = get_provider_model_options("openai")
        assert "gpt-5.6-sol" in options
        assert "gpt-5.6-luna" in options
        assert "gpt-5.5" in options
        assert set(options) == {"gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5"}

    @pytest.mark.asyncio
    async def test_org_default_thinking_overrides_runtime_default(self):
        from brain.platform.providers.model_policy import async_get_default_thinking

        def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            if "SELECT org_id FROM users" in sql:
                return _AsyncMappingResult(first={"org_id": "org-1"})
            if "SELECT memory_model_config FROM orgs" in sql:
                return _AsyncMappingResult(
                    first={"memory_model_config": {"default_thinking": "xhigh"}}
                )
            raise AssertionError(f"Unexpected SQL: {sql}")

        thinking = await async_get_default_thinking(
            _AsyncPolicySession(execute_side_effect),
            user_id="user-1",
        )

        assert thinking == "xhigh"

    @pytest.mark.asyncio
    async def test_org_default_model_overrides_default(self):
        from brain.platform.providers.model_policy import async_get_default_model

        def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            if "SELECT org_id FROM users" in sql:
                return _AsyncMappingResult(first={"org_id": "org-1"})
            if "SELECT memory_model_config FROM orgs" in sql:
                return _AsyncMappingResult(first={"memory_model_config": {"default_model": "openai/gpt-5.5"}})
            raise AssertionError(f"Unexpected SQL: {sql}")

        model = await async_get_default_model(
            _AsyncPolicySession(execute_side_effect),
            "openai",
            include_provider_prefix=False,
            user_id="user-1",
        )

        assert model == "gpt-5.5"

    def test_infer_provider_recognizes_new_openai_models(self):
        from brain.platform.providers.model_policy import infer_provider_from_model

        assert infer_provider_from_model("gpt-5.6-sol") == "openai"
        assert infer_provider_from_model("gpt-5.6-luna") == "openai"
        assert infer_provider_from_model("openai:gpt-5.6-luna") == "openai"
        assert infer_provider_from_model("gpt-5.5") == "openai"

    def test_infer_provider_recognizes_claude_family_names(self):
        from brain.platform.providers.model_policy import infer_provider_from_model

        assert infer_provider_from_model("claude-haiku-4-20250414", default="openai") == "anthropic"
        assert infer_provider_from_model("claude-sonnet-4-6") == "anthropic"

    def test_cost_normalizes_openai_models(self):
        from brain.platform.providers.model_policy import calculate_model_cost, normalize_model_name

        cost = calculate_model_cost("gpt-5.6-luna", 1_000_000, 1_000_000)
        assert abs(cost - 1.4) < 0.001
        assert normalize_model_name("gpt-5.6-luna") == "openai/gpt-5.6-luna"
        assert normalize_model_name("gpt-5.6") == "openai/gpt-5.6-sol"
        assert normalize_model_name("gpt-5.5") == "openai/gpt-5.5"

    def test_cost_uses_native_pricing_for_default_openai_models(self):
        from brain.platform.providers.model_policy import calculate_model_cost

        assert abs(calculate_model_cost("gpt-5.6-sol", 1_000_000, 1_000_000) - 35.0) < 0.001
        assert abs(calculate_model_cost("gpt-5.6-luna", 1_000_000, 1_000_000) - 1.4) < 0.001
        assert abs(calculate_model_cost("gpt-5.5", 1_000_000, 1_000_000) - 35.0) < 0.001

    def test_cost_applies_cached_input_discount_to_cached_subset(self):
        from brain.platform.providers.model_policy import calculate_model_cost

        cost = calculate_model_cost(
            "gpt-5.5",
            449_475,
            12_846,
            cache_read=191_488,
        )

        assert abs(cost - 1.771059) < 0.000001

    def test_unknown_model_still_defaults_to_default_pricing(self):
        from brain.platform.providers.model_policy import calculate_model_cost

        cost = calculate_model_cost("unknown-model", 1_000_000, 1_000_000)
        assert abs(cost - 35.0) < 0.001

    def test_infer_provider_from_prefixed_model(self):
        from brain.platform.providers.model_policy import infer_provider_from_model

        assert infer_provider_from_model("openai/gpt-4o-mini") == "openai"
        assert infer_provider_from_model("anthropic/claude-sonnet-4-6") == "anthropic"

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
    async def test_resolve_default_provider_preserves_org_anthropic(self):
        from brain.platform.providers.model_policy import async_resolve_default_provider

        results = [
            _AsyncMappingResult(first={"org_id": "org-1"}),
            _AsyncMappingResult(first={
                "memory_model_config": {"default_provider": "anthropic"},
            }),
        ]
        session = _AsyncPolicySession(lambda stmt, params=None: results.pop(0))
        assert await async_resolve_default_provider(session, user_id="user-1") == "anthropic"

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
    async def test_get_provider_model_catalogs_returns_all_providers(self):
        from brain.platform.providers.model_policy import async_get_provider_model_catalogs

        catalogs = await async_get_provider_model_catalogs(
            _AsyncPolicySession(lambda _stmt, _params=None: _AsyncMappingResult()),
            user_id="user-1",
        )

        assert catalogs["openai"]["default"] == "gpt-5.6-sol"
        assert "gpt-5.5" in catalogs["openai"]["options"]
        assert catalogs["anthropic"]["default"] == "claude-sonnet-5"

    def test_model_catalog_contract_is_provider_aware_and_pruned(self):
        from brain.platform.providers.model_policy import get_model_catalog_contract

        catalog = get_model_catalog_contract(
            workspace_default="openai/gpt-5.6-sol",
        )
        by_id = {entry["id"]: entry for entry in catalog}

        assert by_id["openai/gpt-5.6-sol"] == {
            "id": "openai/gpt-5.6-sol",
            "label": "GPT-5.6 Sol",
            "provider": "openai",
            "description": "Organization default; falls back to GPT-5.5 when unavailable.",
            "supported_effort_tiers": ["none", "low", "medium", "high", "xhigh"],
            "auth_requirement": "chatgpt",
            "availability_fallback": "openai/gpt-5.5",
            "default_provenance": {
                "provider_default": True,
                "workspace_default": True,
            },
        }
        assert by_id["anthropic/claude-sonnet-5"]["auth_requirement"] == "api_key"
        assert by_id["anthropic/claude-sonnet-5"]["default_provenance"] == {
            "provider_default": True,
            "workspace_default": False,
        }
        assert by_id["anthropic/claude-haiku-4-5"]["supported_effort_tiers"] == [
            "none"
        ]
        assert not {
            "openai/o3-mini",
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "openai/gpt-4.1",
            "openai/gpt-4.1-mini",
            "openai/gpt-5.2",
            "openai/gpt-5.3-codex",
            "openai/gpt-5.3-codex-spark",
        } & by_id.keys()

    @pytest.mark.asyncio
    async def test_org_default_model_strips_provider_prefixes(self):
        from brain.platform.providers.model_policy import async_get_default_model

        def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            if "SELECT org_id FROM users" in sql:
                return _AsyncMappingResult(first={"org_id": "org-1"})
            if "SELECT memory_model_config FROM orgs" in sql:
                return _AsyncMappingResult(first={"memory_model_config": {"default_model": "openai:gpt-5.6-luna"}})
            raise AssertionError(f"Unexpected SQL: {sql}")

        model = await async_get_default_model(
            _AsyncPolicySession(execute_side_effect),
            "openai",
            include_provider_prefix=False,
            user_id="user-1",
        )

        assert model == "gpt-5.6-luna"

    @pytest.mark.asyncio
    async def test_bulk_route_uses_valid_org_overrides(self):
        from brain.platform.providers.model_policy import async_get_bulk_route

        def execute_side_effect(stmt, params=None):
            sql = str(stmt)
            if "SELECT org_id FROM users" in sql:
                return _AsyncMappingResult(first={"org_id": "org-1"})
            if "SELECT memory_model_config FROM orgs" in sql:
                return _AsyncMappingResult(
                    first={
                        "memory_model_config": {
                            "bulk_model": "openai:gpt-5.6-sol",
                            "bulk_thinking": "high",
                        }
                    }
                )
            raise AssertionError(f"Unexpected SQL: {sql}")

        route = await async_get_bulk_route(
            _AsyncPolicySession(execute_side_effect),
            user_id="user-1",
        )

        assert route == {"model": "openai/gpt-5.6-sol", "thinking": "high"}

    @pytest.mark.asyncio
    async def test_bulk_route_rejects_invalid_org_overrides(self):
        from brain.platform.providers.model_policy import async_get_bulk_route

        session = _AsyncPolicySession(
            lambda _stmt, _params=None: _AsyncMappingResult(
                first={
                    "memory_model_config": {
                        "bulk_model": "not-a-model",
                        "bulk_thinking": "ultra",
                    }
                }
            )
        )

        route = await async_get_bulk_route(session, org_id="org-1")

        assert route == {"model": "openai/gpt-5.6-luna", "thinking": "xhigh"}

    def test_llm_request_normalized_model_strips_colon_prefix(self):
        from brain.platform.integrations.providers import LLMRequest

        request = LLMRequest(model="openai:gpt-5.6-luna", messages=[])

        assert request.normalized_model == "gpt-5.6-luna"

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
