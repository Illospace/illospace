from brain.platform.providers.model_policy import (
    DEFAULT_PROVIDER_MODEL_MAPS,
    MODEL_TIERS,
    get_model_for_tier,
    infer_provider_from_model,
    is_valid_model_tier,
    model_tier_from_name,
    normalize_model_tier,
    normalize_runtime_provider,
)


def test_model_tier_aliases_normalize_to_supported_tiers():
    aliases = {
        "large": "high",
        "balanced": "medium",
        "small": "low",
        " HIGH ": "high",
        "": "medium",
        None: "medium",
    }

    for raw, expected in aliases.items():
        assert normalize_model_tier(raw) == expected

    for tier in MODEL_TIERS:
        assert is_valid_model_tier(tier)
    assert not is_valid_model_tier("giant")


def test_provider_and_model_inference_are_inverse_for_default_maps():
    for provider, model_map in DEFAULT_PROVIDER_MODEL_MAPS.items():
        assert normalize_runtime_provider(provider.upper()) == provider
        for tier, model in model_map.items():
            assert infer_provider_from_model(model, default="openai") == provider
            assert model_tier_from_name(model, provider=provider) == tier
            assert get_model_for_tier(tier, provider=provider) == model
            assert get_model_for_tier(tier, provider=provider, include_provider_prefix=True) == f"{provider}/{model}"

    assert normalize_runtime_provider("unknown") == "openai"
