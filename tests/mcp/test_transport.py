import asyncio

import pytest

from illo.brain.client import BrainEvent
from illo.db import all_rows, one
from illo.ledger import tokens
from tests.mcp.client import AgentClient


async def test_initialize_list_and_alias_header(app, http):
    agent = await AgentClient.create(app, http)
    response = await agent.rpc("initialize", {"protocolVersion": "2025-11-25"})
    assert response.json()["result"]["protocolVersion"] == "2025-11-25"
    response = await agent.rpc(
        "tools/list", path="/api/mcp", headers={"X-Illo-Bridge-Token": agent.token}
    )
    tools = response.json()["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "illo_submit",
        "illo_read",
        "illo_get_result",
        "lane_report",
        "lane_search",
        "ask_create",
        "ask_pull",
        "ask_ack",
        "ask_cancel",
        "signal_submit",
    }
    assert all(t["inputSchema"]["additionalProperties"] is False for t in tools)
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT last_used_at FROM tokens WHERE id=%s", (agent.token_id,))
        assert row["last_used_at"]
        audits = await all_rows(conn, "SELECT * FROM audit ORDER BY id")
        assert [r["tool"] for r in audits] == ["initialize", "tools/list"]
        assert all(r["token_id"] == agent.token_id for r in audits)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer unknown"},
        {"Authorization": "Basic abc"},
        {"X-Illo-Bridge-Token": "bad"},
    ],
)
async def test_auth_failures(http, headers):
    response = await http.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
    )
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"


async def test_revoked_and_inactive_tokens(app, http):
    agent = await AgentClient.create(app, http)
    async with app.state.ctx.pool.connection() as conn:
        await tokens.revoke(conn, agent.token_id)
    assert (await agent.rpc("initialize")).status_code == 401
    active = await AgentClient.create(app, http)
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET active=false WHERE id=%s", (active.person_id,))
    assert (await active.rpc("tools/list")).status_code == 401


async def test_errors_and_audit_redaction(app, http):
    agent = await AgentClient.create(app, http)
    await agent.call("illo_submit", {"message": "  "}, error=True)
    await agent.call("illo_read", {"capability": "unknown"}, error=True)
    await agent.call(
        "illo_read", {"capability": "knowledge.search", "arguments": {"query": "cobalt"}}
    )
    response = await agent.rpc("tools/call", {"name": "unknown"})
    assert response.json()["error"]["code"] == -32602
    async with app.state.ctx.pool.connection() as conn:
        rows = await all_rows(conn, "SELECT * FROM audit ORDER BY id")
        assert [r["decision"] for r in rows] == ["deny", "deny", "allow", "deny"]
        assert all(r["principal"] == "person:ada" for r in rows)
        assert all(set(r["args"].values()) <= {"[redacted]"} for r in rows)
        assert all(agent.token not in str(r) for r in rows)


@pytest.mark.parametrize(
    "body,code",
    [
        ([], -32600),
        ({"jsonrpc": "1.0"}, -32600),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}, -32602),
        ({"jsonrpc": "2.0", "id": 1, "method": "unknown"}, -32601),
    ],
)
async def test_invalid_rpc_is_audited(app, http, body, code):
    agent = await AgentClient.create(app, http)
    response = await http.post(
        "/mcp", headers={"Authorization": f"Bearer {agent.token}"}, json=body
    )
    assert response.json()["error"]["code"] == code
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT decision FROM audit"))["decision"] == "deny"


async def test_bad_json_and_notification(app, http):
    agent = await AgentClient.create(app, http)
    headers = {"Authorization": f"Bearer {agent.token}"}
    response = await http.post("/mcp", headers=headers, content="{")
    assert response.json()["error"]["code"] == -32700
    response = await http.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert response.status_code == 202


async def test_requests_return_before_the_brain_finishes(app, http, fake_brain):
    agent = await AgentClient.create(app, http)
    started, finish = asyncio.Event(), asyncio.Event()

    async def run(request):
        started.set()
        await finish.wait()
        yield BrainEvent(type="final", text="The answer is ready.")

    fake_brain.run = run
    accepted = await asyncio.wait_for(
        agent.call("illo_submit", {"message": "Give me an update."}), 2
    )
    assert accepted["status"] == "accepted"
    await asyncio.wait_for(started.wait(), 2)
    pending = await agent.call("illo_get_result", {"event_id": accepted["event_id"]})
    assert pending["run_status"] == "running" and not pending["terminal"]
    finish.set()
    await agent.settle()
    result = await agent.call("illo_get_result", {"event_id": accepted["event_id"]})
    assert result["status"] == result["run_status"] == "completed"
    assert result["final_answer"] == "The answer is ready." and result["terminal"]
    async with app.state.ctx.pool.connection() as conn:
        event = await one(conn, "SELECT * FROM events WHERE channel='mcp'")
        run = await one(conn, "SELECT * FROM runs")
        assert event["principal"] == "person:ada" and run["profile"] == "agent_request"
        assert agent.token not in str(event) and run["token_hash"] is None


async def test_failed_request_has_a_terminal_failure(app, http, fake_brain):
    fake_brain.script = [BrainEvent(type="error", text="A fake failure")]
    agent = await AgentClient.create(app, http)
    saved = await agent.call("illo_submit", {"message": "Give me an update."})
    await agent.settle()
    result = await agent.call("illo_get_result", {"event_id": saved["event_id"]})
    assert result["terminal"] and result["status"] == "failed"
    assert result["failure"] and result["mutated_target_refs"] == []


async def test_official_sdk_can_initialize_list_and_call(app, http):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    agent = await AgentClient.create(app, http)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        headers={"Authorization": f"Bearer {agent.token}"},
    ) as client:
        async with streamable_http_client("http://test/mcp", http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                initialized = await session.initialize()
                assert initialized.model_dump(by_alias=True)["serverInfo"]["name"] == "illo-agents"
                listed = await session.list_tools()
                assert "illo_submit" in {tool.name for tool in listed.tools}
                result = await session.call_tool("lane_search", {})
                result = result.model_dump(by_alias=True)
                assert not result["isError"]
                assert result["structuredContent"] == {"lanes": []}
