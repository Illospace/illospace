"""Rule 5: only the requester can approve a draft in its original DM."""

import pytest

from illo.asks import drafts
from illo.db import one
from illo.ledger import drafts as ledger
from illo.ledger import people, runs


@pytest.mark.parametrize("invalid", ["person", "channel", "message"])
async def test_rule_05_draft_card_ownership(app, invalid):
    ctx = app.state.ctx
    sent = []

    async def perform(ctx, row):
        sent.append(row["id"])

    ctx.draft_performers["example"] = perform
    async with ctx.pool.connection() as conn:
        ada, ben = await people.get(conn, "ada"), await people.get(conn, "ben")
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile="person_dm",
            runtime="fake",
            model="fake",
        )
        row = await ledger.create(
            conn,
            run_id=run["id"],
            approver_person_id=ada["id"],
            kind="example",
            destination="outside:contact@example.com",
            content={"text": "Acme"},
        )
    await drafts.tick(ctx)
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM drafts WHERE id=%s", (row["id"],))
    action = {
        "action_id": f"draft:{row['id']}:approve",
        "channel": row["slack_channel"],
        "message_ts": row["slack_ts"],
    }
    if invalid == "channel":
        action["channel"] = "C_ACME"
    if invalid == "message":
        action["message_ts"] = "wrong"
    await drafts.handle(ctx, ben if invalid == "person" else ada, action)
    assert not sent
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT state FROM drafts WHERE id=%s", (row["id"],)))[
            "state"
        ] == "pending"
