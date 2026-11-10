"""Exercise the JSON-RPC boundary with a real database and HTTP requests."""

from illo.db import all_rows, one
from illo.ledger import people, runs, tokens
from illo.tools.registry import Tool


async def test_auth_unknown_missing_and_expired(http, app, rpc, rpc_token):
    assert (await http.post("/brain/mcp", json={})).status_code == 401
    assert (await rpc("unknown", "tools/list")).status_code == 401
    run, token = rpc_token
    assert (await rpc(token, "tools/list")).status_code == 200
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET token_expires_at=now()-interval '1 second' WHERE id=%s", (run["id"],)
        )
    assert (await rpc(token, "tools/list")).status_code == 401


async def test_client_token_is_not_a_brain_token(app, rpc):
    async with app.state.ctx.pool.connection() as conn:
        person = await people.get(conn, "ada")
        token = await tokens.create(conn, person["id"], "test")
    assert (await rpc(token, "tools/list")).status_code == 401


async def test_initialize_ping_errors_and_list(rpc, rpc_token, http):
    _, token = rpc_token
    result = (await rpc(token, "initialize", {"protocolVersion": "2025-06-18"})).json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert (await rpc(token, "ping")).json()["result"] == {}
    assert (await rpc(token, "unknown")).json()["error"]["code"] == -32601
    assert (await rpc(token, "notifications/initialized")).status_code == 202
    malformed = await http.post(
        "/brain/mcp", headers={"Authorization": f"Bearer {token}"}, content=b"{"
    )
    assert malformed.json()["error"]["code"] == -32700
    names = {t["name"] for t in (await rpc(token, "tools/list")).json()["result"]["tools"]}
    assert "rules_remove_mine" in names
    assert "github_create_issue" in names


async def test_filtered_list_denial_and_audit(app, rpc):
    async with app.state.ctx.pool.connection() as conn:
        run = await runs.create(
            conn,
            principal="team",
            person_id=None,
            profile="team_channel",
            runtime="fake",
            model="fake",
        )
        token = await runs.mint_token(conn, run["id"], 60)
    listed = (await rpc(token, "tools/list")).json()["result"]["tools"]
    assert "rules_remove_mine" not in {t["name"] for t in listed}
    result = (
        await rpc(token, "tools/call", {"name": "rules_remove_mine", "arguments": {"id": 1}})
    ).json()["result"]
    assert result["isError"] and result["structuredContent"]["decision"] == "deny"
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM audit WHERE run_id=%s", (run["id"],))
        assert row["principal"] == "team" and row["decision"] == "deny"
        assert row["args"] == {"id": "[redacted]"}


async def test_real_read_tools(app, rpc, rpc_token):
    _, token = rpc_token
    for name, args in (
        ("people_list", {}),
        ("people_get", {"key": "ada"}),
        ("lanes_search", {}),
        ("lanes_get", {"id": 999}),
        ("lanes_mine", {}),
        ("asks_list", {}),
        ("asks_get", {"id": 999}),
        ("rules_list", {}),
    ):
        response = await rpc(token, "tools/call", {"name": name, "arguments": args})
        assert response.json()["result"]["structuredContent"]["decision"] == "allow", name


async def test_bad_args_and_unknown_tool_are_audited(app, rpc, rpc_token):
    run, token = rpc_token
    for name, args in (
        ("lanes_search", {"principal": "person:ben"}),
        ("lanes_get", {"id": "wrong"}),
        ("missing_tool", {}),
    ):
        assert (await rpc(token, "tools/call", {"name": name, "arguments": args})).json()["result"][
            "isError"
        ]
    async with app.state.ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT id FROM audit WHERE run_id=%s", (run["id"],))) == 3


async def test_tool_failure_rolls_back_but_keeps_audit(app, rpc, rpc_token):
    run, token = rpc_token

    async def fail(ctx, args):
        await ctx.conn.execute("INSERT INTO kv (key,value) VALUES ('should-rollback','{}')")
        raise RuntimeError("Sensitive error details")

    app.state.ctx.registry.tools["people_list"] = Tool(
        "people_list", "people", "read", lambda a: "ledger", False, fail
    )
    response = await rpc(token, "tools/call", {"name": "people_list"})
    assert "Sensitive" not in response.text
    assert response.json()["result"]["isError"]
    async with app.state.ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT * FROM kv WHERE key='should-rollback'")
        assert (await one(conn, "SELECT decision FROM audit WHERE run_id=%s", (run["id"],)))[
            "decision"
        ] == "deny"


async def test_server_enforces_tool_call_limit(app, rpc, rpc_token):
    run, token = rpc_token
    app.state.ctx.workspace.config.brain.max_turns = 1
    assert not (await rpc(token, "tools/call", {"name": "people_list"})).json()["result"]["isError"]
    result = (await rpc(token, "tools/call", {"name": "people_list"})).json()["result"]
    assert result["isError"] and "turn limit" in result["structuredContent"]["reason"]


async def test_mcp_sdk_accepts_the_response_shapes(rpc, rpc_token):
    from mcp.types import CallToolResult, InitializeResult, ListToolsResult

    _, token = rpc_token
    initialized = (await rpc(token, "initialize")).json()["result"]
    assert (
        InitializeResult.model_validate(initialized).model_dump(by_alias=True)["serverInfo"]["name"]
        == "illo-brain-tools"
    )
    listed = (await rpc(token, "tools/list")).json()["result"]
    assert ListToolsResult.model_validate(listed).tools
    called = (await rpc(token, "tools/call", {"name": "people_list"})).json()["result"]
    assert CallToolResult.model_validate(called).model_dump(by_alias=True)["isError"] is False


async def test_malformed_call_is_audited(app, rpc, rpc_token):
    run, token = rpc_token
    result = (await rpc(token, "tools/call", {"name": "people_list", "arguments": []})).json()
    assert result["error"]["code"] == -32602
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT decision FROM audit WHERE run_id=%s", (run["id"],)))[
            "decision"
        ] == "deny"


async def test_broken_destination_is_denied_and_audited(app, rpc, rpc_token):
    run, token = rpc_token

    async def handler(ctx, args):
        raise AssertionError("Do not call this handler")

    def broken(args):
        raise ValueError("Private details")

    app.state.ctx.registry.tools["rules_remove_mine"] = Tool(
        "rules_remove_mine", "rules", "write_internal", broken, False, handler
    )
    response = await rpc(token, "tools/call", {"name": "rules_remove_mine"})
    assert response.json()["result"]["isError"]
    assert "Private details" not in response.text
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT decision FROM audit WHERE run_id=%s", (run["id"],)))[
            "decision"
        ] == "deny"
