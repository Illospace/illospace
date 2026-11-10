"""Slack tools run through the real authority checks and fake Web API."""

from illo.db import one
from illo.ledger import people, runs
from illo.tools.registry import ToolContext


async def call(rpc, token, name, args):
    response = await rpc(token, "tools/call", {"name": name, "arguments": args})
    return response.json()["result"]["structuredContent"]


async def test_send_dm_and_only_configured_channel(app, rpc, rpc_token, slack_client):
    _, token = rpc_token
    result = await call(
        rpc, token, "slack_send_dm", {"person": "ben", "text": "The lane is ready."}
    )
    assert result["decision"] == "allow"
    assert slack_client.calls[0] == {"method": "conversations_open", "users": "U_ACME_BEN"}
    assert result["result"]["channel"] == "D_EXAMPLE"
    result = await call(rpc, token, "slack_post_channel", {"channel": "C_OTHER", "text": "Hello"})
    assert result["decision"] == "deny"
    assert len(slack_client.calls) == 2
    result = await call(
        rpc,
        token,
        "slack_post_channel",
        {"channel": "C_ACME_TEAM", "text": "The lane is ready.", "thread_ts": "1700000000.000001"},
    )
    assert result["decision"] == "allow"
    assert slack_client.calls[-1]["thread_ts"] == "1700000000.000001"
    assert (await call(rpc, token, "slack_send_dm", {"person": "unknown", "text": "Hello"}))[
        "decision"
    ] == "deny"


async def test_read_only_configured_channel_or_current_dm(app, rpc, rpc_token, slack_client):
    _, token = rpc_token
    for args, decision in [
        ({"channel": "C_ACME_TEAM", "thread_ts": "100.1"}, "allow"),
        ({"channel": "D_OTHER_PERSON", "thread_ts": "100.1"}, "deny"),
        ({"channel": "C_UNKNOWN", "thread_ts": "100.1"}, "deny"),
    ]:
        assert (await call(rpc, token, "slack_read_thread", args))["decision"] == decision
    assert len(slack_client.calls) == 2


async def test_current_dm_thread_access(intake, message, fake_brain, slack_client):
    fake_brain.script = [
        {
            "name": "slack_read_thread",
            "arguments": {"channel": "D_ACME_ADA", "thread_ts": "1700000000.000001"},
        }
    ]
    await intake.receive(message())
    assert fake_brain.results[0]["structuredContent"]["decision"] == "allow"
    fake_brain.script[0]["arguments"]["thread_ts"] = "unrelated-thread"
    await intake.receive(message(ts="1700000001.000001"))
    assert fake_brain.results[-1]["structuredContent"]["decision"] == "deny"


async def test_personal_write_draft_then_perform(app, rpc, rpc_token, slack_client):
    run, token = rpc_token
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (run["id"],))
    result = await call(
        rpc, token, "slack_post_channel", {"channel": "C_ACME_TEAM", "text": "Private data"}
    )
    assert result["decision"] == "draft"
    assert not slack_client.calls
    async with app.state.ctx.pool.connection() as conn:
        draft = await one(conn, "SELECT * FROM drafts WHERE id=%s", (result["result"]["draft_id"],))
        assert draft["approver_person_id"] == run["person_id"]
        await conn.execute("UPDATE drafts SET state='approved' WHERE id=%s", (draft["id"],))
        draft["state"] = "approved"
    await app.state.ctx.draft_performers["slack_post_channel"](app.state.ctx, draft)
    assert slack_client.calls[-1]["text"] == "Private data"
    result = await call(rpc, token, "slack_send_dm", {"person": "ada", "text": "Your private data"})
    assert result["decision"] == "allow"
    result = await call(rpc, token, "slack_send_dm", {"person": "ben", "text": "Private data"})
    assert result["decision"] == "draft"


async def test_profiles_hide_slack_writes_and_all_slack_from_taint(app, rpc):
    for profile in ["team_channel", "tainted", "outsider", "routine", "agent_request"]:
        async with app.state.ctx.pool.connection() as conn:
            person = await people.get(conn, "ada")
            run = await runs.create(
                conn,
                principal="outsider" if profile == "outsider" else "person:ada",
                person_id=None if profile == "outsider" else person["id"],
                profile="tainted" if profile == "outsider" else profile,
                tainted=profile in {"tainted", "outsider"},
                runtime="fake",
                model="fake",
            )
            token = await runs.mint_token(conn, run["id"], 60)
        names = {t["name"] for t in (await rpc(token, "tools/list")).json()["result"]["tools"]}
        assert "slack_post_channel" not in names
        assert ("slack_send_dm" in names) == (profile == "routine")
        assert ("slack_read_thread" in names) == (profile == "team_channel")


async def test_inactive_dm_recipient_is_denied(app, rpc, rpc_token):
    _, token = rpc_token
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET active=false WHERE key='ben'")
    assert (await call(rpc, token, "slack_send_dm", {"person": "ben", "text": "Hello"}))[
        "decision"
    ] == "deny"


async def test_tools_without_client_fail_without_external_calls(app):
    ctx = app.state.ctx
    ctx.slack_client = None
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, "ada")
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=person["id"],
            profile="person_dm",
            runtime="fake",
            model="fake",
        )
        result = await ctx.registry.call(
            ToolContext(conn, run, ctx), "slack_send_dm", {"person": "ben", "text": "Hello"}
        )
    assert result["decision"] == "deny"
