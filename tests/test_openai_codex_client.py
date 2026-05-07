from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_codex_client_posts_to_native_responses_endpoint():
    from brain.platform.integrations.openai_codex_client import OpenAICodexClient

    fake_http = MagicMock()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"output": []}
    fake_http.post.return_value = fake_response

    with patch("brain.platform.integrations.openai_codex_client.httpx.Client", return_value=fake_http):
        client = OpenAICodexClient(
            "access-123",
            "acct_123",
            base_url="https://chatgpt.com/backend-api/codex",
            originator="illo-test",
        )
        result = client.responses.create(
            model="gpt-5.4",
            input=[{"role": "user", "content": "hi"}],
            max_output_tokens=512,
            store=False,
            stream=False,
            extra_headers={"session_id": "sess_123"},
        )

    assert result == {"output": []}
    assert fake_http.post.call_args.args[0] == "/responses"
    assert fake_http.post.call_args.kwargs["headers"]["session_id"] == "sess_123"
    assert fake_http.post.call_args.kwargs["json"]["include"] == ["reasoning.encrypted_content"]
    assert "max_output_tokens" not in fake_http.post.call_args.kwargs["json"]
    assert fake_http.post.call_args.kwargs["json"]["store"] is False


def test_codex_client_stream_parses_sse_events():
    from brain.platform.integrations.openai_codex_client import OpenAICodexClient

    fake_http = MagicMock()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.iter_lines.return_value = iter(
        [
            'data: {"type":"response.output_text.delta","delta":"Hi"}',
            "",
            'data: {"type":"response.completed","response":{"output":[]}}',
            "",
            "data: [DONE]",
            "",
        ]
    )
    fake_cm = MagicMock()
    fake_cm.__enter__.return_value = fake_response
    fake_cm.__exit__.return_value = False
    fake_http.stream.return_value = fake_cm

    with patch("brain.platform.integrations.openai_codex_client.httpx.Client", return_value=fake_http):
        client = OpenAICodexClient(
            "access-123",
            "acct_123",
            base_url="https://chatgpt.com/backend-api/codex",
        )
        stream = client.responses.create(
            model="gpt-5.4",
            input=[{"role": "user", "content": "hi"}],
            max_output_tokens=256,
            store=False,
            stream=True,
        )
        events = list(stream)

    assert events[0]["type"] == "response.output_text.delta"
    assert events[0]["delta"] == "Hi"
    assert events[1]["type"] == "response.completed"
    assert fake_http.stream.call_args.kwargs["json"]["stream"] is True
    assert "max_output_tokens" not in fake_http.stream.call_args.kwargs["json"]
    assert fake_http.stream.call_args.kwargs["json"]["store"] is False


def test_codex_client_normalizes_oversized_prompt_cache_key():
    from brain.platform.integrations.openai_codex_client import OpenAICodexClient

    fake_http = MagicMock()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"output": []}
    fake_http.post.return_value = fake_response

    with patch("brain.platform.integrations.openai_codex_client.httpx.Client", return_value=fake_http):
        client = OpenAICodexClient(
            "access-123",
            "acct_123",
            base_url="https://chatgpt.com/backend-api/codex",
        )
        client.responses.create(
            model="gpt-5.5",
            input=[{"role": "user", "content": "hi"}],
            prompt_cache_key="illo:" + ("x" * 80),
        )

    cache_key = fake_http.post.call_args.kwargs["json"]["prompt_cache_key"]
    assert fake_http.post.call_args.kwargs["json"]["model"] == "gpt-5.5"
    assert len(cache_key) <= 64
    assert cache_key.startswith("illo:")


def test_codex_client_normalizes_oversized_session_id_header():
    from brain.platform.integrations.openai_codex_client import OpenAICodexClient

    fake_http = MagicMock()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"output": []}
    fake_http.post.return_value = fake_response

    with patch("brain.platform.integrations.openai_codex_client.httpx.Client", return_value=fake_http):
        client = OpenAICodexClient(
            "access-123",
            "acct_123",
            base_url="https://chatgpt.com/backend-api/codex",
        )
        client.responses.create(
            model="gpt-5.4",
            input=[{"role": "user", "content": "hi"}],
            extra_headers={
                "session_id": "coordinator-idea-12345678-1234-5678-90ab-cdef12345678:final-reply-checker",
            },
        )

    session_id = fake_http.post.call_args.kwargs["headers"]["session_id"]
    assert len(session_id) <= 64
    assert session_id.startswith("coordinator-idea-")


def test_codex_client_accepts_output_text_done_event_without_warning():
    from brain.platform.integrations.openai_codex_client import OpenAICodexClient

    fake_http = MagicMock()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.iter_lines.return_value = iter(
        [
            'data: {"type":"response.output_text.done","text":"Hi"}',
            "",
            'data: {"type":"response.completed","response":{"output":[]}}',
            "",
        ]
    )
    fake_cm = MagicMock()
    fake_cm.__enter__.return_value = fake_response
    fake_cm.__exit__.return_value = False
    fake_http.stream.return_value = fake_cm

    with patch("brain.platform.integrations.openai_codex_client.httpx.Client", return_value=fake_http):
        with patch("brain.platform.integrations.openai_codex_client.logger.warning") as mock_warning:
            client = OpenAICodexClient(
                "access-123",
                "acct_123",
                base_url="https://chatgpt.com/backend-api/codex",
            )
            stream = client.responses.create(
                model="gpt-5.4",
                input=[{"role": "user", "content": "hi"}],
                stream=True,
            )
            events = list(stream)

    assert events[0]["type"] == "response.output_text.done"
    mock_warning.assert_not_called()


def test_codex_client_accepts_reasoning_summary_part_events_without_warning():
    from brain.platform.integrations.openai_codex_client import OpenAICodexClient

    fake_http = MagicMock()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.iter_lines.return_value = iter(
        [
            'data: {"type":"response.reasoning_summary_part.added","part":{"type":"summary_text","text":""}}',
            "",
            'data: {"type":"response.reasoning_summary_part.done","part":{"type":"summary_text","text":"Checking state."}}',
            "",
            'data: {"type":"response.completed","response":{"output":[]}}',
            "",
        ]
    )
    fake_cm = MagicMock()
    fake_cm.__enter__.return_value = fake_response
    fake_cm.__exit__.return_value = False
    fake_http.stream.return_value = fake_cm

    with patch("brain.platform.integrations.openai_codex_client.httpx.Client", return_value=fake_http):
        with patch("brain.platform.integrations.openai_codex_client.logger.warning") as mock_warning:
            client = OpenAICodexClient(
                "access-123",
                "acct_123",
                base_url="https://chatgpt.com/backend-api/codex",
            )
            stream = client.responses.create(
                model="gpt-5.4",
                input=[{"role": "user", "content": "hi"}],
                stream=True,
            )
            events = list(stream)

    assert [event["type"] for event in events] == [
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.completed",
    ]
    mock_warning.assert_not_called()
