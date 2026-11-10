"""An answer to a person's request comes back to that person, once."""

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
