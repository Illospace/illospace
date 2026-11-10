import json

import httpx
import pytest

from collectors.common import CollectorError, MCPClient, clean, read_token


def test_mcp_sse_session_and_protocol():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((body, dict(request.headers)))
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": "2025-06-18"},
                },
                headers={"mcp-session-id": "acme-session"},
            )
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        reply = {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {"structuredContent": {"answers": []}},
        }
        return httpx.Response(
            200,
            text="event: message\r\ndata: " + json.dumps(reply) + "\r\n\r\n",
            headers={"content-type": "text/event-stream"},
        )

    client = MCPClient(
        "https://illo.example/mcp",
        "acme-token",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.call("ask_pull", {}) == {"answers": []}
    assert calls[-1][1]["mcp-session-id"] == "acme-session"
    assert calls[-1][1]["mcp-protocol-version"] == "2025-06-18"
    assert calls[-1][1]["authorization"] == "Bearer acme-token"


@pytest.mark.parametrize(
    "value",
    [
        {"error": {"message": "private response"}},
        {"result": {"isError": True, "content": [{"type": "text", "text": "secret"}]}},
        {"result": {"content": []}},
    ],
)
def test_mcp_errors_do_not_expose_responses(value):
    def handler(request):
        return httpx.Response(
            200, json={"id": json.loads(request.content)["id"], **value}
        )

    client = MCPClient(
        "https://illo.example/mcp",
        "acme-token",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.ready = True
    with pytest.raises(CollectorError) as caught:
        client.call("ask_pull", {})
    assert "secret" not in str(caught.value)
    assert "private response" not in str(caught.value)


def test_token_file_permissions(tmp_path):
    path = tmp_path / "token"
    path.write_text("acme-token\n")
    path.chmod(0o644)
    with pytest.raises(CollectorError):
        read_token(path)
    path.chmod(0o600)
    assert read_token(path) == "acme-token"


def test_scrub_credentials_code_and_url_query():
    value = clean(
        "Bearer acme-secret password=example ```file contents``` "
        "https://sam:password@example.com/task?token=example#secret "
        "sk-abcdefghijklmnop",
        1000,
    )
    assert "acme-secret" not in value
    assert "file contents" not in value
    assert "password" not in value
    assert "https://example.com/task" in value
    assert "sk-abcdefghijklmnop" not in value
