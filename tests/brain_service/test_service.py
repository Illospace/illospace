"""Exercise the HTTP contract and the real worker without a model."""

import asyncio
import json
import sys
from contextlib import aclosing
from pathlib import Path

import httpx
import pytest

from brain.models import event
from brain.process import run_worker
from brain.server import create_app


async def post(app, request, secret="service-test-secret", **overrides):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://brain"
    ) as http:
        return await http.post(
            "/run", json=request.wire() | overrides, headers={"X-Illo-Brain-Secret": secret}
        )


def lines(response):
    return [json.loads(line) for line in response.text.splitlines()]


@pytest.fixture(autouse=True)
def service_secret(monkeypatch):
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "service-test-secret")


async def test_echo_worker_stream_and_cleanup(run_request, fake_mcp, tmp_path, monkeypatch):
    monkeypatch.setenv("ILLO_BRAIN_TMPDIR", str(tmp_path))
    run_request.mcp_url = fake_mcp.url
    response = await post(create_app(), run_request)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = lines(response)
    assert events[0]["type"] == "started"
    assert events[-1]["type"] == "final"
    assert all(item["run_id"] == run_request.run_id for item in events)
    assert json.loads(events[-1]["text"]) == ["lanes_search", "asks_list", "slack_post_status"]
    assert events[-1]["usage"] == {}
    assert list(tmp_path.iterdir()) == []
    assert set(fake_mcp.tokens) == {"Bearer fake-run-token"}
    assert "fake-run-token" not in response.text


async def test_secret_and_safe_validation(run_request, monkeypatch):
    app = create_app()
    assert (await post(app, run_request, secret="wrong")).status_code == 401
    monkeypatch.delenv("ILLO_BRAIN_SECRET")
    assert (await post(app, run_request)).status_code == 503
    response = await post(
        app, run_request, mcp_url="not a URL", mcp_token={"sensitive": "fake-run-token"}
    )
    assert response.status_code == 422
    assert "fake-run-token" not in response.text


async def test_health_does_not_expose_auth(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-provider-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://brain"
    ) as http:
        response = await http.get("/healthz")
    assert response.json()["runtimes"]["claude"]["usable"]
    assert response.json()["runtimes"]["openai_compat"]["experimental"]
    assert "fake-provider-key" not in response.text
    assert "fake-openai-key" not in response.text


async def test_timeout_and_cancellation_close_runner(run_request):
    closed = asyncio.Event()

    async def slow(request):
        try:
            await asyncio.sleep(10)
            yield event("final", text="late", usage={})
        finally:
            closed.set()

    run_request.timeout_s = 0.05
    response = await post(create_app(runner=slow), run_request)
    assert lines(response)[-1]["category"] == "timeout"
    assert closed.is_set()


async def test_concurrency_waits(run_request):
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0
    maximum = 0

    async def blocking(request):
        nonlocal count, maximum
        count += 1
        maximum = max(maximum, count)
        entered.set()
        try:
            await release.wait()
            yield event("final", text="done", usage={})
        finally:
            count -= 1

    app = create_app(concurrency=1, runner=blocking)
    first = asyncio.create_task(post(app, run_request))
    await entered.wait()
    second = asyncio.create_task(post(app, run_request))
    await asyncio.sleep(0.05)
    assert count == 1
    release.set()
    responses = await asyncio.gather(first, second)
    assert maximum == 1
    assert all(lines(response)[-1]["type"] == "final" for response in responses)


async def test_redact_output_and_do_not_emit_exception(run_request):
    async def echo_secret(request):
        yield event("text", text=request.mcp_token.get_secret_value())
        raise RuntimeError("private provider details")

    response = await post(create_app(runner=echo_secret), run_request)
    assert lines(response)[1]["text"] == "[redacted]"
    assert lines(response)[-1]["category"] == "runtime"
    assert "private provider details" not in response.text


def stat(pid):
    try:
        # The fields after the command name: state, ppid, pgrp, ...
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(pid):
    fields = stat(pid)
    return fields is not None and fields[0] not in ("Z", "X")


async def dead_within(pid, seconds):
    # SIGKILL is asynchronous. run_worker reaps only the group leader, and the
    # kernel can run a dying grandchild later on a loaded host (issue #958).
    deadline = asyncio.get_running_loop().time() + seconds
    while alive(pid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    return not alive(pid)


@pytest.mark.parametrize("cancel", [False, True])
async def test_process_tree_killed_and_directory_removed(
    run_request, tmp_path, monkeypatch, cancel
):
    monkeypatch.setenv("ILLO_BRAIN_TMPDIR", str(tmp_path))
    code = (
        "import subprocess,time,json,os; "
        "open('scratch','w').write('x'); "
        # The child must not hold stdout: asyncio's wait() also waits for that pipe.
        "p=subprocess.Popen(['sleep','30'],stdout=subprocess.DEVNULL); "
        "print(json.dumps({'type':'started','parent':os.getpid(),'child':p.pid}),flush=True); "
        "time.sleep(30)"
    )
    pids = {}
    ready = asyncio.Event()

    async def consume():
        async with aclosing(run_worker(run_request, [sys.executable, "-c", code])) as stream:
            async for item in stream:
                pids.update(item)
                ready.set()
                await asyncio.sleep(30)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(ready.wait(), 5)
    assert alive(pids["parent"]) and alive(pids["child"])
    # The child is in the run's process group, and the run folder holds a file.
    assert int(stat(pids["child"])[2]) == pids["parent"]
    [folder] = tmp_path.iterdir()
    assert (folder / "scratch").exists()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await task
    assert not alive(pids["parent"])
    # Without the kill, the child sleeps 30 seconds, so a missed kill still fails.
    assert await dead_within(pids["child"], 10)
    assert list(tmp_path.iterdir()) == []


async def test_missing_claude_auth_returns_auth_error(run_request, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ILLO_BRAIN_USE_HOST_PROVIDER", raising=False)
    run_request.runtime = "claude"
    response = await post(create_app(), run_request)
    assert lines(response)[-1]["category"] == "auth"
