"""Slack-private team channels require a complete membership check."""

from datetime import UTC, datetime, timedelta

import pytest

from illo.channels.slack.access import channel_private
from illo.channels.slack.intake import SlackIntake
from illo.db import one
from illo.ledger import items, kv
from illo.sources import search
from illo.workspace import SlackChannel
from tests.github.test_tools import call

CHANNEL = "C_ACME_TEAM"
KEY = f"slack:privacy:{CHANNEL}"
TEAM = ["U_ACME_ADA", "U_ACME_BEN", "U_ACME_CHLOE"]


async def private_team(app, slack_client):
    channel = app.state.ctx.workspace.config.slack.channels[0]
    channel.team_members_only = True
    async def info(**kwargs):
        await slack_client._record("conversations_info", **kwargs)
        return {"channel": {"is_private": True}}
    slack_client.conversations_info = info
    async def members(**kwargs):
        await slack_client._record("conversations_members", **kwargs)
        return {"members": TEAM}
    slack_client.conversations_members = members
    return channel


async def expire(ctx):
    async with ctx.pool.connection() as conn:
        state = await kv.get(conn, KEY)
        state["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        await kv.set(conn, KEY, state)


async def test_known_members_all_pages_and_cache(app, slack_client):
    await private_team(app, slack_client)
    pages = []
    async def members(**kwargs):
        pages.append(kwargs.get("cursor"))
        if kwargs.get("cursor"):
            return {"members": ["U_ACME_BEN", "U_ACME_CHLOE", "U_ACME_ILLO"]}
        return {"members": ["U_ACME_ADA"], "response_metadata": {"next_cursor": "second"}}
    slack_client.conversations_members = members
    assert await channel_private(app.state.ctx, CHANNEL) is False
    assert await channel_private(app.state.ctx, CHANNEL) is False
    assert pages == [None, "second"]
    async with app.state.ctx.pool.connection() as conn:
        sensor = await one(conn, "SELECT * FROM sensors WHERE key=%s", (KEY,))
    assert sensor["last_ok_at"] and not sensor["last_error_at"]


@pytest.mark.parametrize("members", [["U_UNKNOWN"], ["U_INACTIVE"]])
async def test_unknown_or_inactive_human_is_private_without_identity_in_sensor(app, slack_client, members):
    await private_team(app, slack_client)
    if members == ["U_INACTIVE"]:
        async with app.state.ctx.pool.connection() as conn:
            await conn.execute("UPDATE people SET active=false,slack_id='U_INACTIVE' WHERE key='ben'")
    async def list_members(**kwargs):
        return {"members": members}
    slack_client.conversations_members = list_members
    assert await channel_private(app.state.ctx, CHANNEL)
    async with app.state.ctx.pool.connection() as conn:
        sensor = await one(conn, "SELECT * FROM sensors WHERE key=%s", (KEY,))
    assert sensor["last_error_at"]
    assert sensor["detail"] == "The members of #team are not exactly the people in people.yaml. Illo treats it as private."
    assert members[0] not in sensor["detail"]


async def test_bot_members_are_accepted_and_verdict_is_cached(app, slack_client):
    await private_team(app, slack_client)
    slack_client.users["U_MONITOR"] = {"is_bot": True}
    async def members(**kwargs):
        return {"members": [*TEAM, "U_MONITOR"]}
    slack_client.conversations_members = members
    assert not await channel_private(app.state.ctx, CHANNEL)
    await expire(app.state.ctx)
    assert not await channel_private(app.state.ctx, CHANNEL)
    assert len([c for c in slack_client.calls if c["method"] == "users_info"]) == 1
    async with app.state.ctx.pool.connection() as conn:
        assert (await kv.get(conn, KEY))["bots"] == {"U_MONITOR": True}


@pytest.mark.parametrize("method", ["conversations_info", "conversations_members", "users_info", "auth_test"])
async def test_slack_failure_gives_no_verdict_and_keeps_the_index(app, slack_client, method):
    await private_team(app, slack_client)
    async def members(**kwargs):
        return {"members": ["U_UNKNOWN"]}
    slack_client.conversations_members = members
    async def fail(**kwargs):
        raise RuntimeError("Slack failed")
    setattr(slack_client, method, fail)
    async with app.state.ctx.pool.connection() as conn:
        await items.upsert(conn, source_ref=f"slack:{CHANNEL}:1.0", kind="slack", title="#team",
                           body="Nebula", links=[], visibility="team")
    with pytest.raises(ValueError):
        await channel_private(app.state.ctx, CHANNEL)
    async with app.state.ctx.pool.connection() as conn:
        sensor = await one(conn, "SELECT * FROM sensors WHERE key=%s", (KEY,))
        assert sensor["last_error_at"] and sensor["detail"] == "Slack could not verify channel privacy"
        assert await kv.get(conn, KEY) is None
        assert await one(conn, "SELECT 1 FROM items WHERE source_ref=%s", (f"slack:{CHANNEL}:1.0",))


async def test_active_person_outside_the_channel_makes_it_private(app, slack_client):
    await private_team(app, slack_client)
    async def members(**kwargs):
        return {"members": ["U_ACME_ADA", "U_ACME_BEN"]}
    slack_client.conversations_members = members
    assert await channel_private(app.state.ctx, CHANNEL)


async def test_configured_private_wins_and_default_keeps_slack_private(app, slack_client):
    channel = await private_team(app, slack_client)
    channel.visibility = "private"
    assert await channel_private(app.state.ctx, CHANNEL)
    assert not slack_client.calls
    channel.visibility = "team"
    channel.team_members_only = False
    assert await channel_private(app.state.ctx, CHANNEL)
    assert not any(c["method"] == "conversations_members" for c in slack_client.calls)
    assert not SlackChannel(name="team", id=CHANNEL).team_members_only


async def test_intake_search_read_and_post_follow_team_visibility(app, slack_client, fake_brain, rpc, rpc_token):
    await private_team(app, slack_client)
    intake = SlackIntake(app.state.ctx, "U_ACME_ILLO")
    result = await intake.receive({"event": {"type": "message", "channel": CHANNEL, "ts": "1700000000.1",
                                            "user": "U_ACME_ADA", "text": "<@U_ACME_ILLO> Nebula release plan"}})
    assert result["decision"] == "run" and "error" not in result
    hits = await search.search(app.state.ctx, "team", "Nebula release", kinds=["slack"])
    assert len(hits) == 1 and hits[0].source_ref == f"slack:{CHANNEL}:1700000000.1"
    _, token = rpc_token
    result = await call(rpc, token, "slack_read_thread", {"channel": CHANNEL, "thread_ts": "1700000000.1"})
    assert result["decision"] == "allow" and result["result"]["visibility"] == "team"
    result = await call(rpc, token, "slack_post_channel", {"channel": CHANNEL, "text": "Release ready"})
    assert result["decision"] == "allow"


async def test_outsider_change_hides_index_on_next_check(app, slack_client, rpc, rpc_token):
    await private_team(app, slack_client)
    assert not await channel_private(app.state.ctx, CHANNEL)
    async with app.state.ctx.pool.connection() as conn:
        await items.upsert(conn, source_ref=f"slack:{CHANNEL}:1.1", kind="slack", title="Nebula", body="Release plan")
    async def members(**kwargs):
        return {"members": [*TEAM, "U_OUTSIDER"]}
    slack_client.conversations_members = members
    await expire(app.state.ctx)
    assert not await search.search(app.state.ctx, "team", "Nebula", kinds=["slack"])
    assert await search.get(app.state.ctx, "team", f"slack:{CHANNEL}:1.1") is None
    async with app.state.ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT * FROM items WHERE kind='slack'")
    _, token = rpc_token
    result = await call(rpc, token, "slack_post_channel", {"channel": CHANNEL, "text": "Team update"})
    assert result["decision"] == "deny"


async def test_team_brief_and_replies_use_private_team_channel(app, slack_client, fake_brain, monkeypatch):
    from tests.slack.test_team_brief_threads import prepare

    await private_team(app, slack_client)
    ctx, intake, event, _, _ = await prepare(app, slack_client, monkeypatch)
    result = await intake.receive({"event": event})
    assert result["decision"] == "run" and "error" not in result
    assert '<outside_data source="thread">' in fake_brain.requests[-1].user_message


async def test_team_brief_does_not_post_after_outsider_joins(app, slack_client):
    from illo.routines.team_digest import send_team_due
    from tests.asks.test_digest import NOW
    from tests.asks.test_team_digest import configure, seed_team

    configure(app.state.ctx)
    await private_team(app, slack_client)
    await seed_team(app.state.ctx)
    async def members(**kwargs):
        return {"members": ["U_OUTSIDER"]}
    slack_client.conversations_members = members
    await send_team_due(app.state.ctx, NOW + timedelta(hours=5))
    assert not any(c["method"] == "chat_postMessage" for c in slack_client.calls)


async def test_post_refusal_keeps_the_sensor_and_hides_the_index(app, slack_client, rpc, rpc_token):
    await private_team(app, slack_client)
    async with app.state.ctx.pool.connection() as conn:
        await items.upsert(conn, source_ref=f"slack:{CHANNEL}:1.1", kind="slack", title="Private plan")
    async def members(**kwargs):
        return {"members": ["U_OUTSIDER"]}
    slack_client.conversations_members = members
    _, token = rpc_token
    result = await call(rpc, token, "slack_post_channel", {"channel": CHANNEL, "text": "Team update"})
    assert result["decision"] == "deny"
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT * FROM sensors WHERE key=%s", (KEY,)))["last_error_at"]
        assert not await one(conn, "SELECT * FROM items WHERE kind='slack'")


async def test_config_change_does_not_reuse_an_old_privacy_policy(app, slack_client):
    channel = await private_team(app, slack_client)
    channel.team_members_only = False
    assert await channel_private(app.state.ctx, CHANNEL)
    channel.team_members_only = True
    assert not await channel_private(app.state.ctx, CHANNEL)
    channel.team_members_only = False
    assert await channel_private(app.state.ctx, CHANNEL)
