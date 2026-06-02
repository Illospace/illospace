from __future__ import annotations

from typing import Any

import pytest

from brain.systems.slack import client as slack_client_module
from brain.systems.slack.client import SlackApiError, SlackWebClient


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


def _patch_http_client(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> list[tuple[str, str, dict[str, str], dict[str, Any]]]:
    calls: list[tuple[str, str, dict[str, str], dict[str, Any]]] = []

    class _HttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str], params: dict[str, Any]):
            calls.append(("get", url, headers, params))
            return _Response(payload)

    monkeypatch.setattr(slack_client_module, "async_http_client", lambda **_kwargs: _HttpClient())
    return calls


@pytest.mark.asyncio
async def test_slack_client_reads_thread_context_with_query_params(monkeypatch):
    calls = _patch_http_client(
        monkeypatch,
        {"ok": True, "messages": [{"ts": "1716900000.000100", "text": "Root"}]},
    )

    result = await SlackWebClient("xoxb-test").conversation_replies(
        channel="C456",
        thread_ts="1716900000.000100",
        limit=500,
    )

    assert result["ok"] is True
    assert calls == [
        (
            "get",
            "https://slack.com/api/conversations.replies",
            {"Authorization": "Bearer xoxb-test"},
            {"channel": "C456", "ts": "1716900000.000100", "limit": 200},
        )
    ]


@pytest.mark.asyncio
async def test_slack_client_surfaces_response_metadata_errors(monkeypatch):
    _patch_http_client(
        monkeypatch,
        {
            "ok": False,
            "error": "invalid_arguments",
            "response_metadata": {
                "messages": [
                    "[ERROR] missing required field: channel",
                    "[ERROR] missing required field: ts",
                ]
            },
        },
    )

    with pytest.raises(SlackApiError, match="missing required field: channel") as caught:
        await SlackWebClient("xoxb-test").conversation_replies(
            channel="C456",
            thread_ts="1716900000.000100",
            limit=10,
        )

    assert caught.value.error == "invalid_arguments"
    assert caught.value.response_metadata == {
        "messages": [
            "[ERROR] missing required field: channel",
            "[ERROR] missing required field: ts",
        ]
    }
