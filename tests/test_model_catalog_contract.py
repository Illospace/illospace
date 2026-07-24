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

    assert "MODEL_OPTIONS" not in source
    assert "modelCatalog" in source
    assert "RuntimeModelCatalogEntry" in source
