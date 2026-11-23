"""Exercise the HTTP contract and the real worker without a model."""

import asyncio
import json
import sys
from contextlib import aclosing
from pathlib import Path

import httpx
import pytest

from brain import providers
from brain.models import event
from brain.process import run_worker
from brain.server import FAKE_TRANSCRIPT, create_app
from tests.brain_service.conftest import FAKE, openai


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


async def describe(app, secret="service-test-secret"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://brain"
    ) as http:
        return await http.get("/providers", headers={"X-Illo-Brain-Secret": secret})


async def test_providers_hold_no_secret(monkeypatch):
    monkeypatch.setenv("ILLO_PROVIDER_OPENAI_API_KEY", "fake-openai-secret")
    monkeypatch.setenv("ILLO_PROVIDER_ANTHROPIC_API_KEY", "fake-anthropic-secret")
    monkeypatch.setenv("ILLO_PROVIDER_LOCAL_API_KEY", "fake-local-secret")
    monkeypatch.setenv("ILLO_PROVIDER_LOCAL_WIRE", "chat")
    monkeypatch.setenv("ILLO_PROVIDER_LOCAL_BASE_URL", "https://fake-private-host.example/v1")
    monkeypatch.setenv("ILLO_PROVIDER_LOCAL_CAPABILITIES", "effort:none")
    app = create_app()
    assert (await describe(app, "wrong")).status_code == 401
    response = await describe(app)
    described = {item["name"]: item for item in response.json()["providers"]}
    assert set(described) == {"chatgpt", "fake", "openai", "anthropic", "local"}
    assert described["local"] == {"name": "local", "wire": "chat", "capabilities": {
        "images": False, "web_search": False, "efforts": ["none"], "transcription": False}}
    assert described["openai"]["capabilities"]["transcription"] is True
    assert described["chatgpt"]["wire"] == "responses"
    assert described["anthropic"]["capabilities"]["images"] is False
    for secret in ("fake-openai-secret", "fake-anthropic-secret", "fake-local-secret",
                   "fake-private-host", "chatgpt.com", "api.openai.com"):
        assert secret not in response.text
    assert (await http_get(app, "/healthz")).json() == {"status": "ok"}


async def http_get(app, path):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://brain"
    ) as http:
        return await http.get(path)


def test_a_provider_without_a_key_is_not_in_the_table():
    table = providers.load({"ILLO_PROVIDER_OPENAI_BASE_URL": "https://api.example/v1",
                            "ILLO_PROVIDER_ANTHROPIC_API_KEY": ""})
    assert set(table) == {"chatgpt"}
    table = providers.load({"ILLO_PROVIDER_OPENAI_API_KEY": "fake-key",
                            "ILLO_PROVIDER_OPENAI_BASE_URL": "https://api.example/v1/",
                            "ILLO_PROVIDER_CHATGPT_BASE_URL": "https://proxy.example/codex"})
    assert table["openai"].base_url == "https://api.example/v1"
    assert table["openai"].api_key == "fake-key" and "fake-key" not in repr(table["openai"])
    assert table["chatgpt"].base_url == "https://proxy.example/codex"
    assert "fake" not in table


@pytest.mark.parametrize("environ,variable", [
    ({"ILLO_PROVIDER_OPENAI_APIKEY": "fake-key"}, "ILLO_PROVIDER_OPENAI_APIKEY"),
    ({"ILLO_PROVIDER_FAKE_API_KEY": "fake-key"}, "ILLO_PROVIDER_FAKE_API_KEY"),
    ({"ILLO_PROVIDER_OPENAI_WIRE": "chat"}, "ILLO_PROVIDER_OPENAI"),
    ({"ILLO_PROVIDER_CHATGPT_API_KEY": "fake-key"}, "ILLO_PROVIDER_CHATGPT_API_KEY"),
    ({"ILLO_PROVIDER_LOCAL_API_KEY": "fake-key"}, "ILLO_PROVIDER_LOCAL"),
    ({"ILLO_PROVIDER_LOCAL_API_KEY": "fake-key", "ILLO_PROVIDER_LOCAL_WIRE": "agent_sdk",
      "ILLO_PROVIDER_LOCAL_BASE_URL": "https://models.example/v1"}, "ILLO_PROVIDER_LOCAL"),
    ({"ILLO_PROVIDER_LOCAL_API_KEY": "fake-key", "ILLO_PROVIDER_LOCAL_WIRE": "chat",
      "ILLO_PROVIDER_LOCAL_BASE_URL": "https://user:fake-secret@models.example/v1"},
     "ILLO_PROVIDER_LOCAL_BASE_URL"),
    ({"ILLO_PROVIDER_LOCAL_API_KEY": "fake-key", "ILLO_PROVIDER_LOCAL_WIRE": "chat",
      "ILLO_PROVIDER_LOCAL_BASE_URL": "https://models.example/v1",
      "ILLO_PROVIDER_LOCAL_CAPABILITIES": "shell"}, "ILLO_PROVIDER_LOCAL_CAPABILITIES"),
    # The Chat Completions wire sends text only and has no web search tool.
    *(({"ILLO_PROVIDER_LOCAL_API_KEY": "fake-key", "ILLO_PROVIDER_LOCAL_WIRE": "chat",
        "ILLO_PROVIDER_LOCAL_BASE_URL": "https://models.example/v1",
        "ILLO_PROVIDER_LOCAL_CAPABILITIES": capability}, "ILLO_PROVIDER_LOCAL_CAPABILITIES")
      for capability in ("images", "web_search")),
])
def test_a_wrong_provider_variable_stops_the_service(environ, variable):
    with pytest.raises(ValueError, match=variable) as error:
        providers.load(environ)
    assert "fake-key" not in str(error.value) and "fake-secret" not in str(error.value)


@pytest.mark.parametrize("change,reason", [
    ({"provider": "anthropic"}, "no provider with this name"),
    ({"provider": "chatgpt"}, "ChatGPT sign-in"),
    ({"effort": "extreme"}, "effort"),
    ({"web_search": True, "provider": "local"}, "web search"),
    ({"smoke": True}, "Responses wire"),
    ({"fallback_provider": "openai"}, "fallback"),
])
async def test_a_route_that_does_not_fit_is_a_config_error(run_request, change, reason):
    from brain.providers import Provider

    table = {"fake": FAKE, "openai": openai(), "chatgpt": providers.BUILT_IN["chatgpt"],
             "local": Provider("local", "chat", "http://x", "k")}
    called = []

    async def runner(request, *_):
        called.append(request)
        yield event("final", text="no", usage={})

    for key, value in change.items():
        setattr(run_request, key, value)
    events = lines(await post(create_app(runner=runner, table=table), run_request))
    assert events[-1]["category"] == "config" and reason in events[-1]["message"]
    assert not called


@pytest.mark.parametrize("change,reason", [
    ({"effort": "high"}, "The fallback: The provider does not accept this effort"),
    ({"images": [{"mime": "image/png", "data": "ZmFrZQ=="}]}, "The fallback: The provider cannot"),
])
async def test_a_fallback_that_cannot_serve_the_request_is_a_config_error(run_request, change,
                                                                          reason):
    """The loop can switch to the fallback during the run. The same rule checks both."""
    from brain.models import ChatGPTAuth, RunImage
    from brain.providers import Provider

    table = {"chatgpt": providers.BUILT_IN["chatgpt"],
             "plain": Provider("plain", "responses", "http://x", "k", efforts=("low",))}
    run_request.provider, run_request.fallback_provider = "chatgpt", "plain"
    run_request.provider_auth = ChatGPTAuth(access_token="fake-access", account_id="fake-account")
    for key, value in change.items():
        setattr(run_request, key, [RunImage(**v) for v in value] if key == "images" else value)

    async def runner(request, *_):
        yield event("final", text="no", usage={})

    events = lines(await post(create_app(runner=runner, table=table), run_request))
    assert events[-1]["category"] == "config" and reason in events[-1]["message"]


async def test_one_service_serves_chatgpt_and_openai_with_their_own_keys(run_request, monkeypatch):
    """The openai key comes from its own variable. ChatGPT tokens come with each run."""
    from brain.models import ChatGPTAuth

    monkeypatch.setenv("ILLO_PROVIDER_OPENAI_API_KEY", "fake-key-a")
    seen = []

    seen_tokens = []

    async def runner(request, provider, fallback):
        seen.append((request.provider, provider.name, provider.api_key, provider.base_url,
                     fallback.name if fallback else None))
        if provider.name == "chatgpt":
            seen_tokens.append(request.provider_auth.access_token.get_secret_value())
        yield event("final", text="ok", usage={})

    app = create_app(runner=runner)
    run_request.provider = "openai"
    assert lines(await post(app, run_request))[-1]["type"] == "final"
    run_request.provider = "chatgpt"
    run_request.fallback_provider = "openai"
    run_request.provider_auth = ChatGPTAuth(access_token="fake-access", account_id="fake-account")
    assert lines(await post(app, run_request))[-1]["type"] == "final"
    assert seen == [
        ("openai", "openai", "fake-key-a", "https://api.openai.com/v1", None),
        ("chatgpt", "chatgpt", None, "https://chatgpt.com/backend-api/codex", "openai"),
    ]
    assert seen_tokens == ["fake-access"]


async def test_transcribe_with_the_fake_provider(monkeypatch):
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://brain"
    ) as http:
        params = {"provider": "fake", "model": "fake-model", "extension": "m4a"}
        refused = await http.post("/transcribe", params=params, content=b"audio")
        response = await http.post("/transcribe", params=params, content=b"audio",
                                   headers={"X-Illo-Brain-Secret": "service-test-secret"})
        missing = await http.post("/transcribe", params={**params, "provider": "chatgpt"},
                                  content=b"audio",
                                  headers={"X-Illo-Brain-Secret": "service-test-secret"})
    assert refused.status_code == 401
    assert response.json() == {"text": FAKE_TRANSCRIPT}
    assert missing.status_code == 422 and missing.json() == {"category": "config"}


@pytest.mark.parametrize("transcription", [True, False])
async def test_a_transcript_never_returns_the_provider_key(transcription):
    """A provider that echoes its authorization header gets no text into the inbound message."""
    from brain.providers import Provider

    def echo(request):
        return httpx.Response(200, json={"text": "Heard: " + request.headers["Authorization"]})

    table = {"plain": Provider("plain", "chat", "https://models.example/v1", "fake-plain-key",
                               transcription=transcription)}
    app = create_app(table=table, transport=httpx.MockTransport(echo))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://brain"
    ) as http:
        response = await http.post(
            "/transcribe", params={"provider": "plain", "model": "fake-model", "extension": "m4a"},
            content=b"audio", headers={"X-Illo-Brain-Secret": "service-test-secret"})
    assert "fake-plain-key" not in response.text
    # A provider without transcription is a settings error. A key echo is a provider failure.
    assert (response.status_code, response.json()) == (
        (502, {"category": "runtime"}) if transcription else (422, {"category": "config"}))


async def test_the_worker_never_sees_the_other_keys(run_request, tmp_path, monkeypatch):
    """The worker environment is an allowlist. A key with an old or unknown name stays out."""
    for name in ("ILLO_PROVIDER_OPENAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "ANTHROPIC_API_KEY", "ACME_TOKEN"):
        monkeypatch.setenv(name, "fake-other-key")
    code = ("import json,os,sys; sys.stdin.readline(); given=json.loads(sys.stdin.readline()); "
            "print(json.dumps({'type':'final','text':json.dumps(sorted(k for k in os.environ "
            "if 'fake-other-key' in os.environ[k] or k=='ILLO_BRAIN_SECRET')),"
            "'given':given['provider']['name']}),flush=True)")
    events = [e async for e in run_worker(run_request, FAKE, None, [sys.executable, "-c", code])]
    assert json.loads(events[-1]["text"]) == [] and events[-1]["given"] == "fake"


async def test_timeout_and_cancellation_close_runner(run_request):
    closed = asyncio.Event()

    async def slow(request, *_):
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

    async def blocking(request, *_):
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
    async def echo_secret(request, *_):
        yield event("text", text=request.mcp_token.get_secret_value())
        raise RuntimeError("private provider details")

    response = await post(create_app(runner=echo_secret), run_request)
    assert lines(response)[1]["text"] == "[redacted]"
    assert lines(response)[-1]["category"] == "internal"
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
        async with aclosing(run_worker(run_request, FAKE, None, [sys.executable, "-c", code])) as stream:
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


async def test_provider_identifiers_cannot_log_secrets(run_request, caplog, monkeypatch):
    monkeypatch.setenv("ILLO_PROVIDER_OPENAI_API_KEY", "fake-sensitive-key")

    async def failure(request, *_):
        yield event(
            "error",
            category="runtime",
            code="fake-sensitive-key",
            provider_type="fake-sensitive-key",
            http_status=400,
        )

    response = await post(create_app(runner=failure), run_request)
    assert "fake-sensitive-key" not in response.text + caplog.text
    assert "http_status=400" in caplog.text


async def test_worker_exit_without_an_answer_is_an_internal_failure(run_request):
    events = [e async for e in run_worker(run_request, FAKE, None, [sys.executable, "-c", "pass"])]
    assert events[-1]["type"] == "error" and events[-1]["category"] == "internal"


def test_unknown_worker_exception_keeps_only_the_internal_category():
    from brain.errors import failure_event

    result = failure_event(ValueError("fake-credential-in-an-error"))
    assert result["category"] == "internal"
    assert "fake-credential" not in json.dumps(result)
