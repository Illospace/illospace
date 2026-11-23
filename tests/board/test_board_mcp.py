import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from illo.channels.mcp.board import NAMES
from illo.ledger import board


@pytest.mark.parametrize("enabled,scope", [(True, "agent"), (True, "collector"), (False, "agent")])
async def test_board_discovery_and_calls_require_enabled_agent_scope(app, client, enabled, scope):
    app.state.ctx.workspace.config.board.enabled = enabled
    agent = await client(scope=scope)
    initialize = (await agent.rpc("initialize")).json()["result"]
    listed = (await agent.rpc("tools/list")).json()["result"]["tools"]
    names = {t["name"] for t in listed}
    if enabled and scope == "agent":
        assert "board_inbox" in initialize["instructions"]
        assert NAMES <= names
    else:
        assert "instructions" not in initialize
        assert not NAMES & names
        for name in NAMES:
            assert await agent.call(name, error=True) == {
                "error": {"message": "The token scope does not allow this tool"},
            }


async def test_sdk_sees_identical_unread_counts_and_can_ack(app, client):
    ada, ben = await client(), await client("ben")
    post = await ben.call("board_post", {"title": "SDK", "text": "Check the results",
                                         "mentions": ["person:ada"]})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                headers={"Authorization": f"Bearer {ada.token}"}) as http:
        async with streamable_http_client("http://test/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                assert "board_inbox" in (await session.initialize()).instructions
                count = await session.call_tool("lane_search", {})
                assert not count.isError
                assert count.structuredContent == json.loads(count.content[0].text)
                assert count.structuredContent["unread_board_mentions"] == 1
                inbox = await session.call_tool("board_inbox", {})
                assert "unread_board_mentions" not in inbox.structuredContent
                assert inbox.structuredContent["mentions"][0]["post_id"] == post["post_id"]
                ack = await session.call_tool("board_ack", {"post_ids": [post["post_id"]]})
                assert ack.structuredContent == {"acked": 1}
                empty = await session.call_tool("lane_search", {})
                assert empty.structuredContent == {"lanes": []}


async def test_piggyback_db_failure_keeps_original_writes_and_response(app, client, monkeypatch, caplog):
    agent = await client()

    async def fails(conn, **kwargs):
        await conn.execute("SELECT * FROM missing_board_count_table")

    monkeypatch.setattr(board, "unread_count", fails)
    post = await agent.call("board_post", {"title": "Still works", "text": "Committed"})
    assert "unread_board_mentions" not in post
    result = await agent.call("board_read", {"thread_id": post["thread_id"]})
    assert result["posts"][0]["text"] == "Committed"
    assert "Could not count unread board mentions." in caplog.text
