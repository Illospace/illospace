"""Draft actions commit approval before an external send and never repeat it."""

import asyncio

from illo.asks import drafts
from illo.db import one
from illo.ledger import people


async def read(ctx, draft_id):
    async with ctx.pool.connection() as conn:
        return await one(conn, "SELECT * FROM drafts WHERE id=%s", (draft_id,))


def click(row, verb="approve", **changes):
    return {
        "action_id": f"draft:{row['id']}:{verb}",
        "channel": row["slack_channel"],
        "message_ts": row["slack_ts"],
        **changes,
    }


async def setup_draft(ctx, seed):
    row = await seed(draft=True)
    await drafts.tick(ctx)
    row = await read(ctx, row["id"])
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, "ada")
    return row, person


async def test_approve_performs_once_after_concurrent_clicks(app, seed):
    ctx = app.state.ctx
    sent = []

    async def perform(ctx, row):
        sent.append(row["id"])

    ctx.draft_performers["example"] = perform
    row, person = await setup_draft(ctx, seed)
    await asyncio.gather(
        drafts.handle(ctx, person, click(row)), drafts.handle(ctx, person, click(row))
    )
    await drafts.tick(ctx)
    assert sent == [row["id"]]
    assert (await read(ctx, row["id"]))["state"] == "sent"


async def test_reject_performs_nothing(app, seed, slack_client):
    ctx = app.state.ctx

    async def perform(ctx, row):
        raise AssertionError("A rejected action must not run")

    ctx.draft_performers["example"] = perform
    row, person = await setup_draft(ctx, seed)
    await drafts.handle(ctx, person, click(row, "reject"))
    assert (await read(ctx, row["id"]))["state"] == "rejected"
    assert "Not sent" in str(slack_client.calls[-1])


async def test_expired_draft_cannot_send(app, seed, slack_client):
    ctx = app.state.ctx
    row, person = await setup_draft(ctx, seed)
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE drafts SET created_at=now()-interval '25 hours' WHERE id=%s", (row["id"],)
        )
    await drafts.handle(ctx, person, click(row))
    assert (await read(ctx, row["id"]))["state"] == "expired"
    assert "actions" not in str(slack_client.calls[-1]["blocks"])


async def test_uncertain_send_has_no_automatic_retry(app, seed):
    ctx = app.state.ctx
    calls = []

    async def perform(ctx, row):
        calls.append(row["id"])
        raise OSError("The remote reply is missing")

    ctx.draft_performers["example"] = perform
    row, person = await setup_draft(ctx, seed)
    await drafts.handle(ctx, person, click(row))
    await drafts.handle(ctx, person, click(row))
    await drafts.tick(ctx)
    after = await read(ctx, row["id"])
    assert after["state"] == "approved" and after["send_error"]
    assert calls == [row["id"]]


async def test_missing_performer_keeps_pending(app, seed):
    ctx = app.state.ctx
    row, person = await setup_draft(ctx, seed)
    await drafts.handle(ctx, person, click(row))
    assert (await read(ctx, row["id"]))["state"] == "pending"


async def test_registry_draft_performs_exact_stored_tool(app, seed):
    from pydantic import Field

    from illo.tools.registry import Arguments, Tool

    class Args(Arguments):
        text: str = Field(min_length=1)

    ctx = app.state.ctx
    calls = []

    async def send(tool_ctx, args):
        calls.append(args["text"])

    ctx.registry.add(
        Tool(
            "example_send",
            "example",
            "send_outside",
            lambda a: "outside:contact@example.com",
            False,
            send,
            arguments=Args,
            profiles=frozenset({"person_dm"}),
        )
    )
    row = await seed(
        draft=True,
        kind="example_send",
        content={"tool": "example_send", "arguments": {"text": "Acme update"}},
    )
    await drafts.tick(ctx)
    row = await read(ctx, row["id"])
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, "ada")
    await drafts.handle(ctx, person, click(row))
    assert calls == ["Acme update"]
    assert (await read(ctx, row["id"]))["state"] == "sent"
