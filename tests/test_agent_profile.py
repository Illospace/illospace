from __future__ import annotations


def test_agent_profile_is_product_owned_runtime_layer():
    from brain.systems.personality import DEFAULT_AGENT_PROFILE_MD, agent_profile_prompt_section

    prompt = agent_profile_prompt_section()

    assert prompt.startswith("## Agent Profile")
    assert "SOUL defines identity and taste" in DEFAULT_AGENT_PROFILE_MD
    assert "## Fast Runtime" not in prompt
    assert "single-agent interactive path" not in prompt
    assert "Final Reply Presenter" in prompt
    assert "When the user only confirms, corrects, asks yes/no" in prompt
    assert "usually under 160 characters" in prompt
    assert "config snippets, caveats, or next steps" in prompt
