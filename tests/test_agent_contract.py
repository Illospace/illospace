from __future__ import annotations


def test_agent_contract_is_product_owned_runtime_layer():
    from brain.systems.personality import (
        DEFAULT_AGENT_CONTRACT_MD,
        agent_contract_prompt_section,
    )

    prompt = agent_contract_prompt_section()

    assert prompt.startswith("## Agent Contract")
    assert "SOUL defines identity, voice, and taste" in DEFAULT_AGENT_CONTRACT_MD
    assert "## Fast Runtime" not in prompt
    assert "single-agent interactive path" not in prompt
    assert "Reply Integrity" in prompt
    assert "Never claim a test, command, external check" in prompt
    assert "Ground Illospace-specific screens" in prompt
