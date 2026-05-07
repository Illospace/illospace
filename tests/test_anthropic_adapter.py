from unittest.mock import MagicMock, patch


def test_build_auth_adapter_uses_bearer_auth_for_setup_tokens():
    from brain.platform.integrations.anthropic_adapter import build_auth_adapter

    with patch("brain.platform.integrations.anthropic_adapter.get_oauth_betas", return_value=["oauth-2025-04-20"]), \
         patch("anthropic.Anthropic") as mock_sdk:
        adapter = build_auth_adapter("sk-ant-oat01-real-looking", timeout=30)

    kwargs = mock_sdk.call_args.kwargs
    assert kwargs["api_key"] is None
    assert kwargs["auth_token"] == "sk-ant-oat01-real-looking"
    assert kwargs["default_headers"]["x-app"] == "cli"
    assert adapter.is_oauth is True
    assert adapter.extra_headers == {"anthropic-beta": "oauth-2025-04-20"}


def test_create_message_with_token_merges_oauth_headers():
    from brain.platform.integrations.anthropic_adapter import AnthropicAuthAdapter, create_message_with_token

    fake_client = MagicMock()
    fake_client.messages.create.return_value = {"ok": True}
    adapter = AnthropicAuthAdapter(
        client=fake_client,
        is_oauth=True,
        extra_headers={"anthropic-beta": "oauth-2025-04-20"},
        token_prefix="sk-ant-oat01-abc",
        token_suffix="suffix",
    )

    with patch("brain.platform.integrations.anthropic_adapter.build_auth_adapter", return_value=adapter):
        create_message_with_token(
            "sk-ant-oat01-real-looking",
            timeout=15,
            model="claude-sonnet-4-6",
            max_tokens=5,
            messages=[{"role": "user", "content": "hi"}],
            extra_headers={"x-test": "1"},
        )

    fake_client.messages.create.assert_called_once()
    kwargs = fake_client.messages.create.call_args.kwargs
    assert kwargs["extra_headers"] == {"x-test": "1", "anthropic-beta": "oauth-2025-04-20"}
