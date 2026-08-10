import pytest

from brain.platform.effort import (
    EFFORT_TIER_SET,
    EFFORT_TIERS,
    PROVIDER_EFFORT_RENDERINGS,
    render_reasoning_effort,
)


def test_every_provider_renders_the_full_canonical_ladder():
    """Adding a provider without a complete effort mapping must fail loudly."""
    for provider, renderings in PROVIDER_EFFORT_RENDERINGS.items():
        assert frozenset(renderings) == EFFORT_TIER_SET, provider


@pytest.mark.parametrize("tier", [t for t in EFFORT_TIERS if t != "none"])
def test_openai_rendering_is_native(tier):
    assert render_reasoning_effort("openai", tier) == tier


def test_anthropic_ceiling_renders_to_max():
    assert render_reasoning_effort("anthropic", "xhigh") == "max"
    assert render_reasoning_effort("anthropic", "high") == "high"


def test_ollama_omits_native_effort_for_every_tier():
    assert set(PROVIDER_EFFORT_RENDERINGS["ollama"]) == EFFORT_TIER_SET
    assert all(
        value is None
        for value in PROVIDER_EFFORT_RENDERINGS["ollama"].values()
    )


@pytest.mark.parametrize("provider", ["openai", "anthropic", "unknown-provider"])
@pytest.mark.parametrize("tier", ["none", "", None, "  NONE  "])
def test_none_and_empty_always_omit_reasoning(provider, tier):
    assert render_reasoning_effort(provider, tier) is None


def test_native_and_unknown_values_pass_through():
    # A caller deliberately speaking provider vocabulary keeps working.
    assert render_reasoning_effort("anthropic", "max") == "max"
    # Unknown providers get the canonical value untranslated.
    assert render_reasoning_effort("some-new-provider", "xhigh") == "xhigh"


def test_model_policy_reexports_the_leaf_vocabulary():
    from brain.platform import effort
    from brain.platform.providers import model_policy

    assert model_policy.EFFORT_TIERS is effort.EFFORT_TIERS
    assert model_policy.EFFORT_TIER_SET is effort.EFFORT_TIER_SET
