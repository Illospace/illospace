import hashlib
import json
import os
import shutil

import httpx
import pytest

from collectors.common import (
    CODE_VERSION,
    VERSION,
    CollectorError,
    MCPClient,
    clean,
    code_fingerprint,
    read_token,
)


def test_code_fingerprint_depends_on_python_paths_and_contents(tmp_path):
    root = tmp_path / "collectors"
    hooks = root / "hooks"
    hooks.mkdir(parents=True)
    (root / "main.py").write_bytes(b"main = True\n")
    (hooks / "report.py").write_bytes(b"report = True\n")
    expected = hashlib.sha256(b"hooks/report.py\0report = True\n\0main.py\0main = True\n\0")
    fingerprint = expected.hexdigest()[:12]
    assert code_fingerprint(root) == fingerprint

    cache = hooks / "__pycache__" / "nested"
    cache.mkdir(parents=True)
    (cache / "ignored.py").write_bytes(b"ignored = True\n")
    (root / "requirements.txt").write_text("httpx\n")
    copy = shutil.copytree(root, tmp_path / "install" / "collectors")
    os.utime(copy / "main.py", (1, 1))
    assert code_fingerprint(root) == code_fingerprint(copy) == fingerprint

    (copy / "main.py").write_bytes(b"main = None\n")
    changed = code_fingerprint(copy)
    assert changed != fingerprint
    (copy / "extra.py").touch()
    assert code_fingerprint(copy) != changed


def test_code_version_includes_the_fingerprint():
    assert CODE_VERSION == f"{VERSION}+{code_fingerprint()}"


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
    assert calls[0][0]["params"]["clientInfo"]["version"] == CODE_VERSION
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
        return httpx.Response(200, json={"id": json.loads(request.content)["id"], **value})

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
