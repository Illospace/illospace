"""Tests that setup-tokens go through the native SDK (no CLI/gateway detour)."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_llm_mock(is_oauth: bool, client: MagicMock):
    """Build a mock LLMClient."""
    llm = MagicMock()
    llm.client = client
    llm.provider = "anthropic"
    llm.source = "user_default"
    llm.is_oauth = is_oauth
    llm.extra_headers = {"anthropic-beta": "oauth-2025-04-20"} if is_oauth else {}
    llm.token_prefix = "sk-ant-oat01-real" if is_oauth else "sk-ant-api03-test"
    llm.system_prompt_prefix = "You are Claude Code, Anthropic's official CLI for Claude." if is_oauth else ""
    llm.get_extra_headers.return_value = dict(llm.extra_headers)
    return llm


def test_setup_token_uses_native_sdk_not_cli_backend():
    """Setup tokens (OAuth) now go through the native agent loop, not CLI/gateway."""
    import brain.systems.runs.direct_agent as agent

    fake_response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=11, output_tokens=7,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
        stop_reason="end_turn",
        content=[{"type": "text", "text": "native ok"}],
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response

    llm = _make_llm_mock(is_oauth=True, client=fake_client)

    with patch("brain.systems.runs.direct_agent.resolve_llm_client", return_value=llm), \
         patch("brain.systems.runs.direct_agent._harvest_session"):
        result = agent.run_agent(
            message="hello",
            system_prompt="system",
            tools=[],
            persist_session=False,
            max_turns=1,
            user_id="user-1",
        )

    assert result.success is True
    assert result.output == "native ok"
    fake_client.messages.create.assert_called_once()


def test_api_key_uses_native_sdk():
    """Regular API keys also use the native agent loop."""
    import brain.systems.runs.direct_agent as agent

    fake_response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=11, output_tokens=7,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
        stop_reason="end_turn",
        content=[{"type": "text", "text": "api ok"}],
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response

    llm = _make_llm_mock(is_oauth=False, client=fake_client)

    with patch("brain.systems.runs.direct_agent.resolve_llm_client", return_value=llm), \
         patch("brain.systems.runs.direct_agent._harvest_session"):
        result = agent.run_agent(
            message="hello",
            system_prompt="system",
            tools=[],
            persist_session=False,
            max_turns=1,
            user_id="user-1",
        )

    assert result.success is True
    assert result.output == "api ok"
    fake_client.messages.create.assert_called_once()
