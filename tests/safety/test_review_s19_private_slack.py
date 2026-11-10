"""Rules 2 and 3: private Slack text needs live membership and stays private."""

import pytest

from illo.channels.slack.intake import SlackIntake
from illo.db import one
from illo.ledger import items, runs


async def private_info(**kwargs):
    return {"channel": {"id": kwargs["channel"], "is_private": True}}


async def test_rule_02_private_channel_never_enters_shared_index(app, slack_client, fake_brain):
    ctx = app.state.ctx
    slack_client.conversations_info = private_info
    async with ctx.pool.connection() as conn:
        await items.upsert(conn, source_ref="slack:C_ACME_ALERTS:99.1", kind="slack",
                           title="Leadership", body="Old private plan")
    result = await SlackIntake(ctx, "U_ACME_ILLO").receive({"event": {
        "type": "message", "channel": "C_ACME_ALERTS", "ts": "100.1",
        "user": "U_ACME_ADA", "text": "Private plan",
    }})
    assert result["decision"] == "ignore"
    assert not fake_brain.requests
    async with ctx.pool.connection() as conn:
        assert not await items.search(conn, "plan", principal="person:ben")
        assert not await items.get(conn, "slack:C_ACME_ALERTS:99.1", principal="team")
        assert not await items.get(conn, "slack:C_ACME_ALERTS:100.1", principal="team")


@pytest.mark.parametrize("member", [False, True])
async def test_rule_02_03_private_thread_requires_live_member_and_marks_read(
    app, rpc, rpc_token, slack_client, member
):
    run, token = rpc_token
    slack_client.conversations_info = private_info
    pages = []
    async def members(**kwargs):
        pages.append(kwargs.get("cursor"))
        if not kwargs.get("cursor"):
            return {"members": ["U_ACME_BEN"], "response_metadata": {"next_cursor": "page2"}}
        return {"members": ["U_ACME_ADA"] if member else []}
    slack_client.conversations_members = members
    slack_client.messages = {"100.1": {"ts": "100.1", "text": "Private plan"}}
    response = await rpc(token, "tools/call", {"name": "slack_read_thread", "arguments": {
        "channel": "C_ACME_TEAM", "thread_ts": "100.1",
    }})
    result = response.json()["result"]["structuredContent"]
    assert result["decision"] == ("allow" if member else "deny")
    assert pages == [None, "page2"]
    assert any(c["method"] == "conversations_replies" for c in slack_client.calls) == member
    async with app.state.ctx.pool.connection() as conn:
        saved = await one(conn, "SELECT * FROM runs WHERE id=%s", (run["id"],))
        assert saved["personal_read"] == member
    if member:
        member = False
        again = await rpc(token, "tools/call", {"name": "slack_read_thread", "arguments": {
            "channel": "C_ACME_TEAM", "thread_ts": "100.1",
        }})
        assert again.json()["result"]["structuredContent"]["decision"] == "deny"


async def test_rule_02_private_thread_refuses_team_run(app, rpc, slack_client):
    slack_client.conversations_info = private_info
    async with app.state.ctx.pool.connection() as conn:
        run = await runs.create(conn, principal="team", person_id=None, profile="team_channel",
                                runtime="fake", model="fake")
        token = await runs.mint_token(conn, run["id"], 60)
    response = await rpc(token, "tools/call", {"name": "slack_read_thread", "arguments": {
        "channel": "C_ACME_TEAM", "thread_ts": "100.1",
    }})
    assert response.json()["result"]["structuredContent"]["decision"] == "deny"


async def test_rule_02_configured_private_overrides_slack_public_flag(
    app, rpc, rpc_token, slack_client
):
    from illo.workspace import SlackChannel

    channel = next(c for c in app.state.ctx.workspace.config.slack.channels if c.id == "C_ACME_TEAM")
    channel.visibility = "private"
    assert SlackChannel.model_validate(channel.model_dump()).visibility == "private"
    async def members(**kwargs):
        return {"members": ["U_ACME_BEN"]}
    slack_client.conversations_members = members
    _, token = rpc_token
    response = await rpc(token, "tools/call", {"name": "slack_read_thread", "arguments": {
        "channel": "C_ACME_TEAM", "thread_ts": "100.1",
    }})
    assert response.json()["result"]["structuredContent"]["decision"] == "deny"
    assert not any(c["method"] == "conversations_replies" for c in slack_client.calls)


async def test_rule_02_migration_removes_unverified_old_slack_index(conn):
    from pathlib import Path

    await items.upsert(conn, source_ref="slack:C_PRIVATE:100.1", kind="slack",
                       title="Leadership", body="Private plan")
    await items.upsert(conn, source_ref="saved:plan", title="Public plan")
    path = Path(__file__).parents[2] / "illo/migrations/0100_slack_private_index.sql"
    await conn.execute(path.read_text())
    assert not await items.get(conn, "slack:C_PRIVATE:100.1", principal="team")
    assert await items.get(conn, "saved:plan", principal="team")


async def test_rule_02_missing_channel_privacy_fails_closed(app, rpc, rpc_token, slack_client):
    async def unknown(**kwargs):
        return {"channel": {"id": kwargs["channel"]}}
    slack_client.conversations_info = unknown
    _, token = rpc_token
    response = await rpc(token, "tools/call", {"name": "slack_read_thread", "arguments": {
        "channel": "C_ACME_TEAM", "thread_ts": "100.1",
    }})
    assert response.json()["result"]["structuredContent"]["decision"] == "deny"
    assert not any(c["method"] == "conversations_replies" for c in slack_client.calls)
