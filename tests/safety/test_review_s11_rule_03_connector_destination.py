"""Rule 3: connector arguments cannot leave after a personal read."""

# ruff: noqa: F811

from dataclasses import replace

import pytest

from illo.tools.checks import check
from illo.tools.connectors import call_connector
from illo.tools.registry import ToolContext
from tests.connectors.test_mcp import call, mcp_app  # noqa: F401


async def test_rule_03_connector_read_is_denied_after_personal_read(mcp_app, rpc, rpc_token):
    ctx, _, session = mcp_app
    run, token = rpc_token
    args = {"connector": "docs", "tool": "read", "arguments": {"query": "Private mail code"}}
    async with ctx.pool.connection() as conn:
        run = dict(run, personal_read=True)
        resolved = ctx.registry.tools["connectors_call"].resolve(ToolContext(conn, run, ctx), args)
        assert resolved.destination(args) == "outside:docs"
        assert check(run, resolved, args).outcome == "deny"
        await conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (run["id"],))
    result = await call(rpc, token, "read")
    assert result["decision"] == "deny"
    assert not session.calls and not session.listings
    async with ctx.pool.connection() as conn:
        draft = await (await conn.execute("SELECT * FROM drafts")).fetchone()
    assert draft is None


async def test_rule_03_direct_connector_handler_refuses_after_personal_read(mcp_app, rpc_token):
    ctx, _, session = mcp_app
    run, _ = rpc_token
    args = {"connector": "docs", "tool": "read", "arguments": {"query": "Private mail code"}}
    async with ctx.pool.connection() as conn:
        with pytest.raises(ValueError, match="personal data"):
            await call_connector(ToolContext(conn, dict(run, personal_read=True), ctx), args)
    assert not session.calls and not session.listings


async def test_rule_03_local_reads_still_work_after_personal_read(mcp_app, rpc_token):
    ctx, _, _ = mcp_app
    run, _ = rpc_token
    run = dict(run, personal_read=True)
    remote = replace(ctx.registry.tools["connectors_call"], destination=lambda a: "outside:docs")
    local = replace(remote, destination=lambda a: "ledger")
    assert check(run, remote, {}).outcome == "deny"
    assert check(run, local, {}).outcome == "allow"
    assert check(dict(run, personal_read=False), remote, {}).outcome == "allow"
