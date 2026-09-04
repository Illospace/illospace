import pytest

from brain.platform.providers.model_policy import required_openai_auth_mode


@pytest.mark.parametrize(
    "model",
    [
        "gpt-6-astra",
        "openai/gpt-6-astra",
        "openai:gpt-6-astra",
        "GPT-6-ASTRA",
        "gpt-5.5",
        "openai/gpt-5.5",
        "openai:gpt-5.5",
        "GPT-5.5",
        "gpt-5.6-sol",
        "openai/gpt-5.6-sol",
        "gpt-5.6-luna",
        "openai/gpt-5.6-luna",
        "openai/gpt-5.6",
    ],
)
def test_subscription_only_models_require_chatgpt_auth(model):
    assert required_openai_auth_mode(model) == "chatgpt"


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4.1",
        "openai/gpt-4.1-mini",
        "anthropic/claude-sonnet-4-6",
        "ollama/qwen3.6-27b",
        "",
        None,
    ],
)
def test_other_models_do_not_pin_an_auth_mode(model):
    assert required_openai_auth_mode(model) is None


def test_cycle_preflight_and_run_agree_on_required_auth_mode():
    """The shared probe must validate the credential the run will actually use.

    A divergent preflight copy recognized only gpt-5.5, so a
    GPT-5.6 cycle validated an interchangeable credential and lost the
    actionable auth-blocked message when the ChatGPT credential was dead.
    """
    from brain.platform.integrations import provider_auth_preflight
    from brain.systems.runs import direct_agent
    from brain.systems.runs.direct_loop import final_reply_checker

    for module in (provider_auth_preflight, direct_agent, final_reply_checker):
        assert not hasattr(module, "_required_openai_auth_mode"), (
            f"{module.__name__} redeclares the auth-mode rule; import the shared helper"
        )

    assert (
        provider_auth_preflight.required_openai_auth_mode
        is required_openai_auth_mode
    )
    assert direct_agent.required_openai_auth_mode is required_openai_auth_mode
    assert final_reply_checker.required_openai_auth_mode is required_openai_auth_mode
