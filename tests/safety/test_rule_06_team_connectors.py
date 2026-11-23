"""Rule 6 and director decision 3: tainted MCP reads need explicit permission."""

from contextlib import asynccontextmanager

from illo.connectors.config import Connector
from illo.connectors.mcp_client import MCPClient
from illo.ledger import runs
from tests.fakes import FakeMCPSession
from tests.tool_results import read_result


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
    listed = read_result(response.json()["result"])["result"]
    assert "docs · read · read" in listed
    assert "private_read" not in listed and "write" not in listed
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


async def test_rule_06_tainted_search_cannot_bypass_shared_drive_grant(app, rpc, monkeypatch):
    from illo.connectors import google
    from illo.sources import search
    from tests.fakes import FakeGoogleClient

    ctx = app.state.ctx
    monkeypatch.setattr(search, "_providers", {})
    ctx.workspace = ctx.workspace.model_copy(
        update={
            "connectors": [
                Connector(
                    key="drive-shared",
                    kind="google",
                    scope="team",
                    drive_id="acme-drive",
                    auth={"type": "service_account", "secret": "acme-service"},
                )
            ]
        }
    )
    await google.setup(ctx)
    ctx.google_client = FakeGoogleClient(result={"files": [{"id": "doc", "name": "Acme plan"}]})
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
    response = await rpc(
        token,
        "tools/call",
        {"name": "search_all", "arguments": {"query": "Acme plan", "kinds": ["doc"]}},
    )
    assert response.json()["result"]["structuredContent"]["decision"] == "deny"
    assert not ctx.google_client.calls
    hits = await search.search(ctx, "team", "Acme plan", kinds=["doc"])
    assert hits[0].title == "Acme plan"
    assert not hits.personal_read
    assert len(ctx.google_client.calls) == 1
