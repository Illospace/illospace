"""Retry only briefs that started during the delivery window."""

from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb

from illo.asks.service import message_id
from illo.db import one
from illo.ledger import kv
from illo.ledger.digest_snapshot import TEAM_STATE_KEY
from illo.routines.digest import send_due
from illo.routines.team_digest import send_team_due
from tests.asks.test_digest import NOW, only_ada, posts
from tests.asks.test_team_digest import configure, seed_team


@pytest.mark.parametrize("team", [False, True], ids=["personal", "team"])
@pytest.mark.parametrize("expires", [False, True], ids=["retry", "expire"])
async def test_late_brief_requires_a_stored_event(
    app, fake_brain, slack_client, monkeypatch, team, expires,
):
    ctx = app.state.ctx
    config = configure(ctx)
    config.timezone = "UTC"
    config.digest.schedule = "0 23 * * 1-5"
    config.digest.team.schedule = config.digest.schedule
    due = NOW.replace(hour=23)
    await only_ada(ctx)

    if team:
        await seed_team(ctx)
        send, channel, state_key = send_team_due, "team_digest", TEAM_STATE_KEY
        external_id = f"digest-team:{due.isoformat()}"
        dedupe_id = message_id("routine", f"{external_id}:0")
        snapshot = {"team_snapshot": {"finished": [], "stuck": [], "waiting": [], "in_work": {}}}
    else:
        config.digest.team = None
        send, channel, state_key = send_due, "digest", "digest:ada"
        async with ctx.pool.connection() as conn:
            person = await one(conn, "SELECT id FROM people WHERE key='ada'")
        external_id = f"{person['id']}:{due.isoformat()}"
        dedupe_id = message_id("digest", external_id)
        snapshot = {"digest_snapshot": {}}

    late = due + timedelta(minutes=3)
    await send(ctx, late)
    assert not fake_brain.requests and not posts(slack_client)
    async with ctx.pool.connection() as conn:
        assert await one(conn, "SELECT 1 FROM events WHERE channel=%s", (channel,)) is None
        assert await one(conn, "SELECT 1 FROM routine_state WHERE key=%s", (state_key,)) is None
        await conn.execute(
            """INSERT INTO events (channel,external_id,kind,status,payload)
            VALUES (%s,%s,'scheduled','handled',%s)""",
            (channel, external_id, Jsonb({"payload": snapshot, "final_answer": "The example brief."})),
        )

    attempts = []
    post = slack_client.chat_postMessage

    async def fail_once(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError("Example send failure")
        return await post(**kwargs)

    monkeypatch.setattr(slack_client, "chat_postMessage", fail_once)
    with pytest.raises(RuntimeError, match="Example send failure"):
        await send(ctx, late)
    if expires:
        expired = due + timedelta(hours=2, seconds=1)
        await send(ctx, expired)
        await send(ctx, expired + timedelta(seconds=30))
        assert len(attempts) == 1 and not posts(slack_client)
        async with ctx.pool.connection() as conn:
            state = await one(
                conn, "SELECT last_run_at,last_status FROM routine_state WHERE key=%s", (state_key,)
            )
            assert state == {"last_run_at": due, "last_status": "skipped"}
            assert await kv.get(conn, f"brief:sent:{state_key}") is None
    else:
        retry = late + timedelta(seconds=30)
        await send(ctx, retry)
        await send(ctx, retry)
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        assert attempts[0]["client_msg_id"] == dedupe_id
        assert posts(slack_client)[0]["text"].startswith("The example brief.")
        assert len(posts(slack_client)) == 1
    assert not fake_brain.requests


async def test_team_send_failure_still_sends_the_personal_brief(app, slack_client, monkeypatch):
    ctx = app.state.ctx
    config = configure(ctx)
    config.timezone = "UTC"
    team_channel = config.slack.channels[0].id
    await seed_team(ctx)
    await only_ada(ctx)
    post = slack_client.chat_postMessage
    failure = RuntimeError("Example team send failure")

    async def fail_team(**kwargs):
        if kwargs["channel"] == team_channel:
            raise failure
        return await post(**kwargs)

    monkeypatch.setattr(slack_client, "chat_postMessage", fail_team)
    with pytest.raises(RuntimeError) as error:
        await send_due(ctx, NOW)
    assert error.value is failure
    assert [call["channel"] for call in posts(slack_client)] == ["D_EXAMPLE"]
