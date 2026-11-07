"""External MCP transport, auth, policy, and result boundaries."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from brain.systems.external_mcp import config, runtime, tokens
from brain.systems.external_mcp.client import McpAuthError, McpClient, McpProtocolError
from tests.external_mcp_support import CLIENT_SECRET, configured_mcp, mcp_server  # noqa: F401

NAME = "mcp__reports__get_report"


async def surface():
    return await runtime.external_mcp_tool_surface(org_id="org-test", run_id=42, actor_user_id="user-test")


async def test_external_mcp_client_credentials_and_allowed_tools(configured_mcp, caplog):
    caplog.set_level(logging.DEBUG)
    tools, handlers, statuses = await surface()
    assert [tool["name"] for tool in tools] == [NAME]
    result = await handlers[NAME](id="report-1")
    assert result["content"] == "Report data"
    assert result["untrusted_data"] is True
    assert result["structured_content"] == {"total": 3}
    assert statuses[0]["available"] is True
    peer = configured_mcp.peer
    assert peer.token_requests == 1
    assert peer.token_forms == [{"grant_type": ["client_credentials"], "scope": ["reports.read"], "resource": ["https://mcp.example.com/"]}]
    audit = configured_mcp.audit.call_args.kwargs
    assert audit["outcome"] == "ok" and audit["run_id"] == 42
    assert "arguments" not in audit and "result" not in audit
    assert peer.token not in caplog.text
    assert CLIENT_SECRET not in caplog.text
    assert not any(record.name.startswith(("httpx", "httpcore")) for record in caplog.records)


@pytest.mark.parametrize("failures,expected_tokens,expected_calls", [(1, 2, 2), (2, 2, 2)])
async def test_external_mcp_401_one_refresh_and_retry(configured_mcp, failures, expected_tokens, expected_calls):
    _, handlers, _ = await surface()
    configured_mcp.peer.unauthorized_calls = failures
    result = await handlers[NAME](id="report-1")
    assert configured_mcp.peer.token_requests == expected_tokens
    assert len(configured_mcp.peer.calls) == expected_calls
    if failures == 2:
        assert result["error_kind"] == "auth_failed"
        assert configured_mcp.audit.call_args.kwargs["outcome"] == "auth_failed"
    else:
        assert result["content"] == "Report data"


async def test_external_mcp_token_cache_parallel_refresh_and_config_change(configured_mcp):
    server = configured_mcp.server
    cache = tokens.TOKEN_CACHE
    await asyncio.gather(*(cache.get(server, actor_user_id="user-test") for _ in range(5)))
    assert configured_mcp.peer.token_requests == 1
    cached = cache._tokens[cache.key(server)]
    cached.expires_at = tokens.time.monotonic() + 59
    await cache.get(server, actor_user_id="user-test")
    assert configured_mcp.peer.token_requests == 2
    server.updated_at += timedelta(seconds=1)
    await cache.get(server, actor_user_id="user-test")
    assert configured_mcp.peer.token_requests == 3


async def test_external_mcp_token_error_never_echoes_secrets(configured_mcp, caplog):
    caplog.set_level(logging.DEBUG)
    configured_mcp.peer.token_error = {"error": CLIENT_SECRET, "error_description": CLIENT_SECRET}
    with pytest.raises(McpAuthError) as caught:
        await tokens.TOKEN_CACHE.get(configured_mcp.server, actor_user_id="user-test")
    assert str(caught.value) == "Token endpoint returned HTTP 401"
    assert CLIENT_SECRET not in caplog.text
    assert configured_mcp.peer.token_requests == 1


async def test_external_mcp_sse_matching_response(configured_mcp):
    configured_mcp.peer.sse = True
    _, handlers, _ = await surface()
    assert (await handlers[NAME]())["content"] == "Report data"


@pytest.mark.parametrize("rpc_error", [False, True])
async def test_external_mcp_remote_errors(configured_mcp, rpc_error):
    configured_mcp.peer.rpc_error = rpc_error
    configured_mcp.peer.tool_error = not rpc_error
    configured_mcp.peer.result = {"content": [{"type": "text", "text": "Report is unavailable"}]}
    _, handlers, _ = await surface()
    result = await handlers[NAME]()
    assert result["error"] == "Report is unavailable" and result["error_kind"] == "remote_tool_error"
    assert result["untrusted_data"] is True
    assert configured_mcp.audit.call_args.kwargs["outcome"] == "error"


async def test_external_mcp_timeout(configured_mcp):
    configured_mcp.server.timeout_seconds = 1
    _, handlers, _ = await surface()
    configured_mcp.peer.delay = 2
    result = await handlers[NAME]()
    assert result["error_kind"] == "timeout"
    assert "timed out after 1 s" in result["error"]
    assert len(configured_mcp.peer.calls) == 1


async def test_external_mcp_down_and_missing_secret(configured_mcp):
    configured_mcp.server.url = "http://127.0.0.1:0/mcp"
    tools, handlers, statuses = await surface()
    assert tools == [] and handlers == {}
    assert statuses[0]["available"] is False
    assert statuses[0]["reason"] == "unavailable"
    configured_mcp.server.url = configured_mcp.peer.url
    token_requests = configured_mcp.peer.token_requests
    # The failure is remembered for a short time: the next run does not connect again.
    _, _, statuses = await surface()
    assert statuses[0]["reason"] == "unavailable"
    assert configured_mcp.peer.token_requests == token_requests
    # A configuration change (new updated_at) connects again at once.
    configured_mcp.server.updated_at += timedelta(seconds=1)
    _, handlers, statuses = await surface()
    assert statuses[0]["available"] is True and NAME in handlers
    configured_mcp.server.updated_at += timedelta(seconds=1)
    configured_mcp.values.clear()
    tokens.TOKEN_CACHE.invalidate(configured_mcp.server)
    _, _, statuses = await surface()
    assert statuses[0]["reason"] == "missing_secret"


async def test_external_mcp_none_bearer_and_revoked_tool(configured_mcp):
    configured_mcp.server.auth_mode = "none"
    configured_mcp.peer.require_auth = False
    _, handlers, _ = await surface()
    assert (await handlers[NAME]())["untrusted_data"]
    assert configured_mcp.peer.token_requests == 0
    configured_mcp.server.auth_mode = "bearer"
    configured_mcp.server.bearer_token_key = "bearer"
    configured_mcp.peer.require_auth = True
    configured_mcp.peer.token = "fixture-bearer"
    assert (await handlers[NAME]())["content"] == "Report data"
    configured_mcp.server.allowed_tools = []
    assert (await handlers[NAME]())["error_kind"] == "unavailable"
    assert len(configured_mcp.peer.calls) == 2


async def test_external_mcp_schema_name_collision_and_cache(configured_mcp):
    peer = configured_mcp.peer
    peer.tools += [{"name": "bad.name", "inputSchema": {"type": "object"}},
                   {"name": "x" * 80}, {"name": "array", "inputSchema": {"type": "array"}},
                   {"name": "large", "inputSchema": {"type": "object", "description": "x" * 17000}},
                   {"name": "default"}]
    configured_mcp.server.allowed_tools = [tool["name"] for tool in peer.tools]
    tools, _, statuses = await runtime.external_mcp_tool_surface(org_id="org-test", run_id=42, existing_names=[NAME])
    assert [tool["name"] for tool in tools] == ["mcp__reports__update_report", "mcp__reports__default"]
    assert tools[-1]["input_schema"] == {"type": "object", "properties": {}}
    reasons = {item["reason"] for item in statuses[0]["skipped_tools"]}
    assert reasons == {"invalid_name", "invalid_schema", "name_collision"}
    requests = list(peer.requests)
    await surface()
    assert requests == peer.requests
    configured_mcp.server.updated_at += timedelta(seconds=1)
    await surface()
    assert len(peer.requests) > len(requests)


def test_external_mcp_untrusted_result_budget():
    result = runtime.untrusted_result("reports", "get_report", {"content": [
        {"type": "image", "data": "raw"}, {"type": "text", "text": "漢\"\\" * 20000}],
        "structuredContent": {"data": "x" * 20000}}, budget=1500)
    assert result["untrusted_data"] and result["truncated"]
    assert "cut" in result["notice"]
    assert "[image content omitted]" in result["content"]
    assert len(json.dumps(result)) <= 1500
    assert "structured_content" not in result


async def test_external_mcp_no_servers_no_network_and_unchanged_surface(monkeypatch):
    from brain.systems.runs.recipes.shared import merge_external_mcp_surface
    monkeypatch.setattr(runtime, "load_servers", AsyncMock(return_value=[]))
    def no_network(*a, **kw):
        pytest.fail("No servers must not open an HTTP client")
    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    tools, handlers = [{"name": "read_file"}], {"read_file": object()}
    run = SimpleNamespace(request=SimpleNamespace(org_id="org-test", user_id="user-test", metadata={}), run=SimpleNamespace(id=42))
    merged, raw, metadata = await merge_external_mcp_surface(run, tools, handlers)
    assert merged is tools and raw is handlers and metadata == {}
    monkeypatch.setattr(runtime, "load_servers", AsyncMock(side_effect=RuntimeError("private DB details")))
    assert await surface() == ([], {}, [])


@pytest.mark.parametrize("name", ["", "UPPER", "two__parts", "x" * 33, "bad-name"])
async def test_external_mcp_invalid_names(name):
    with pytest.raises(ValueError, match="Name"):
        await config.validate_config({"name": name, "url": "https://mcp.example.com/mcp"})


@pytest.mark.parametrize("url", ["http://169.254.169.254/mcp", "https://169.254.1.1/mcp", "http://[fe80::1]/mcp",
                                 "https://[fd00:ec2::254]/mcp", "https://metadata.google.internal/mcp", "https://[::ffff:169.254.1.1]/mcp",
                                 "https://0.0.0.0/mcp", "https://[::]/mcp"])
async def test_external_mcp_metadata_always_rejected(monkeypatch, url):
    monkeypatch.setenv("ILLO_EXTERNAL_MCP_ALLOW_PRIVATE_HTTP", "1")
    with pytest.raises(ValueError, match="Metadata"):
        await config.validate_url(url)


async def test_external_mcp_private_http_policy(monkeypatch):
    monkeypatch.delenv("ILLO_EXTERNAL_MCP_ALLOW_PRIVATE_HTTP", raising=False)
    with pytest.raises(ValueError, match="HTTP requires"):
        await config.validate_url("http://127.0.0.1/mcp")
    monkeypatch.setenv("ILLO_EXTERNAL_MCP_ALLOW_PRIVATE_HTTP", "true")
    assert await config.validate_url("http://localhost/mcp")
    with pytest.raises(ValueError, match="HTTP requires"):
        await config.validate_url("http://8.8.8.8/mcp")
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", AsyncMock(return_value=[(2, 1, 6, '', ('127.0.0.1', 80)), (2, 1, 6, '', ('8.8.8.8', 80))]))
    with pytest.raises(ValueError, match="HTTP requires"):
        await config.validate_url("http://mixed.example.com/mcp")
    monkeypatch.setattr(loop, "getaddrinfo", AsyncMock(return_value=[(2, 1, 6, '', ('169.254.1.1', 443))]))
    with pytest.raises(ValueError, match="Metadata"):
        await config.validate_url("https://mcp.example.com/mcp")


async def test_external_mcp_config_clears_other_auth_fields():
    data = await config.validate_config({"name": "reports", "url": "https://127.0.0.1/mcp", "auth_mode": "none",
                                        "bearer_token_key": "unused", "client_secret_key": "unused", "scope": "unused",
                                        "allowed_tools": ["get_report", "get_report"]})
    assert data["bearer_token_key"] is None and data["client_secret_key"] is None and data["scope"] is None
    assert data["allowed_tools"] == ["get_report"]
    with pytest.raises(ValueError, match="client_secret_key"):
        await config.validate_config({**data, "auth_mode": "client_credentials", "token_url": "https://127.0.0.1/token", "client_id_key": "client_id"})


@pytest.mark.parametrize("mode", ["enforce", "require_approval"])
async def test_external_mcp_real_executor_policy_gate(configured_mcp, monkeypatch, mode):
    from tests.test_agent_run_runtime import _runtime
    from brain.systems.runs import tools as run_tools
    from brain.systems.runs.tool_catalog.registry import get_tool_registration
    monkeypatch.setenv("AGENT_ACTION_POLICY_MODE", mode)
    manifests = []
    def record(manifest):
        manifests.append(manifest)
        return 1
    monkeypatch.setattr(run_tools, "record_action_manifest", record)
    monkeypatch.setattr(run_tools, "complete_action_manifest", AsyncMock())
    _, handlers, _ = await surface()
    run = _runtime("fast")
    wrapped = run_tools.wrap_tool_handlers(handlers, executor=run.tool_executor(), run_id=42)
    result = await wrapped[NAME](id="report-1")
    assert result["content"] == "Report data"
    assert len(manifests) == 1
    assert manifests[0].policy_result == "allow_audit"
    assert manifests[0].approval_required is False
    assert manifests[0].risk == "high"
    registration = get_tool_registration(NAME)
    assert registration.side_effect_class.value == "external_call"
    assert registration.parallel_safety.value == "serial"
    assert "run.tool_completed" in [event.event_type for event in run.store.events]
    assert run.store.artifacts[-1].payload["side_effect"] == "external_call"


@pytest.mark.parametrize("recipe", ["fast", "worker"])
@pytest.mark.parametrize("disabled", [False, True])
async def test_external_mcp_recipe_and_direct_policy(configured_mcp, monkeypatch, recipe, disabled):
    from tests.test_agent_run_runtime import _runtime
    from brain.systems.runs.recipes.fast import FastRecipe
    from brain.systems.runs.recipes.workers import WorkerRecipe
    from brain.systems.runs.direct_agent import _apply_tool_policy
    from brain.systems.runs.tool_catalog.handlers.workers import _merge_tool_policy
    from brain.systems.runs import tools as run_tools
    module = "brain.systems.runs.recipes." + ("fast" if recipe == "fast" else "workers")
    monkeypatch.setattr(module + ".build_agent_tools", lambda role: [{"name": "native"}])
    monkeypatch.setattr(module + ".build_tool_handlers", lambda **kwargs: {"native": lambda: None})
    monkeypatch.setattr(run_tools, "record_action_manifest", lambda manifest: 1)
    monkeypatch.setattr(run_tools, "complete_action_manifest", AsyncMock())
    if recipe == "worker":
        monkeypatch.setattr(module + ".async_summarize_run_usage_in_savepoint", AsyncMock(return_value={}))
    captured = {}
    async def invoke(spec):
        captured["spec"] = spec
        names = {tool["name"] for tool in spec.tools}
        if disabled:
            assert NAME not in names and NAME not in spec.tool_handlers
            with pytest.raises(KeyError):
                spec.tool_handlers[NAME]
        else:
            assert NAME in names
            assert (await spec.tool_handlers[NAME]())["content"] == "Report data"
        return SimpleNamespace(success=True, output="Done", error=None)
    monkeypatch.setattr(module + ".invoke_direct_agent_async", invoke)
    run = _runtime(recipe)
    if disabled:
        policy = {"disabled_tools": [NAME]}
        run.request.metadata["tool_policy"] = _merge_tool_policy(policy, headless=True) if recipe == "worker" else policy
    result = await (FastRecipe() if recipe == "fast" else WorkerRecipe()).execute(run)
    assert result.status.value == "completed"
    assert len(configured_mcp.peer.calls) == (0 if disabled else 1)
    tools, handlers, _ = await surface()
    filtered, raw = _apply_tool_policy(tools, handlers, {"tool_policy": {"disabled_tools": [NAME]}})
    assert filtered == [] and raw == {}


async def test_external_mcp_fast_recipe_completes_with_down_server(configured_mcp, monkeypatch):
    from tests.test_agent_run_runtime import _runtime
    from brain.systems.runs.recipes.fast import FastRecipe
    configured_mcp.server.url = "http://127.0.0.1:0/mcp"
    module = "brain.systems.runs.recipes.fast"
    monkeypatch.setattr(module + ".build_agent_tools", lambda role: [{"name": "native"}])
    monkeypatch.setattr(module + ".build_tool_handlers", lambda **kwargs: {})
    async def invoke(spec):
        assert spec.tools == [{"name": "native"}]
        assert spec.metadata["external_mcp_status"][0]["available"] is False
        return SimpleNamespace(success=True, output="Done", error=None)
    monkeypatch.setattr(module + ".invoke_direct_agent_async", invoke)
    result = await FastRecipe().execute(_runtime("fast"))
    assert result.status.value == "completed"


def test_external_mcp_capability_status_uses_run_discovery():
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.capabilities import _handle_read_capabilities
    from brain.systems.runs.tool_catalog.handlers.self_context import _handle_read_self_context
    metadata = {"external_mcp_status": [
        {"name": "reports", "available": False, "reason": "timeout", "tool_names": []},
        {"name": "support", "available": True, "reason": None, "tool_names": ["mcp__support__lookup"]},
    ]}
    with bind_agent_context({"execution_metadata": metadata}):
        result = json.loads(_handle_read_capabilities(capability_key="external_mcp:reports"))
        assert result["capabilities"][0]["availability"] == "unavailable"
        assert "timeout" in result["capabilities"][0]["summary"]
        context = json.loads(_handle_read_self_context(include_paths=False, include_git=False))
        assert context["external_mcp"][1]["tools"] == ["mcp__support__lookup"]


async def test_external_mcp_short_token_and_parallel_401_refresh(configured_mcp):
    configured_mcp.peer.token_lifetime = 10
    cache = tokens.TOKEN_CACHE
    server = configured_mcp.server
    first = await cache.get(server, actor_user_id="user-test")
    cached = cache._tokens[cache.key(server)]
    assert cached.refresh_margin == 2
    refreshed = await asyncio.gather(*(cache.get(server, actor_user_id="user-test", force_refresh=True, rejected_token=first) for _ in range(5)))
    assert configured_mcp.peer.token_requests == 2
    assert len(set(refreshed)) == 1
    cache._tokens[cache.key(server)].expires_at = tokens.time.monotonic() + 1
    await cache.get(server, actor_user_id="user-test")
    assert configured_mcp.peer.token_requests == 3


async def test_external_mcp_auth_echo_is_redacted(configured_mcp):
    _, handlers, _ = await surface()
    configured_mcp.peer.result = {"content": [{"type": "text", "text": configured_mcp.peer.token}]}
    result = await handlers[NAME]()
    assert result["content"] == "[redacted]"
    assert configured_mcp.peer.token not in json.dumps(result)


async def test_external_mcp_url_rechecked_before_connect(configured_mcp, monkeypatch):
    _, handlers, _ = await surface()
    monkeypatch.delenv("ILLO_EXTERNAL_MCP_ALLOW_PRIVATE_HTTP")
    result = await handlers[NAME]()
    assert result["error_kind"] == "protocol_error"
    assert configured_mcp.peer.calls == []


async def test_external_mcp_cancelled_call_is_not_audited_as_success(configured_mcp):
    _, handlers, _ = await surface()
    configured_mcp.peer.delay = .2
    task = asyncio.create_task(handlers[NAME]())
    async with asyncio.timeout(2):
        while not configured_mcp.peer.calls:
            await asyncio.sleep(.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert configured_mcp.audit.call_args.kwargs["outcome"] == "cancelled"
