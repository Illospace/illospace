"""An answer to a person's request comes back to that person, once."""

import asyncio

import pytest

from illo.asks import relay
from illo.db import one
from illo.ledger import asks, people


async def _ask(ctx, *, requester="ada", owner="ben"):
    async with ctx.pool.connection() as conn:
        asker, teammate = await people.get(conn, requester), await people.get(conn, owner)
        row = await asks.create(
            conn,
            owner_person_id=teammate["id"],
            requester_person_id=asker["id"],
            question="Can the new runner take the GPU jobs this week?",
            options=[{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
        )
        await asks.answer(
            conn, row["id"], person_id=teammate["id"], option="yes", text="From Wednesday"
        )
        return row["id"]


async def test_answer_goes_back_to_the_requester_once(app, slack_client):
    ctx = app.state.ctx
    ask_id = await _ask(ctx)
    await relay.tick(ctx)
    await relay.tick(ctx)
    posts = [c for c in slack_client.calls if c["method"] == "chat_postMessage"]
    assert len(posts) == 1
    assert "Ben answered" in posts[0]["text"] and "Yes. From Wednesday" in posts[0]["text"]
    assert "GPU jobs" in posts[0]["text"]
    opened = [c for c in slack_client.calls if c["method"] == "conversations_open"]
    assert opened[0]["users"] == "U_ACME_ADA"
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT delivery_state FROM asks WHERE id=%s", (ask_id,))
    assert row["delivery_state"] == "acked"


async def test_own_ask_and_missing_slack_send_nothing(app, slack_client):
    ctx = app.state.ctx
    await _ask(ctx, requester="ben", owner="ben")
    await relay.tick(ctx)
    assert not slack_client.calls
    ask_id = await _ask(ctx)
    ctx.slack_client = None
    await relay.tick(ctx)
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT delivery_state FROM asks WHERE id=%s", (ask_id,))
    assert row["delivery_state"] == "pending"


async def test_cancel_after_accepted_post_is_not_retried(app, slack_client):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    ask_id = await _ask(ctx)
    original = slack_client.chat_postMessage

    async def accepted_then_cancelled(**kwargs):
        await original(**kwargs)
        raise asyncio.CancelledError()

    slack_client.chat_postMessage = accepted_then_cancelled
    with pytest.raises(asyncio.CancelledError):
        await relay.tick(ctx)
    slack_client.chat_postMessage = original
    await relay.tick(ctx)
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM asks WHERE id=%s", (ask_id,))
        assert row["delivery_state"] == "delivered"
        assert row["delivery_attempts"] == 1
        await conn.execute(
            "UPDATE asks SET delivered_at=now()-interval '3 minutes' WHERE id=%s", (ask_id,)
        )
    await relay.tick(ctx)
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM asks WHERE id=%s", (ask_id,))
    assert row["delivery_state"] == "failed"
    assert len([c for c in slack_client.calls if c["method"] == "chat_postMessage"]) == 1


async def test_later_batch_failure_keeps_earlier_ack(app, slack_client):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    first, second = await _ask(ctx), await _ask(ctx)
    original = slack_client.chat_postMessage
    count = 0

    async def fail_second(**kwargs):
        nonlocal count
        await original(**kwargs)
        count += 1
        if count == 2:
            raise RuntimeError("The response was lost")
        return {"ts": "accepted"}

    slack_client.chat_postMessage = fail_second
    await relay.tick(ctx)
    slack_client.chat_postMessage = original
    await relay.tick(ctx)
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT * FROM asks WHERE id=%s", (first,)))["delivery_state"] == "acked"
        assert (await one(conn, "SELECT * FROM asks WHERE id=%s", (second,)))["delivery_state"] == "delivered"
    assert count == 2
    assert len([c for c in slack_client.calls if c["method"] == "chat_postMessage"]) == 2


async def test_network_call_holds_no_ask_locks(app, slack_client):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    ids = [await _ask(ctx), await _ask(ctx)]
    original = slack_client.chat_postMessage

    async def check_locks(**kwargs):
        async with ctx.pool.connection() as conn:
            await conn.execute("SET LOCAL lock_timeout='100ms'")
            rows = await (await conn.execute(
                "SELECT id FROM asks WHERE id=ANY(%s) FOR UPDATE", (ids,)
            )).fetchall()
            assert len(rows) == 2
        return await original(**kwargs)

    slack_client.chat_postMessage = check_locks
    await relay.tick(ctx)
    assert len([c for c in slack_client.calls if c["method"] == "chat_postMessage"]) == 2
