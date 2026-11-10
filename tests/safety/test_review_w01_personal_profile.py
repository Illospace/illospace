"""Rule 2: personal tools require person_dm even without an explicit profile list."""

from dataclasses import replace

import pytest

from illo.connectors.google import personal_call
from illo.tools.registry import Arguments, ToolContext
from tests.github.test_tools import call
from tests.safety.test_rule_02_03_06_google import prepare


async def test_rule_02_default_personal_tool_is_not_an_agent_grant(app, rpc, rpc_token):
    ctx = app.state.ctx
    run, token = rpc_token
    called = []
    async def handler(ctx, args):
        called.append(True)
        return {}
    ctx.registry.tools["mail_search"] = replace(
        ctx.registry.tools["mail_search"], profiles=None, arguments=Arguments, handler=handler,
    )
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE runs SET profile='agent_request' WHERE id=%s", (run["id"],))
    assert (await call(rpc, token, "mail_search", {}))["decision"] == "deny"
    assert not called
    response = await rpc(token, "tools/list")
    assert "mail_search" not in {tool["name"] for tool in response.json()["result"]["tools"]}


async def test_rule_02_google_handler_refuses_agent_request(app, rpc_token):
    ctx = await prepare(app)
    run, _ = rpc_token
    async with ctx.pool.connection() as conn:
        with pytest.raises(ValueError, match="owner"):
            await personal_call(ToolContext(conn, {**run, "profile": "agent_request"}, ctx),
                                "mail_search", {"query": "plan"})
    assert not ctx.google_client.calls
