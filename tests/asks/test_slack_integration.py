"""The real Slack intake reaches the ask and draft handlers through shared hooks."""

from illo.asks import drafts, service
from illo.channels.slack.intake import SlackIntake
from illo.db import one
from illo.ledger import ask_workflow as ledger


def click(row, action_id):
    return {
        "type": "block_actions",
        "user": {"id": "U_ACME_ADA"},
        "channel": {"id": row["slack_channel"]},
        "message": {"ts": row["slack_ts"]},
        "actions": [{"action_id": action_id, "action_ts": "101.001"}],
    }


async def test_slack_other_button_and_thread_answer(app, seed, fake_brain):
    ctx = app.state.ctx
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    row = await seed()
    await service.tick(ctx)
    async with ctx.pool.connection() as conn:
        row = await ledger.lock_ask(conn, row["id"])
    await intake.receive(click(row, f"ask:{row['id']}:other"))
    result = await intake.receive(
        {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": row["slack_channel"],
                "channel_type": "im",
                "user": "U_ACME_ADA",
                "ts": "1700000000.001",
                "thread_ts": row["slack_ts"],
                "text": "Use the second Acme plan.",
            },
        }
    )
    assert result["decision"] == "claimed" and not fake_brain.requests
    async with ctx.pool.connection() as conn:
        row = await ledger.lock_ask(conn, row["id"])
        assert row["answer_text"] == "Use the second Acme plan."


async def test_slack_registered_draft_performer_sends_once(app, rpc, rpc_token, slack_client):
    ctx = app.state.ctx
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    run, token = rpc_token
    channel = ctx.workspace.config.slack.channels[0].id
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (run["id"],))
    result = await rpc(
        token,
        "tools/call",
        {
            "name": "slack_post_channel",
            "arguments": {"channel": channel, "text": "Acme approved update"},
        },
    )
    assert result.json()["result"]["structuredContent"]["decision"] == "draft"
    await drafts.tick(ctx)
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM drafts")
    await intake.receive(click(row, f"draft:{row['id']}:approve"))
    await intake.receive(click(row, f"draft:{row['id']}:approve"))
    posts = [
        c
        for c in slack_client.calls
        if c["method"] == "chat_postMessage" and c.get("channel") == channel
    ]
    assert len(posts) == 1 and posts[0]["text"] == "Acme approved update"
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT state FROM drafts"))["state"] == "sent"
