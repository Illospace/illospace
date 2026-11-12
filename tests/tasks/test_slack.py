"""Task changes survive Slack failures. Cards accept only their reviewer."""


from illo.channels.slack import tasks as surface
from illo.db import all_rows, one
from illo.gateway import InboundEvent
from illo.ledger import kv, people, tasks
from illo.workspace import TaskList

ADA = tasks.Viewer("ada", "person:ada", human=True)
BUILD = tasks.Viewer("ada", "agent:build")


async def returned(ctx, **values):
    async with ctx.pool.connection() as conn:
        row = await tasks.create(conn, workspace=ctx.workspace, viewer=ADA, list_key="launches", title="A launch <test>", holder="agent:build", **values)
        row = await tasks.write_result(conn, row["id"], workspace=ctx.workspace, viewer=BUILD,
                                      append="A result", message="Please confirm the result.", returned=True)
    return row


async def test_lazy_thread_card_and_done(app, slack_client):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        row = await tasks.create(conn, workspace=ctx.workspace, viewer=ADA, list_key="launches", title="A video", holder="agent:build")
    await surface.tick(ctx)
    assert not any(call["method"] == "chat_postMessage" for call in slack_client.calls)
    async with ctx.pool.connection() as conn:
        await tasks.write_result(conn, row["id"], workspace=ctx.workspace, viewer=BUILD,
                                 append="A result", message="Please confirm <the result>.", returned=True)
    await surface.tick(ctx)
    async with ctx.pool.connection() as conn:
        row = await tasks.get(conn, row["id"], viewer=ADA)
        card = await kv.get(conn, f"task:card:{row['id']}")
        ada = await people.get(conn, "ada")
        ben = await people.get(conn, "ben")
    assert row["slack_channel"].startswith("D") and row["slack_ts"] and card
    posts = [call for call in slack_client.calls if call["method"] == "chat_postMessage"]
    assert posts[0]["text"].startswith("Task: A video")
    assert "<@U_ACME_ADA>" in posts[-1]["text"]
    buttons = posts[-1]["blocks"][-1]["elements"]
    assert [button["text"]["text"] for button in buttons] == ["Done", "Send back"]
    action = {"action_id": f"task:{row['id']}:done", "channel": row["slack_channel"], "message_ts": card["ts"]}
    await surface.handle(ctx, ben, action)
    async with ctx.pool.connection() as conn:
        assert (await tasks.get(conn, row["id"], viewer=ADA))["state"] == "review"
    await surface.handle(ctx, ada, {**action, "message_ts": "wrong"})
    await surface.handle(ctx, ada, action)
    async with ctx.pool.connection() as conn:
        assert (await tasks.get(conn, row["id"], viewer=ADA))["state"] == "done"
    assert any(call["method"] == "chat_update" and call.get("blocks") and call["blocks"][-1]["text"]["text"] == "Done." for call in slack_client.calls)


async def test_send_back_asks_for_reason(app):
    ctx = app.state.ctx
    row = await returned(ctx)
    await surface.tick(ctx)
    async with ctx.pool.connection() as conn:
        row = await tasks.get(conn, row["id"], viewer=ADA)
        card = await kv.get(conn, f"task:card:{row['id']}")
        ada = await people.get(conn, "ada")
    await surface.handle(ctx, ada, {"action_id": f"task:{row['id']}:back", "channel": row["slack_channel"], "message_ts": card["ts"]})
    event = InboundEvent(channel="slack", external_id="task-back-1", kind="dm", sender=ada["slack_id"], text="Use the shorter version.", reply_to=row["slack_ts"], payload={"channel_id": row["slack_channel"], "thread_ts": row["slack_ts"]})
    from illo.gateway.principal import Authority
    assert await surface.claim_back(ctx, event, Authority("person:ada", "person_dm", ada["id"]))
    async with ctx.pool.connection() as conn:
        row = await tasks.get(conn, row["id"], viewer=ADA)
        assert row["state"] == "todo" and row["waits_for"] == "agent:build"
        assert (await tasks.events(conn, row["id"], viewer=ADA))[-1]["text"] == "Use the shorter version."


async def test_slack_failure_keeps_change_and_retry(app, slack_client, monkeypatch):
    ctx = app.state.ctx
    row = await returned(ctx)
    real = slack_client.chat_postMessage
    async def failed(**kwargs):
        raise RuntimeError("A Slack failure")
    monkeypatch.setattr(slack_client, "chat_postMessage", failed)
    await surface.tick(ctx)
    async with ctx.pool.connection() as conn:
        assert (await tasks.get(conn, row["id"], viewer=ADA))["state"] == "review"
        assert await kv.get(conn, f"task:pending:{row['id']}")
        assert (await one(conn, "SELECT * FROM sensors WHERE key='tasks:slack'"))["last_error_at"]
    monkeypatch.setattr(slack_client, "chat_postMessage", real)
    await surface.tick(ctx)
    async with ctx.pool.connection() as conn:
        assert not await kv.get(conn, f"task:pending:{row['id']}")
        assert (await tasks.get(conn, row["id"], viewer=ADA))["slack_ts"]


async def test_question_and_thread_reply_add_context(app, slack_client):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        row = await tasks.create(conn, workspace=ctx.workspace, viewer=ADA, list_key="launches", title="A launch", holder="agent:build")
        await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=BUILD, text="Which title?", question=True)
    await surface.tick(ctx)
    async with ctx.pool.connection() as conn:
        row = await tasks.get(conn, row["id"], viewer=ADA)
    event = InboundEvent(channel="slack", external_id="task-message-1", kind="dm", sender="U_ACME_ADA", text="Use the search phrase.", reply_to=row["slack_ts"], payload={"channel_id": row["slack_channel"], "thread_ts": row["slack_ts"]})
    context = await surface.context(ctx, event)
    assert 'source="task-context"' in context and "Which title?" in context
    await surface.context(ctx, event)
    async with ctx.pool.connection() as conn:
        row = await tasks.get(conn, row["id"], viewer=ADA)
        assert row["waits_for"] == "agent:build"
        assert len(await all_rows(conn, "SELECT * FROM task_events WHERE task_id=%s AND actor='person:ada' AND kind='message'", (row["id"],))) == 1
    event.payload["contains_outside_text"] = True
    assert await surface.context(ctx, event) is None


async def test_private_list_uses_owner_dm(app, slack_client):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.lists.append(TaskList(key="private", name="Private", owner="ada", slack_channel="software"))
    async with ctx.pool.connection() as conn:
        row = await tasks.create(conn, workspace=ctx.workspace, viewer=ADA, list_key="private", title="A private task", holder="agent:build")
        await tasks.write_result(conn, row["id"], workspace=ctx.workspace, viewer=BUILD, append="A result", message="Please confirm.", returned=True)
    await surface.tick(ctx)
    posts = [call for call in slack_client.calls if call["method"] == "chat_postMessage"]
    assert posts and all(call["channel"].startswith("D") for call in posts)
    assert any(call.get("users") == "U_ACME_ADA" for call in slack_client.calls)


async def test_new_event_during_delivery_keeps_pending_key(app, slack_client, monkeypatch):
    ctx = app.state.ctx
    row = await returned(ctx)
    # Simulate a new committed marker after the sender reads its snapshot.
    original = surface._thread
    async def changed_marker(app_ctx, conn, task, entry, person):
        task = await original(app_ctx, conn, task, entry, person)
        await kv.set(conn, f"task:pending:{task['id']}", {"event": 999999})
        return task
    monkeypatch.setattr(surface, "_thread", changed_marker)
    await surface.tick(ctx)
    async with ctx.pool.connection() as conn:
        assert await kv.get(conn, f"task:pending:{row['id']}") == {"event": 999999}


async def test_task_thread_starts_a_run_with_task_context(app, slack_client, fake_brain):
    from illo.channels.slack.intake import SlackIntake

    ctx = app.state.ctx
    row = await returned(ctx)
    await surface.tick(ctx)
    async with ctx.pool.connection() as conn:
        row = await tasks.get(conn, row["id"], viewer=ADA)
    intake = SlackIntake(ctx, "U_ACME_ILLO", bot_identity="B_ILLO")
    result = await intake.receive({"type": "event_callback", "event": {
        "type": "message", "channel": row["slack_channel"], "channel_type": "im",
        "user": "U_ACME_ADA", "ts": "1700000100", "thread_ts": row["slack_ts"],
        "text": "Did they finish?"}})
    assert "error" not in result
    request = fake_brain.requests[-1]
    assert "task-context" in request.user_message and '"list_key":"launches"' in request.user_message
    assert request.user_message.endswith("Request:\nDid they finish?")


async def test_other_person_gets_question_and_review_in_their_dm(app, slack_client, monkeypatch):
    ctx = app.state.ctx
    async def distinct_dm(**kwargs):
        await slack_client._record("conversations_open", **kwargs)
        return {"channel": {"id": "D_" + kwargs["users"]}}
    monkeypatch.setattr(slack_client, "conversations_open", distinct_dm)
    async with ctx.pool.connection() as conn:
        row = await tasks.create(conn, workspace=ctx.workspace, viewer=ADA, list_key="launches", title="A launch", holder="agent:build")
    await surface.tick(ctx)
    async with ctx.pool.connection() as conn:
        await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=BUILD, text="Which version?", question=True, to="person:ben")
    await surface.tick(ctx)
    assert any(call.get("channel") == "D_U_ACME_BEN" and "Which version?" in call.get("text", "") for call in slack_client.calls)
    # A first question in Ben's DM still sends the later review card to Ada.
    async with ctx.pool.connection() as conn:
        other = await tasks.create(conn, workspace=ctx.workspace, viewer=ADA, list_key="launches", title="Another launch", holder="agent:build")
        await tasks.post(conn, other["id"], workspace=ctx.workspace, viewer=BUILD, text="Which format?", question=True, to="person:ben")
    await surface.tick(ctx)
    async with ctx.pool.connection() as conn:
        await tasks.write_result(conn, other["id"], workspace=ctx.workspace, viewer=BUILD, append="A result", message="Please confirm.", returned=True)
    await surface.tick(ctx)
    async with ctx.pool.connection() as conn:
        card = await kv.get(conn, f"task:card:{other['id']}")
        task = await tasks.get(conn, other["id"], viewer=ADA)
        ada = await people.get(conn, "ada")
    assert task["slack_channel"] == "D_U_ACME_BEN"
    assert card["channel"] == "D_U_ACME_ADA"
    await surface.handle(ctx, ada, {"action_id": f"task:{other['id']}:done", "channel": card["channel"], "message_ts": card["ts"]})
    async with ctx.pool.connection() as conn:
        assert (await tasks.get(conn, other["id"], viewer=ADA))["state"] == "done"


def test_agent_comment_uses_slack_markdown_and_bounded_text():
    body = "## Progress\n**The checks pass.** [Proof](https://example.com/proof)\n" + "More detail. " * 100
    event = {"text": body, "data": {"url": "https://github.com/acme/app/issues/1#issuecomment-2"}}
    text = surface.agent_message(event)
    assert text.startswith("*Progress*\n*The checks pass.* <https://example.com/proof|Proof>")
    assert "**" not in text and "##" not in text
    prefix, rest = text.split("… ")
    assert len(prefix) <= 600
    assert rest == "<https://github.com/acme/app/issues/1#issuecomment-2|Read the rest>"
    assert surface.agent_message({"text": "**Ready.** <@U_ACME_ADA>", "data": {}}) == "*Ready.* &lt;@U_ACME_ADA&gt;"


async def test_task_root_uses_plain_state_words(app, slack_client):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        row = await tasks.create(conn, workspace=ctx.workspace, viewer=ADA, list_key="launches", title="A task", holder="person:ben")
    await surface.tick(ctx)
    assert any(c["text"].endswith(" · to do") for c in slack_client.calls if c["method"] == "chat_postMessage")
    assert [surface.state_label(s) for s in ("todo", "working", "review", "done", "cancelled")] == [
        "to do", "in work", "in review", "done", "cancelled"]
    assert row["state"] == "todo"
