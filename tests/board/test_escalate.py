"""Exercise the Slack outbox, card receipt, and typed replies."""

from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from illo.asks import actions, service
from illo.board import escalate
from illo.channels.slack.intake import SlackIntake
from illo.db import all_rows, one
from illo.gateway.events import InboundEvent
from illo.ledger import board, people
from illo.tasks.pages import signin
from tests.board.test_coordinator import admission, last_run, question, triage


async def queue(ctx, client, fmt="link", *, person="ada", summary=False, **fields):
    ctx.workspace.config.board.coordinator.slack_summaries = summary
    q = await question(client, **fields)
    event = await admission(ctx)
    choice = {"person": f"person:{person}", "format": fmt}
    if summary:
        choice["summary"] = 'Check <@U111> @here \x01 `https://example.com/x` www.example.org/a'
    if fmt == "card":
        choice.update(question="Which <#C123|room> plan? https://example.com/x",
                      options=[{"label": "Plan A", "detail": "@everyone Quick"},
                               {"label": "Plan B", "detail": "mailto:me@example.com"}],
                      recommended=1)
    await triage(ctx, event, "need_ask_person", **choice)
    assert (await last_run(ctx))["outcome"] == "applied"
    return q


async def escalation(ctx):
    async with ctx.pool.connection() as conn:
        return await one(conn, "SELECT * FROM board_escalations ORDER BY id DESC LIMIT 1")


def reply(root, *, text="Cache A.", sender=None, channel=None, ts=None, files=()):
    channel = channel or root["channel"]
    stamp = ts or f"{datetime.now(UTC).timestamp():.6f}"
    return InboundEvent(channel="slack", kind="dm", sender=sender or channel[1:],
                        external_id=f"{channel}:{stamp}", text="Prepared transcription and history",
                        reply_to=root["ts"], payload={
                            "message_ts": stamp, "thread_ts": root["ts"], "channel_id": channel,
                            "files": list(files), "raw_slack_text": text, "slack_typed_text": "Clip transcript"})


@pytest.mark.parametrize("fmt,summary", [("link", False), ("link", True), ("dm", True), ("card", True)])
async def test_slack_formats_are_private_and_card_commits_with_ts(coordinator_ctx, slack, client, fmt, summary):
    ctx = coordinator_ctx
    q = await queue(ctx, client, fmt, summary=summary)
    assert (await escalation(ctx))["state"] == "pending"
    async with ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT 1 FROM asks")
    await service.tick(ctx)
    assert not slack.roots()
    await escalate.send_due(ctx)
    [root] = slack.roots()
    sent = await escalation(ctx)
    assert sent["state"] == "sent" and root["channel"].startswith("D")
    assert not root["unfurl_links"] and not root["unfurl_media"]
    assert "Cache latency" not in root["text"] and "Why is" not in root["text"]
    if summary:
        for unsafe in ("www.example.org", "mailto:", "@here", "@everyone", "<@U"):
            assert unsafe not in str(root)
        # Bare HTTPS links are preserved in summaries so people can inspect referenced PRs/issues.
        assert "https://example.com/x" in str(root)
    else:
        assert "has a question for you" in root["text"]
    if fmt == "dm":
        assert "Reply in this thread" in root["text"]
        assert root["blocks"][1]["elements"][0]["action_id"] == f"web-open-board:{q['thread_id']}"
    elif fmt == "link":
        assert root["blocks"][1]["elements"][0]["action_id"] == f"web-open-board:{q['thread_id']}"
    else:
        assert sent["slack_ts"] is None and sent["slack_channel"] is None
        async with ctx.pool.connection() as conn:
            ask = await one(conn, "SELECT * FROM asks")
        assert ask["slack_ts"] == root["ts"] and ask["slack_channel"] == root["channel"]
        assert ask["id"] == sent["ask_id"] == sent["card_ask_id"]
        assert not ask["allow_rule"] and ask["source_ref"].startswith(f"board:{q['thread_id']}:")
        assert ask["recommended_option"] == "o2"
        assert "https://" not in ask["question"]
    await service.tick(ctx)
    assert len(slack.roots()) == 1


async def test_queued_private_card_keeps_summary_and_options_even_with_picture(coordinator_ctx, slack, client):
    ctx = coordinator_ctx
    q = await queue(ctx, client, "card", summary=True, visibility="private")
    queued = await escalation(ctx)
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE board_posts SET blocks=%s WHERE id=%s",
                           (Jsonb([{"kind": "image"}]), q["post_id"]))
    await escalate.send_due(ctx)
    [root] = slack.roots()
    sent = await escalation(ctx)
    assert sent["id"] == queued["id"] and sent["state"] == "sent"
    assert sent["content_mode"] == "summary" and sent["format"] == "card"
    assert sent["ask_id"] == sent["card_ask_id"]
    assert "Plan A" in str(root) and "Plan B" in str(root)
    async with ctx.pool.connection() as conn:
        assert await one(conn, "SELECT id FROM asks WHERE id=%s", (sent["ask_id"],))
        assert (await one(conn, "SELECT state FROM board_episodes"))["state"] == "escalated"


@pytest.mark.parametrize("format", ["link", "card"])
async def test_timeout_after_slack_accepts_reconciles_without_second_post(coordinator_ctx, slack, client, format):
    ctx = coordinator_ctx
    await queue(ctx, client, format, summary=format == "card")
    slack.lose_receipt = True
    await escalate.send_due(ctx)
    assert (await escalation(ctx))["state"] == "sending"
    async with ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT 1 FROM asks")
        await conn.execute("UPDATE board_escalations SET lease_until=now()-interval '1 second'")
    await service.tick(ctx)
    assert len(slack.roots()) == 1
    await escalate.send_due(ctx)
    sent = await escalation(ctx)
    assert sent["state"] == "sent" and sent["attempts"] == 2
    assert len(slack.roots()) == 1
    if format == "card":
        async with ctx.pool.connection() as conn:
            assert (await one(conn, "SELECT slack_ts FROM asks"))["slack_ts"] == slack.roots()[0]["ts"]


async def test_early_reply_and_transient_verification_release_dedupe(coordinator_ctx, slack, client):
    ctx = coordinator_ctx
    question = await queue(ctx, client)
    slack.lose_history = True
    calls = []

    async def when_slack_accepts(fields, ts):
        assert (await escalation(ctx))["state"] == "sending"
        event = reply({**fields, "ts": ts}, text="Use cache B &amp; restart. <!-- hide -->",
                      files=[{"id": "FAKE"}])
        calls.append(event)
        first = await ctx.gateway.receive(event)
        assert first == {"decision": "retry", "retryable": True}
        async with ctx.pool.connection() as conn:
            assert not await one(conn, "SELECT 1 FROM events WHERE external_id=%s", (event.external_id,))
        result = await ctx.gateway.receive(event)
        assert result["decision"] == "claimed"
        assert result["text"].endswith("Pictures stay in Slack.")
        assert (await escalation(ctx))["state"] == "answered"

    slack.on_send = when_slack_accepts
    await escalate.send_due(ctx)
    assert len(calls) == 1
    assert (await ctx.gateway.receive(calls[0]))["duplicate"]
    row = await escalation(ctx)
    assert row["state"] == "answered"
    async with ctx.pool.connection() as conn:
        answer = await one(conn, "SELECT * FROM board_posts WHERE id=%s", (row["answer_post_id"],))
        assert answer["kind"] == "answer" and answer["reply_to_post_id"] == question["post_id"]
        assert answer["text"] == "Use cache B & restart."
        assert not await one(conn, "SELECT 1 FROM runs WHERE profile != 'board_coordinator'")
        assert (await one(conn, "SELECT consecutive_agent_posts FROM board_threads"))["consecutive_agent_posts"] == 0


@pytest.mark.parametrize("offset", [-60, 90])
async def test_unrelated_dm_root_outside_send_window_needs_no_lookup(
    coordinator_ctx, slack, client, fake_brain, offset
):
    ctx = coordinator_ctx
    await queue(ctx, client)

    async def while_sending(fields, ts):
        unrelated = f"{float(ts) + offset:.6f}"
        slack.lose_history = True
        result = await ctx.gateway.receive(reply({**fields, "ts": unrelated}))
        assert result["decision"] == "run"
        assert slack.lose_history

    slack.on_send = while_sending
    await escalate.send_due(ctx)
    assert len(fake_brain.requests) == 1
    assert (await escalation(ctx))["state"] == "sent"


async def test_raw_slack_text_survives_clip_preparer(coordinator_ctx, slack, client, monkeypatch):
    ctx = coordinator_ctx
    await queue(ctx, client)
    await escalate.send_due(ctx)
    [root] = slack.roots()
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    intake.team_id = "T_ACME"

    async def clips(*args, **kwargs):
        return ["This is the clip transcript."]

    monkeypatch.setattr("illo.channels.slack.intake.read_clips", clips)
    result = await intake.receive({"team_id": "T_ACME", "event": {
        "type": "message", "channel": root["channel"], "channel_type": "im",
        "user": root["channel"][1:], "thread_ts": root["ts"],
        "ts": f"{datetime.now(UTC).timestamp():.6f}", "text": "Typed words.",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Supplied block."}}],
        "files": [{"id": "F123", "mimetype": "audio/mp4", "is_audio": True}],
    }})
    assert result["decision"] == "claimed"
    async with ctx.pool.connection() as conn:
        data = (await one(conn, "SELECT payload FROM events WHERE channel='slack'"))["payload"]
        assert data["payload"]["raw_slack_text"] == "Typed words."
        assert "clip transcript" in data["text"]
        assert (await one(conn, "SELECT text FROM board_posts WHERE is_human"))["text"] == "Typed words."


async def test_known_root_later_replies_and_non_board_dm(coordinator_ctx, slack, client, fake_brain):
    ctx = coordinator_ctx
    q = await queue(ctx, client)
    await escalate.send_due(ctx)
    [root] = slack.roots()
    msg = reply(root, text="", files=[{"id": "F123"}])
    assert (await ctx.gateway.receive(msg))["text"] == escalate.WRITE_TEXT
    assert (await ctx.gateway.receive(reply(root)))["decision"] == "claimed"
    assert (await ctx.gateway.receive(reply(root, text="More detail.")))["decision"] == "claimed"
    other = reply(root, sender="U_ACME_BEN")
    assert (await ctx.gateway.receive(other))["decision"] == "run"
    top = reply(root)
    top.payload["thread_ts"] = None
    top.reply_to = None
    assert (await ctx.gateway.receive(top))["decision"] == "run"
    await (await client()).call("board_resolve", {"thread_id": q["thread_id"]})
    assert "closed" in (await ctx.gateway.receive(reply(root)))["text"]
    async with ctx.pool.connection() as conn:
        posted = await all_rows(conn, "SELECT kind,text FROM board_posts WHERE is_human ORDER BY id")
        assert posted == [{"kind": "answer", "text": "Cache A."}, {"kind": "message", "text": "More detail."}]
        assert len(fake_brain.requests) == 2


async def test_other_person_closes_sent_root_and_late_reply_is_extra_note(coordinator_ctx, slack, client):
    ctx = coordinator_ctx
    q = await queue(ctx, client)
    await escalate.send_due(ctx)
    [root] = slack.roots()
    async with ctx.pool.connection() as conn:
        await board.human_post(conn, workspace=ctx.workspace, person=await people.get(conn, "ben"),
                               thread_id=q["thread_id"], text="Already fixed.", source_ref="web:other",
                               reply_to_post_id=q["post_id"], via="web")
    await escalate.send_due(ctx)
    await escalate.send_due(ctx)
    notices = [m for m in slack.messages.values() if m.get("thread_ts") == root["ts"]]
    assert len(notices) == 1 and "Nothing needed from you" in notices[0]["text"]
    result = await ctx.gateway.receive(reply(root, text="For future reference."))
    assert "answered first, so this is an extra note" in result["text"]
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE board_threads SET visibility='private',owner_person_id=%s",
                           ((await people.get(conn, "ben"))["id"],))
    assert "cannot put" in (await ctx.gateway.receive(reply(root)))["text"]
    old = (datetime.now(UTC) - timedelta(days=15)).timestamp()
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE board_escalations SET slack_ts=%s", (f"{old:.6f}",))
    assert "old" in (await ctx.gateway.receive(reply({**root, "ts": f"{old:.6f}"})))["text"]


@pytest.mark.parametrize("change", ["answer", "resolve", "privacy", "quiet", "refusal", "channel"])
async def test_delivery_rechecks_live_conditions(coordinator_ctx, slack, client, change, monkeypatch):
    ctx = coordinator_ctx
    q = await queue(ctx, client)
    if change == "answer":
        await (await client("ben")).call("board_post", {
            "thread_id": q["thread_id"], "kind": "answer", "text": "All done.",
            "reply_to_post_id": q["post_id"],
        })
    elif change == "resolve":
        await (await client()).call("board_resolve", {"thread_id": q["thread_id"]})
    elif change == "privacy":
        async with ctx.pool.connection() as conn:
            await conn.execute("UPDATE board_threads SET visibility='private',owner_person_id=%s",
                               ((await people.get(conn, "ben"))["id"],))
    elif change == "quiet":
        monkeypatch.setattr(escalate.policy, "next_window",
                            lambda workspace, person, now=None: now + timedelta(hours=2))
    elif change == "refusal":
        slack.refused = True
    elif change == "channel":
        async def bad_channel(**kwargs):
            return {"channel": {"id": "C123"}}

        monkeypatch.setattr(slack, "conversations_open", bad_channel)
    await escalate.send_due(ctx)
    assert slack.roots() == []
    row = await escalation(ctx)
    if change == "quiet":
        assert row["state"] == "pending" and row["due_at"] > datetime.now(UTC) + timedelta(hours=1)
    elif change in {"refusal", "channel"}:
        assert row["state"] == "failed"
    else:
        assert row["state"] == "closed"


async def test_card_closed_while_sending_commits_a_cancelled_ask(coordinator_ctx, slack, client):
    ctx = coordinator_ctx
    q = await queue(ctx, client, "card", summary=True)

    async def close_during_send(fields, ts):
        await (await client()).call("board_resolve", {"thread_id": q["thread_id"]})

    slack.on_send = close_during_send
    await escalate.send_due(ctx)
    row = await escalation(ctx)
    assert row["state"] == "closed"
    async with ctx.pool.connection() as conn:
        ask = await one(conn, "SELECT * FROM asks WHERE id=%s", (row["ask_id"],))
        assert ask["state"] == "cancelled" and ask["slack_ts"] == slack.roots()[0]["ts"]
    assert any(call["method"] == "chat_update" for call in slack.calls)


@pytest.mark.parametrize("answer_kind", ["option", "other", "older_image", "expire", "cancel"])
async def test_card_replies_and_sweep(coordinator_ctx, slack, client, answer_kind):
    ctx = coordinator_ctx
    q = await queue(ctx, client, "card", summary=True)
    await escalate.send_due(ctx)
    row = await escalation(ctx)
    [root] = slack.roots()
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        if answer_kind == "older_image":
            from illo.ledger import asks

            await asks.answer(conn, row["ask_id"], person_id=ada["id"], option="o1")
        if answer_kind in {"expire", "cancel"}:
            await conn.execute("UPDATE asks SET state=%s WHERE id=%s",
                               ("expired" if answer_kind == "expire" else "cancelled", row["ask_id"]))
    click = {"channel": root["channel"], "message_ts": root["ts"]}
    if answer_kind == "option":
        await actions.handle(ctx, ada, click | {"action_id": f"ask:{row['ask_id']}:opt:o1"})
    elif answer_kind == "other":
        await actions.handle(ctx, ada, click | {"action_id": f"ask:{row['ask_id']}:other"})
        assert (await ctx.gateway.receive(reply(root, text="Plan C.")))["text"] == "Answer saved."
    await escalate.send_due(ctx)
    row = await escalation(ctx)
    if answer_kind in {"expire", "cancel"}:
        assert row["state"] == "closed" and not row["answer_post_id"]
        return
    assert row["state"] == "answered"
    async with ctx.pool.connection() as conn:
        post = await one(conn, "SELECT * FROM board_posts WHERE id=%s", (row["answer_post_id"],))
    assert post["is_human"] and post["kind"] == "answer" and post["reply_to_post_id"] == q["post_id"]
    assert post["text"] == ("Plan C." if answer_kind == "other" else "Plan A. Quick")


async def test_sweep_skips_missing_and_broken_rows_then_delivers(coordinator_ctx, slack, client, monkeypatch, caplog):
    ctx = coordinator_ctx
    ctx.workspace.config.board.coordinator.max_escalations_per_person_per_day = 5
    rows = []
    for _ in range(3):
        await queue(ctx, client, "card", summary=True)
        await escalate.send_due(ctx)
        rows.append(await escalation(ctx))
    await queue(ctx, client)
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE asks SET state='expired'")
    original = board.lock_for_ask

    async def lock(conn, ask_id):
        if ask_id == rows[0]["ask_id"]:
            return None
        if ask_id == rows[1]["ask_id"]:
            raise RuntimeError
        return await original(conn, ask_id)

    monkeypatch.setattr(board, "lock_for_ask", lock)
    await escalate.send_due(ctx)
    assert f"Could not reconcile board escalation {rows[1]['id']} (RuntimeError)." in caplog.text
    async with ctx.pool.connection() as conn:
        states = await all_rows(conn, "SELECT state,closed_reason FROM board_escalations ORDER BY id")
    assert states == [
        {"state": "sent", "closed_reason": None},
        {"state": "sent", "closed_reason": None},
        {"state": "closed", "closed_reason": "ask_expired"},
        {"state": "sent", "closed_reason": None},
    ]
    assert len(slack.roots()) == 4


async def test_open_thread_button_creates_a_private_signin(coordinator_ctx, slack, client):
    ctx = coordinator_ctx
    q = await queue(ctx, client)
    await escalate.send_due(ctx)
    [root] = slack.roots()
    click = {"channel": root["channel"], "message_ts": root["ts"], "trigger_id": "trigger",
             "action_id": f"web-open-board:{q['thread_id']}"}
    async with ctx.pool.connection() as conn:
        ada, ben = await people.get(conn, "ada"), await people.get(conn, "ben")
    await signin.open_action(ctx, ben, click)
    await signin.open_action(ctx, ada, click)
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT person_id,next_path FROM web_signins")
        assert row == {"person_id": ada["id"], "next_path": f"/web/board/t/{q['thread_id']}"}
    [modal] = [call["view"] for call in slack.calls if call["method"] == "views_open"]
    assert modal["title"]["text"] == "Board thread"
