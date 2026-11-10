"""Rule 4: configured connector writes keep their risk at the registry boundary."""

from illo.connectors.config import Connector


async def test_rule_04_connector_outside_write_is_a_draft(app, rpc, rpc_token):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(
        update={
            "connectors": [
                Connector(
                    key="docs",
                    kind="mcp",
                    scope="team",
                    url="https://mcp.example.com/mcp",
                    tools={"send": {"risk": "send_outside"}},
                )
            ]
        }
    )
    _, token = rpc_token
    response = await rpc(
        token,
        "tools/call",
        {
            "name": "connectors_call",
            "arguments": {
                "connector": "docs",
                "tool": "send",
                "arguments": {"text": "Acme update"},
            },
        },
    )
    result = response.json()["result"]["structuredContent"]
    assert result["decision"] == "draft"
    async with ctx.pool.connection() as conn:
        draft = await (
            await conn.execute("SELECT * FROM drafts WHERE id=%s", (result["result"]["draft_id"],))
        ).fetchone()
        assert draft["kind"] == "connectors_call"
        assert draft["destination"] == "outside:docs"
        assert not await (
            await conn.execute("SELECT * FROM sensors WHERE key='connector:docs'")
        ).fetchall()
