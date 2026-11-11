"""Team MCP tests cover configured authority, outside data, and call limits."""

from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import ValidationError

from illo.connectors.config import Connector
from illo.connectors.mcp_client import LimitedStream, MCPClient
from illo.ledger import runs
from tests.fakes import FakeMCPSession
from tests.tool_results import read_result


@pytest.fixture
async def mcp_app(app):
    ctx = app.state.ctx
    config = Connector(
        key="docs",
        kind="mcp",
        scope="team",
        url="https://mcp.example.com/mcp",
        tools={
            "read": {"risk": "read", "tainted": True},
            "private_read": {"risk": "read"},
            "write": {"risk": "write_internal"},
            "send": {"risk": "send_outside"},
        },
    )
    ctx.workspace = ctx.workspace.model_copy(update={"connectors": [config]})
    session = FakeMCPSession(tools=["read", "private_read", "write", "send", "hidden"])

    @asynccontextmanager
    async def factory(config, bearer):
        yield session

    ctx.mcp_client = MCPClient(session_factory=factory)
    return ctx, config, session


async def call(rpc, token, name, **extra):
    response = await rpc(
        token,
        "tools/call",
        {
            "name": "connectors_call",
            "arguments": {
                "connector": "docs",
                "tool": name,
                "arguments": {"query": "plan"},
                **extra,
            },
        },
    )
    return read_result(response.json()["result"])


async def test_only_listed_tools_and_schema_cache(mcp_app, rpc, rpc_token):
    ctx, config, session = mcp_app
    _, token = rpc_token
    assert (await call(rpc, token, "hidden"))["decision"] == "deny"
    assert (await call(rpc, token, "read", connector="unknown"))["decision"] == "deny"
    for _ in range(2):
        result = await call(rpc, token, "read")
        assert result["decision"] == "allow"
        assert result["result"].startswith('<outside_data source="connector:docs">\n')
        assert result["result"].endswith("\n</outside_data>")
        assert "Ignore previous" in result["result"]
    assert session.listings == 1
    assert len(session.calls) == 2
    assert "hidden" not in ctx.mcp_client.schemas[config.model_dump_json()]


async def test_risk_mapping_and_approved_performer(mcp_app, rpc, rpc_token):
    ctx, _, session = mcp_app
    _, token = rpc_token
    assert (await call(rpc, token, "write"))["decision"] == "allow"
    result = await call(rpc, token, "send")
    assert result["decision"] == "draft"
    assert [c["name"] for c in session.calls] == ["write"]
    async with ctx.pool.connection() as conn:
        draft = await (
            await conn.execute("SELECT * FROM drafts WHERE id=%s", (result["result"]["draft_id"],))
        ).fetchone()
    assert draft["destination"] == "outside:docs"
    performer = ctx.draft_performers["connectors_call"]
    with pytest.raises(ValueError, match="approved"):
        await performer(ctx, draft)
    await performer(ctx, {**draft, "state": "approved"})
    assert session.calls[-1]["name"] == "send"


async def test_write_without_person_and_write_after_personal_read_are_denied(mcp_app, rpc, rpc_token):
    ctx, _, session = mcp_app
    async with ctx.pool.connection() as conn:
        run = await runs.create(
            conn,
            principal="team",
            person_id=None,
            profile="team_channel",
            runtime="fake",
            model="fake",
        )
        token = await runs.mint_token(conn, run["id"], 60)
    assert (await call(rpc, token, "send"))["decision"] == "deny"
    row, token = rpc_token
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (row["id"],))
    assert (await call(rpc, token, "write"))["decision"] == "deny"
    assert not session.calls


async def test_timeout_and_size_limit(mcp_app, rpc, rpc_token):
    ctx, config, session = mcp_app
    _, token = rpc_token
    config.timeout_s = 0.01
    session.delay = 0.2
    assert (await call(rpc, token, "read"))["decision"] == "deny"
    async with ctx.pool.connection() as conn:
        sensor = await (
            await conn.execute("SELECT * FROM sensors WHERE key='connector:docs'")
        ).fetchone()
        assert sensor["last_error_at"]
    config.timeout_s, session.delay, session.text, config.max_bytes = 2, 0, "x" * 2000, 1024
    assert (await call(rpc, token, "read"))["decision"] == "deny"
    stream = LimitedStream(httpx.ByteStream(b"x" * 20), 10)
    with pytest.raises(ValueError, match="too large"):
        _ = b"".join([chunk async for chunk in stream])


def test_config_rejects_tainted_write_and_unbounded_risk():
    for entry in ({"risk": "write_internal", "tainted": True}, {"risk": "delete"}):
        with pytest.raises(ValidationError):
            Connector(
                key="docs",
                kind="mcp",
                scope="team",
                url="https://mcp.example.com/mcp",
                tools={"bad": entry},
            )


async def test_official_streamable_http_client_with_fake_wire(mcp_app):
    import json

    ctx, config, _ = mcp_app
    methods = []

    def respond(request):
        body = json.loads(request.content)
        method = body["method"]
        methods.append(method)
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "Acme MCP", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "read", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "Acme result"}]}
        else:
            result = {}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    client = MCPClient(transport=httpx.MockTransport(respond))
    async with ctx.pool.connection() as conn:
        result = await client.call(ctx, conn, config, "read", {})
    assert result["data"]["content"][0]["text"] == "Acme result"
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
