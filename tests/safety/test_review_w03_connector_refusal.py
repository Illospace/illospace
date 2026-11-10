"""Rule 3: a personal read refuses a connector call without an unusable draft."""

# ruff: noqa: F811

import pytest

from illo.db import one
from tests.connectors.test_mcp import call, mcp_app  # noqa: F401

MESSAGE = "This run read personal data, so it cannot call a connector."


@pytest.mark.parametrize("remote", ["read", "write", "send"])
async def test_rule_03_personal_read_denies_connector_without_a_draft(mcp_app, rpc, rpc_token, remote):
    ctx, _, session = mcp_app
    run, token = rpc_token
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (run["id"],))
    result = await call(rpc, token, remote)
    assert result["decision"] == "deny"
    assert result["reason"] == MESSAGE
    assert not session.calls and not session.listings
    async with ctx.pool.connection() as conn:
        assert await one(conn, "SELECT * FROM drafts") is None


async def test_rule_03_connector_performer_cannot_bypass_personal_read(mcp_app, rpc, rpc_token):
    ctx, _, session = mcp_app
    run, token = rpc_token
    result = await call(rpc, token, "send")
    assert result["decision"] == "draft"
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (run["id"],))
        draft = await one(conn, "SELECT * FROM drafts")
    with pytest.raises(ValueError, match="personal data"):
        await ctx.draft_performers["connectors_call"](ctx, {**draft, "state": "approved"})
    assert not session.calls
