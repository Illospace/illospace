from __future__ import annotations

from typing import Any

import pytest

from brain.systems.slack import client as slack_client_module
from brain.systems.slack.client import SlackApiError, SlackWebClient
from brain.systems.slack.uploads import slack_image_upload_from_data_url


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


def test_slack_image_upload_normalizes_data_url_payload():
    upload = slack_image_upload_from_data_url(
        "data:image/svg+xml;base64,PHN2Zy8+",
        filename="weekly active users",
        title="Weekly active users",
    )

    assert upload is not None
    assert upload.file_bytes == b"<svg/>"
    assert upload.content_type == "image/svg+xml"
    assert upload.filename == "weekly-active-users.svg"
    assert upload.title == "Weekly active users"
    assert upload.alt_txt == "Weekly active users"


def test_slack_image_upload_rejects_invalid_data_url_payload():
    with pytest.raises(ValueError, match="base64 data:image URL"):
        slack_image_upload_from_data_url("https://example.com/graph.png")


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
async def test_slack_client_pages_channel_history_through_existing_transport(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    client = SlackWebClient("xoxb-test")

    async def fake_post(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, payload))
        return {"ok": True, "messages": []}

    monkeypatch.setattr(client, "_post", fake_post)

    result = await client.conversation_history(
        channel="C456",
        limit=500,
        cursor="history-2",
    )

    assert result["ok"] is True
    assert calls == [
        (
            "conversations.history",
            {"channel": "C456", "limit": 200, "cursor": "history-2"},
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


@pytest.mark.asyncio
async def test_slack_client_uploads_file_with_external_upload_flow(monkeypatch):
    calls: list[tuple[str, str, dict[str, str], dict[str, Any] | bytes]] = []

    class _HttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def post(self, url: str, *, headers: dict[str, str], json=None, content=None):
            calls.append(("post", url, headers, json if json is not None else content))
            if url.endswith("/files.getUploadURLExternal"):
                return _Response({"ok": True, "upload_url": "https://files.slack.test/upload", "file_id": "F123"})
            if url == "https://files.slack.test/upload":
                return _Response({"ok": True})
            if url.endswith("/files.completeUploadExternal"):
                return _Response({"ok": True, "files": [{"id": "F123", "title": "Graph"}]})
            return _Response({"ok": False, "error": "unexpected_url"})

    monkeypatch.setattr(slack_client_module, "async_http_client", lambda **_kwargs: _HttpClient())

    result = await SlackWebClient("xoxb-test").upload_file(
        channel="C456",
        file_bytes=b"png-bytes",
        filename="graph.png",
        title="Graph",
        initial_comment="Here is the graph.",
        thread_ts="1716900000.000100",
        alt_txt="Line chart",
        content_type="image/png",
    )

    assert result == {"ok": True, "files": [{"id": "F123", "title": "Graph"}]}
    assert calls == [
        (
            "post",
            "https://slack.com/api/files.getUploadURLExternal",
            {"Authorization": "Bearer xoxb-test", "Content-Type": "application/json; charset=utf-8"},
            {"filename": "graph.png", "length": 9, "alt_txt": "Line chart"},
        ),
        (
            "post",
            "https://files.slack.test/upload",
            {"Content-Type": "image/png"},
            b"png-bytes",
        ),
        (
            "post",
            "https://slack.com/api/files.completeUploadExternal",
            {"Authorization": "Bearer xoxb-test", "Content-Type": "application/json; charset=utf-8"},
            {
                "files": [{"id": "F123", "title": "Graph"}],
                "channel_id": "C456",
                "initial_comment": "Here is the graph.",
                "thread_ts": "1716900000.000100",
            },
        ),
    ]


async def test_runtime_client_prefers_env_token(monkeypatch):
    from brain.systems.slack.client import slack_web_client_from_runtime

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-env-token")
    client = await slack_web_client_from_runtime(requested_by="t", reason="t")
    assert client.bot_token == "xoxb-env-token"


async def test_runtime_client_falls_back_to_runtime_secret(monkeypatch):
    """Deployments keep SLACK_BOT_TOKEN in DB-backed runtime secrets, not in
    every service env."""
    import brain.systems.slack.client as slack_client
    import brain.systems.slack.connector as connector
    import brain.systems.vault.runtime_secrets as runtime_secrets

    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ILLO_SLACK_ORG_ID", raising=False)
    monkeypatch.delenv("ILLO_SLACK_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("ILLO_ORG_ID", raising=False)
    monkeypatch.delenv("ILLO_OWNER_USER_ID", raising=False)

    async def fake_authority(*, org_id, owner_user_id):
        assert org_id is None and owner_user_id is None
        return "org-1", "user-1"

    calls = {}

    async def fake_read_runtime_secret(key_name, *, context, reason, requested_by, access, allow_env_fallback=False, env_names=None):
        calls["key"] = key_name
        calls["context"] = (context.actor_user_id, context.org_id)
        calls["access"] = access
        return "xoxb-vault-token"

    monkeypatch.setattr(connector, "resolve_slack_connector_authority", fake_authority)
    monkeypatch.setattr(runtime_secrets, "read_runtime_secret", fake_read_runtime_secret)

    client = await slack_client.slack_web_client_from_runtime(requested_by="t", reason="t")
    assert client.bot_token == "xoxb-vault-token"
    assert calls["key"] == "SLACK_BOT_TOKEN"
    assert calls["context"] == ("user-1", "org-1")
    assert calls["access"] == "service"
