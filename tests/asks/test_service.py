"""Ask routing, card recovery, expiry, and decision search use real Postgres."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from illo.asks import actions, service
from illo.db import one
from illo.ledger import ask_workflow as ledger
from illo.ledger import asks, items, people, rules
from illo.routines import reminders, stale


async def read(ctx, ask_id):
    async with ctx.pool.connection() as conn:
        return await ledger.lock_ask(conn, ask_id)


def click(row, operation="opt:yes", **changes):
    return {
        "action_id": f"ask:{row['id']}:{operation}",
        "channel": row["slack_channel"],
        "message_ts": row["slack_ts"],
        **changes,
    }


async def member(ctx, key="ada"):
    async with ctx.pool.connection() as conn:
        return await people.get(conn, key)


@pytest.mark.parametrize(
    "case,expected",
    [("explicit", "ben"), ("area", "ben"), ("lane", "ada"), ("token", "ada"), ("default", "ben")],
)
async def test_routing_order(app, seed, case, expected):
    ctx = app.state.ctx
    row = await seed(
        owner="ben" if case == "explicit" else None,
        area="backend" if case in {"explicit", "area"} else None,
    )
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET areas=ARRAY['backend'] WHERE key='ben'")
        if case in {"token", "default"}:
            await conn.execute("UPDATE asks SET lane_id=NULL WHERE id=%s", (row["id"],))
        if case == "default":
            await conn.execute("UPDATE asks SET requester_token_id=NULL WHERE id=%s", (row["id"],))
        row = await ledger.lock_ask(conn, row["id"])
        routed = await ledger.route(conn, row, "ben")
        person = await people.get(conn, expected)
        assert routed["owner_person_id"] == person["id"]


async def test_card_once_after_restart_and_parallel_ticks(app, seed, slack_client):
    ctx = app.state.ctx
    row = await seed()
    await asyncio.gather(service.tick(ctx), service.tick(ctx))
    before = await read(ctx, row["id"])
    await service.tick(ctx)
    after = await read(ctx, row["id"])
    posts = [c for c in slack_client.calls if c["method"] == "chat_postMessage"]
    assert len(posts) == 1
    assert before["slack_ts"] == after["slack_ts"]
    assert posts[0]["client_msg_id"] == service.message_id("ask", row["id"])
    assert (await _stats(ctx))["cards_sent"] == 1


async def _stats(ctx):
    async with ctx.pool.connection() as conn:
        return await one(conn, "SELECT * FROM ask_daily_stats")


async def test_missing_slack_recovers(app, seed, slack_client):
    ctx = app.state.ctx
    row = await seed()
    ctx.slack_client = None
    await service.tick(ctx)
    assert not (await read(ctx, row["id"]))["slack_ts"]
    async with ctx.pool.connection() as conn:
        sensor = await one(conn, "SELECT * FROM sensors WHERE key='asks:slack'")
        assert sensor["last_error_at"]
    ctx.slack_client = slack_client
    await service.tick(ctx)
    assert (await read(ctx, row["id"]))["slack_ts"]


async def test_tap_answers_edits_and_writes_searchable_decision(app, seed, slack_client):
    ctx = app.state.ctx
    row = await seed(question="Approve the Acme export?")
    await service.tick(ctx)
    row = await read(ctx, row["id"])
    await actions.handle(ctx, await member(ctx), click(row))
    answered = await read(ctx, row["id"])
    assert answered["state"] == "answered" and answered["delivery_state"] == "pending"
    assert answered["answer_option"] == "yes"
    edited = [c for c in slack_client.calls if c["method"] == "chat_update"][-1]
    assert all(b["type"] != "actions" for b in edited["blocks"])
    async with ctx.pool.connection() as conn:
        found = await items.search(conn, "Acme export")
        assert found[0]["source_ref"] == f"ask:{row['id']}"
        item = await items.get(conn, found[0]["source_ref"])
        assert (
            "Acme example" in item["body"]
            and "Continue" in item["body"]
            and "Who: Ada" in item["body"]
        )
        await asks.pull(conn, row["requester_token_id"])
        await asks.ack(conn, row["id"], row["requester_token_id"])
    await service.tick(ctx)
    assert "the agent has it" in str(slack_client.calls[-1])


@pytest.mark.parametrize("invalid", ["other_person", "channel", "message", "closed", "duplicate"])
async def test_invalid_or_second_tap_has_no_effect(app, seed, invalid):
    ctx = app.state.ctx
    row = await seed()
    await service.tick(ctx)
    row = await read(ctx, row["id"])
    person = await member(ctx, "ben" if invalid == "other_person" else "ada")
    action = click(row)
    if invalid == "channel":
        action["channel"] = "C_ACME"
    if invalid == "message":
        action["message_ts"] = "wrong"
    if invalid in {"closed", "duplicate"}:
        async with ctx.pool.connection() as conn:
            if invalid == "closed":
                await asks.cancel(conn, row["id"])
            else:
                await asks.answer(conn, row["id"], person_id=person["id"], option="no")
    await actions.handle(ctx, person, action)
    after = await read(ctx, row["id"])
    assert after["answer_option"] == ("no" if invalid == "duplicate" else None)


async def test_other_claims_only_owner_next_thread_reply(app, seed, replay, fake_brain):
    ctx = app.state.ctx
    row = await seed()
    await service.tick(ctx)
    row = await read(ctx, row["id"])
    await actions.handle(ctx, await member(ctx), click(row, "other"))
    result = await replay(
        text="Use the second plan.",
        payload={"channel_id": row["slack_channel"], "thread_ts": row["slack_ts"]},
    )
    assert result["decision"] == "claimed"
    assert (await read(ctx, row["id"]))["answer_text"] == "Use the second plan."
    assert not fake_brain.requests
    result = await replay(
        text="Another answer",
        payload={"channel_id": row["slack_channel"], "thread_ts": row["slack_ts"]},
    )
    assert result["decision"] == "claimed" and "closed" in result["text"]


async def test_always_rule_has_exact_normalized_question_and_owner(app, seed):
    ctx = app.state.ctx
    row = await seed(question="Allow  the [safe]* plan?")
    await service.tick(ctx)
    row = await read(ctx, row["id"])
    await actions.handle(ctx, await member(ctx), click(row, "always:yes"))
    async with ctx.pool.connection() as conn:
        saved = (await rules.list_rules(conn, principal="person:ada"))[0]
        assert saved["origin"] == "card" and saved["answer_option_label"] == "Continue"
        row = {**row, "requester_person_id": await rules.requester_person_id(conn, row)}
        assert rules.matches(
            saved, {**row, "question": "ALLOW the [safe]* plan?"}, {"source": "t3", "repo": None}
        )
        assert not rules.matches(
            saved, {**row, "question": "Allow the sssss planZ"}, {"source": "t3", "repo": None}
        )
        assert await rules.remove(conn, saved["id"], saved["owner_person_id"])


async def test_exactly_one_reminder_and_expiry(app, seed, slack_client):
    ctx = app.state.ctx
    now = datetime.now(UTC)
    row = await seed(due_at=now - timedelta(minutes=1), expires_at=now + timedelta(hours=1))
    await service.tick(ctx)
    await asyncio.gather(reminders.remind(ctx), reminders.remind(ctx))
    await reminders.remind(ctx)
    assert len([c for c in slack_client.calls if c.get("text") == reminders.REMINDER]) == 1
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE asks SET expires_at=now()-interval '1 second' WHERE id=%s", (row["id"],)
        )
    await service.tick(ctx)
    assert (await read(ctx, row["id"]))["state"] == "expired"
    assert "No answer is needed" in str(slack_client.calls[-1])
    assert "actions" not in str(slack_client.calls[-1]["blocks"])


@pytest.mark.parametrize("state", ["done", "failed"])
async def test_closed_lane_cancels_before_card_or_answer(app, seed, state):
    ctx = app.state.ctx
    row = await seed(source_ref="source-question")
    await service.tick(ctx)
    row = await read(ctx, row["id"])
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE lanes SET state=%s,end_summary='Acme export is ready.',closed_at=now() WHERE id=%s",
            (state, row["lane_id"]),
        )
    await actions.handle(ctx, await member(ctx), click(row))
    await stale.reconcile(ctx)
    after = await read(ctx, row["id"])
    assert after["state"] == "cancelled" and after["answer_option"] is None
    async with ctx.pool.connection() as conn:
        assert await items.get(conn, f"lane:{row['lane_id']}:end")


async def test_private_decision_keeps_lane_visibility(app, seed):
    ctx = app.state.ctx
    row = await seed(lane={"visibility": "private"})
    async with ctx.pool.connection() as conn:
        answered = await asks.answer(
            conn, row["id"], person_id=row["owner_person_id"], option="yes"
        )
        await ledger.decision(conn, answered)
        assert await items.get(conn, f"ask:{row['id']}", principal="person:ada")
        assert not await items.get(conn, f"ask:{row['id']}", principal="person:ben")


@pytest.mark.parametrize("by_expiry", [False, True])
async def test_unacked_delivery_fails_after_limit(app, seed, by_expiry):
    ctx = app.state.ctx
    row = await seed()
    async with ctx.pool.connection() as conn:
        await asks.answer(conn, row["id"], person_id=row["owner_person_id"], option="yes")
        if by_expiry:
            await conn.execute(
                "UPDATE asks SET expires_at=now()-interval '1 second' WHERE id=%s", (row["id"],)
            )
        else:
            await conn.execute(
                "UPDATE asks SET answered_at=now()-interval '31 minutes' WHERE id=%s", (row["id"],)
            )
    await service.tick(ctx)
    assert (await read(ctx, row["id"]))["delivery_state"] == "failed"


async def test_stale_reconcile_replaces_card_even_with_same_digest(app, seed, slack_client):
    ctx = app.state.ctx
    await seed()
    await service.tick(ctx)
    await stale.reconcile(ctx)
    assert len([c for c in slack_client.calls if c["method"] == "chat_update"]) == 1


async def test_rule_answers_before_card_and_writes_decision(app, seed, slack_client):
    ctx = app.state.ctx
    row = await seed()
    async with ctx.pool.connection() as conn:
        await rules.create(
            conn,
            owner_person_id=row["owner_person_id"],
            text="Use the proposed plan",
            match={"ask_kind": "decision", "lane_source": "t3"},
            answer_option_label="Continue",
        )
    await service.tick(ctx)
    answered = await read(ctx, row["id"])
    assert answered["state"] == "answered" and answered["answered_via"] == "rule"
    assert answered["answer_text"] is None and answered["rule_id"]
    assert not [c for c in slack_client.calls if c["method"] == "chat_postMessage"]
    async with ctx.pool.connection() as conn:
        assert await items.get(conn, f"ask:{row['id']}")
        assert (await one(conn, "SELECT * FROM ask_rule_stats"))["answered_by_rule"] == 1


async def test_late_pull_and_ack_cannot_revive_failed_answer(app, seed):
    ctx = app.state.ctx
    row = await seed()
    async with ctx.pool.connection() as conn:
        await asks.answer(conn, row["id"], person_id=row["owner_person_id"], option="yes")
        await asks.pull(conn, row["requester_token_id"])
        await conn.execute(
            "UPDATE asks SET answered_at=now()-interval '31 minutes' WHERE id=%s", (row["id"],)
        )
        assert not await asks.ack(conn, row["id"], row["requester_token_id"])
        assert not await asks.pull(conn, row["requester_token_id"])
        assert (await ledger.lock_ask(conn, row["id"]))["delivery_state"] == "failed"


async def test_private_lane_cannot_send_card_to_different_explicit_owner(app, seed, slack_client):
    ctx = app.state.ctx
    row = await seed(owner="ben", lane={"visibility": "private"})
    await service.tick(ctx)
    assert not (await read(ctx, row["id"]))["slack_ts"]
    assert not [c for c in slack_client.calls if c["method"] == "chat_postMessage"]


async def test_stats_command_reports_real_counts(app, seed, capsys):
    from illo.cli import execute, parser
    from illo.config import Config

    ctx = app.state.ctx
    await seed()
    await service.tick(ctx)
    assert (
        await execute(parser().parse_args(["stats"]), Config(database_url=ctx.pool.conninfo)) == 0
    )
    output = capsys.readouterr().out
    assert "Cards sent for each person and day:" in output
    assert "ada" in output and "Asks answered by a rule:" in output


async def test_expired_tap_removes_buttons_before_the_next_timer(app, seed, slack_client):
    ctx = app.state.ctx
    row = await seed()
    await service.tick(ctx)
    row = await read(ctx, row["id"])
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE asks SET expires_at=now()-interval '1 second' WHERE id=%s", (row["id"],)
        )
    await actions.handle(ctx, await member(ctx), click(row))
    after = await read(ctx, row["id"])
    assert after["state"] == "expired" and after["answer_option"] is None
    assert all(b["type"] != "actions" for b in slack_client.calls[-1]["blocks"])
