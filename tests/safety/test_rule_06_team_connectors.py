"""Rule 6 and director decision 3: tainted MCP reads need explicit permission."""

from contextlib import asynccontextmanager

from illo.connectors.config import Connector
from illo.connectors.mcp_client import MCPClient
from illo.ledger import runs
from tests.fakes import FakeMCPSession


async def test_rule_06_tainted_connector_read_requires_opt_in(app, rpc):
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
        },
    )
    ctx.workspace = ctx.workspace.model_copy(update={"connectors": [config]})
    session = FakeMCPSession(tools=["read", "private_read", "write"])

    @asynccontextmanager
    async def factory(config, bearer):
        yield session

    ctx.mcp_client = MCPClient(session_factory=factory)
    async with ctx.pool.connection() as conn:
        run = await runs.create(
            conn,
            principal="team",
            person_id=None,
            profile="tainted",
            tainted=True,
            runtime="fake",
            model="fake",
        )
        token = await runs.mint_token(conn, run["id"], 60)
    response = await rpc(token, "tools/call", {"name": "connectors_list"})
    listed = response.json()["result"]["structuredContent"]["result"]
    assert [t["name"] for t in listed[0]["tools"]] == ["read"]
    for tool, expected in (
        ("read", "allow"),
        ("private_read", "deny"),
        ("write", "deny"),
        ("hidden", "deny"),
    ):
        response = await rpc(
            token,
            "tools/call",
            {
                "name": "connectors_call",
                "arguments": {"connector": "docs", "tool": tool, "arguments": {}},
            },
        )
        assert response.json()["result"]["structuredContent"]["decision"] == expected
    assert [c["name"] for c in session.calls] == ["read"]
