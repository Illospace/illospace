"""Keep sandbox errors safe and HTTP settings independent of the host."""

import json

import httpx
import pytest

from illo.sandbox.client import SandboxClient, SandboxError


async def test_client_contract_and_transport():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/cleanup":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"exit_code": 0, "stdout": "ok", "stderr": "",
            "truncated": False, "timed_out": False, "duration_ms": 2})

    client = SandboxClient("http://sandbox", transport=httpx.MockTransport(handler))
    assert await client.healthy()
    result = await client.run(run_id=15, command="pwd", timeout_s=7, cwd="/repos")
    assert result.stdout == "ok"
    assert json.loads(calls[-1].content) == {"run_id": "15", "command": "pwd", "timeout_s": 7, "cwd": "/repos"}
    assert calls[-1].extensions["timeout"]["read"] == 22
    await client.cleanup(15)
    assert json.loads(calls[-1].content) == {"run_id": "15"}


@pytest.mark.parametrize("status, text", [(429, "The sandbox is busy. Try again."),
    (500, "The sandbox refused the request."), (422, "The sandbox refused the request.")])
async def test_safe_error(status, text):
    client = SandboxClient("http://sandbox", transport=httpx.MockTransport(
        lambda _: httpx.Response(status, text="secret")))
    with pytest.raises(SandboxError, match=text):
        await client.run(run_id=1, command="true")
    assert not await client.healthy()


async def test_invalid_response_and_connection_error():
    client = SandboxClient("http://sandbox", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={})))
    with pytest.raises(SandboxError, match="invalid result"):
        await client.run(run_id=1, command="true")

    def fail(request):
        raise httpx.ConnectError("secret", request=request)

    client.transport = httpx.MockTransport(fail)
    with pytest.raises(SandboxError, match="The sandbox did not answer."):
        await client.cleanup(1)
