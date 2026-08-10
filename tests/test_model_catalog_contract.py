from pathlib import Path


def test_catalog_is_the_complete_owner_for_derived_provider_tables():
    from brain.platform.model_catalog import MODEL_CATALOG
    from brain.platform.providers.model_policy import (
        DEFAULT_PROVIDER_MODELS,
        MODEL_PRICING_PER_MILLION,
        PROVIDER_MODEL_OPTIONS,
    )
    from brain.systems.context.budget import resolve_model_context_budget
    from brain.systems.runs.direct_loop.model_fallback import fallback_model_for

    ids = [entry.id for entry in MODEL_CATALOG]
    assert len(ids) == len(set(ids))
    assert set(MODEL_PRICING_PER_MILLION) == set(ids)
    assert DEFAULT_PROVIDER_MODELS == {
        entry.provider: entry.model_name
        for entry in MODEL_CATALOG
        if entry.provider_default
    }
    assert PROVIDER_MODEL_OPTIONS == {
        provider: tuple(
            entry.model_name
            for entry in MODEL_CATALOG
            if entry.provider == provider
        )
        for provider in DEFAULT_PROVIDER_MODELS
    }
    for entry in MODEL_CATALOG:
        assert fallback_model_for(entry.id) == entry.availability_fallback
        assert (
            resolve_model_context_budget(
                model=entry.id,
                provider=entry.provider,
            ).context_window_tokens
            == entry.context_window_tokens
        )


def test_composer_consumes_runtime_catalog_instead_of_owning_model_options():
    source = Path(
        "frontend/src/lib/features/composer/domain/runSettings.ts"
    ).read_text()
    runtime_types = Path("frontend/src/lib/types/runtimeSettings.ts").read_text()

    assert "MODEL_OPTIONS" not in source
    assert "modelCatalog" in source
    assert "RuntimeModelCatalogEntry" in source
    assert "'openai' | 'anthropic' | 'ollama'" in runtime_types


def test_gpt_5_6_sol_uses_provider_context_contract(monkeypatch):
    from brain.platform.model_catalog import get_model_catalog_entry
    from brain.systems.context.budget import resolve_model_context_budget

    for name in (
        "AGENT_MODEL_CONTEXT_WINDOW_TOKENS",
        "AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS",
        "AGENT_CONTEXT_RESERVED_REASONING_TOKENS",
        "AGENT_CONTEXT_RESERVED_TOOL_TOKENS",
        "AGENT_CONTEXT_SAFETY_MARGIN_TOKENS",
        "AGENT_AUTO_COMPACT_TOKEN_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)

    entry = get_model_catalog_entry("openai/gpt-5.6-sol")
    assert entry is not None
    assert entry.context_window_tokens == 1_050_000

    budget = resolve_model_context_budget(
        model=entry.id,
        reasoning_effort="xhigh",
        max_output_tokens=32_768,
        tools=[{"name": f"tool-{index}"} for index in range(87)],
    )
    assert budget.context_window_tokens == 1_050_000
    assert budget.auto_compact_threshold_tokens == 863_690


def test_gpt_5_6_luna_uses_subscription_catalog_contract():
    from brain.platform.model_catalog import get_model_catalog_entry
    from brain.platform.providers.model_policy import required_openai_auth_mode

    entry = get_model_catalog_entry("openai/gpt-5.6-luna")

    assert entry is not None
    assert entry.label == "GPT-5.6 Luna"
    assert entry.availability_fallback == "openai/gpt-5.6-sol"
    assert entry.context_window_tokens == 1_050_000
    assert entry.input_price_per_million == 0.20
    assert entry.output_price_per_million == 1.20
    assert required_openai_auth_mode(entry.id) == "chatgpt"


def test_ollama_qwen_uses_free_local_catalog_contract():
    from brain.platform.model_catalog import get_model_catalog_entry
    from brain.platform.providers.model_policy import get_model_catalog_contract
    from brain.systems.runtime_settings.schemas import RuntimeModelCatalogEntry

    entry = get_model_catalog_entry("ollama/qwen3.6-27b")
    contract = {
        item["id"]: item
        for item in get_model_catalog_contract()
    }["ollama/qwen3.6-27b"]

    assert entry is not None
    assert entry.input_price_per_million == 0.0
    assert entry.output_price_per_million == 0.0
    assert entry.availability_fallback == "openai/gpt-5.6-luna"
    assert entry.context_window_tokens == 32_768
    assert entry.supported_effort_tiers == ("none",)
    assert contract["auth_requirement"] == "none"
    assert RuntimeModelCatalogEntry.model_validate(contract).provider == "ollama"


def test_runtime_schema_validates_every_catalog_contract_entry():
    from brain.platform.providers.model_policy import get_model_catalog_contract
    from brain.systems.runtime_settings.schemas import RuntimeModelCatalogEntry

    contract = get_model_catalog_contract()

    validated_entries = [
        RuntimeModelCatalogEntry.model_validate(item)
        for item in contract
    ]
    assert [entry.id for entry in validated_entries] == [
        item["id"] for item in contract
    ]
