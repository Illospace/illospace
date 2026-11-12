"""Send task questions and returned work after the task transaction commits."""

from collections import Counter
from uuid import NAMESPACE_URL, uuid5

from psycopg.types.json import Jsonb

from illo.asks.cards import escape, task_return_card
from illo.channels.slack.access import channel_private
from illo.db import all_rows, one
from illo.ledger import audit, kv, people, sensors
from illo.ledger import tasks as ledger
from illo.tasks.release import in_window
from illo.tools._format import data_text, json_text


async def holder_name(ctx, conn, ref):
    if not ref:
        return "No holder"
    kind, _, key = ref.partition(":")
    if kind == "person":
        person = await people.get(conn, key)
        return person["name"] if person else key
    agent = next((a for a in ctx.workspace.agents if a.key == key), None)
    return agent.name if agent else key


def message_id(kind, task_id, event_id=0):
    return str(uuid5(NAMESPACE_URL, f"illo:task:{kind}:{task_id}:{event_id}"))


async def _channel(ctx, conn, row, entry, person):
    if row["visibility"] == "team" and entry.slack_channel:
        channel = next((c for c in ctx.workspace.config.slack.channels
                        if c.name == entry.slack_channel and c.mode != "off"), None)
        if channel and not await channel_private(ctx, channel.id, conn=conn):
            return channel.id
    if not person or not person["slack_id"]:
        raise ValueError("The task needs a person with Slack access.")
    response = await ctx.slack_client.conversations_open(users=person["slack_id"])
    return response["channel"]["id"]


async def _thread(ctx, conn, row, entry, person):
    if row["slack_channel"] and row["slack_ts"]:
        # Recheck a configured channel before any later post.
        if not row["slack_channel"].startswith("D") and await channel_private(ctx, row["slack_channel"], conn=conn):
            raise ValueError("The task channel is now private.")
        return row
    channel = await _channel(ctx, conn, row, entry, person)
    text = ("Task: " + escape(row["title"]) + " · " + escape(entry.name) + " · "
            + escape(await holder_name(ctx, conn, row["holder"])) + " · " + escape(row["state"]))
    sent = await ctx.slack_client.chat_postMessage(channel=channel, text=text,
        unfurl_links=False, client_msg_id=message_id("root", row["id"]))
    row = await one(conn, "UPDATE tasks SET slack_channel=%s,slack_ts=%s WHERE id=%s RETURNING *",
                    (channel, sent["ts"], row["id"]))
    await kv.set(conn, f"slack:thread:{channel}:{sent['ts']}", True)
    if channel.startswith("D"):
        await kv.set(conn, f"task:dm-owner:{row['id']}", person["key"])
    return row


async def _recipient_channel(ctx, conn, row, person):
    if row["slack_channel"].startswith("D") and person and (
        await kv.get(conn, f"task:dm-owner:{row['id']}") != person["key"]
    ):
        opened = await ctx.slack_client.conversations_open(users=person["slack_id"])
        return opened["channel"]["id"]
    return row["slack_channel"]


async def _thread_task(conn, channel, thread):
    row = await one(conn, "SELECT id FROM tasks WHERE slack_channel=%s AND slack_ts=%s", (channel, thread))
    if row:
        return row
    card = await one(conn, """SELECT key FROM kv WHERE key LIKE 'task:card:%%'
        AND value->>'channel'=%s AND value->>'ts'=%s""", (channel, thread))
    return {"id": int(card["key"].rsplit(":", 1)[1])} if card else None


async def _send(ctx, conn, task_id, reminder_counts=None):
    # This separate connection can see only committed task changes.
    await conn.execute("SELECT pg_advisory_xact_lock(12000002)")
    pending_version = await kv.get(conn, f"task:pending:{task_id}")
    row = await one(conn, "SELECT * FROM tasks WHERE id=%s", (task_id,))
    if not row:
        await conn.execute("DELETE FROM kv WHERE key=%s AND value=%s",
                           (f"task:pending:{task_id}", Jsonb(pending_version)))
        return
    entry = next((entry for entry in ctx.workspace.lists if entry.key == row["list_key"]), None)
    if not entry:
        raise ValueError("The task list is missing.")
    cursor = await kv.get(conn, f"task:sent:{task_id}") or 0
    events = await all_rows(conn, "SELECT * FROM task_events WHERE task_id=%s AND id>%s ORDER BY id", (task_id, cursor))
    target = row["waits_for"] if (row["waits_for"] or "").startswith("person:") else None
    if row["visibility"] == "private":
        person = await one(conn, "SELECT * FROM people WHERE id=%s AND active", (row["owner_person_id"],))
    else:
        ref = target or await ledger.reviewer(conn, ctx.workspace, row)
        person = await people.get(conn, ref[7:]) if ref else None
    reminder = next((e for e in reversed(events) if e["kind"] == "reminded"
                     and e["data"].get("to") == target), None)
    if reminder and (not in_window(ctx.workspace, person) or
                     (reminder_counts is not None and reminder_counts[target] >= 5)):
        raise ValueError("The task reminder waits for its delivery window.")
    # The thread starts only when the task needs a person: a question, returned work,
    # or a hand-over from someone else. A read never starts it.
    handed = [e for e in events if e["kind"] == "handed" and target
              and e["data"].get("holder") == target and e["actor"] != target]
    needs_person = handed or reminder or any(e["kind"] in {"question", "returned"}
        or (e["kind"] == "wake_failed" and e["data"].get("final"))
        or e["data"].get("escalated") or e["data"].get("notify") for e in events) or (
        row["state"] == "review" and target)
    if not row["slack_ts"] and not needs_person:
        if events:
            await kv.set(conn, f"task:sent:{task_id}", events[-1]["id"])
        await conn.execute("DELETE FROM kv WHERE key=%s AND value=%s",
                           (f"task:pending:{task_id}", Jsonb(pending_version)))
        return
    row = await _thread(ctx, conn, row, entry, person)
    root_text = ("Task: " + escape(row["title"]) + " · " + escape(entry.name) + " · "
                 + escape(await holder_name(ctx, conn, row["holder"])) + " · " + escape(row["state"]))
    if await kv.get(conn, f"task:root:{task_id}") != root_text:
        await ctx.slack_client.chat_update(channel=row["slack_channel"], ts=row["slack_ts"], text=root_text)
        await kv.set(conn, f"task:root:{task_id}", root_text)
    mention = f"<@{person['slack_id']}> " if person and person["slack_id"] else ""
    card = await kv.get(conn, f"task:card:{task_id}")
    returned = next((e for e in reversed(events) if e["kind"] == "returned"), None)
    for event in events:
        if event["kind"] in {"reminded", "wake_failed"} or event["data"].get("escalated") or event["data"].get("notify"):
            if event["kind"] == "reminded" and event is not reminder:
                continue
            if event["kind"] == "wake_failed" and not event["data"].get("final"):
                continue
            text = event["text"]
            name = await holder_name(ctx, conn, event["data"].get("holder") or row["holder"])
            if event["kind"] == "wake_failed":
                text = f"Illo could not wake {name}. Reason: {text}"
            elif event["data"].get("escalated"):
                text += f" Holder: {name}. Silence: {event['data']['hours']} hours. Illo tried {event['data']['restarts']} restarts."
            await ctx.slack_client.chat_postMessage(channel=row["slack_channel"], thread_ts=row["slack_ts"],
                text=mention + escape(text), client_msg_id=message_id(event["kind"], task_id, event["id"]))
            direct = await _recipient_channel(ctx, conn, row, person)
            if direct != row["slack_channel"]:
                await ctx.slack_client.chat_postMessage(channel=direct, text=mention + f"Task {task_id}: " + escape(text),
                    client_msg_id=message_id("notice-" + event["kind"], task_id, event["id"]))
        elif event["kind"] == "question":
            asked = await people.get(conn, event["data"].get("to", "")[7:])
            prefix = f"<@{asked['slack_id']}> " if asked and asked["slack_id"] else mention
            await ctx.slack_client.chat_postMessage(channel=row["slack_channel"], thread_ts=row["slack_ts"],
                text=prefix + escape(event["text"]), client_msg_id=message_id("question", task_id, event["id"]))
            if asked:
                direct = await _recipient_channel(ctx, conn, row, asked)
                if direct != row["slack_channel"]:
                    await ctx.slack_client.chat_postMessage(channel=direct,
                        text=f"Task {task_id}: " + escape(row["title"]) + "\n" + escape(event["text"])
                             + f"\nReply with task {task_id} and your answer.",
                        client_msg_id=message_id("question-notice", task_id, event["id"]))
        elif event in handed and event is handed[-1]:
            await ctx.slack_client.chat_postMessage(channel=row["slack_channel"], thread_ts=row["slack_ts"],
                text=mention + "You hold this task now.", client_msg_id=message_id("handed", task_id, event["id"]))
        elif event["kind"] == "message" and event["actor"].startswith("agent:") and not returned:
            await ctx.slack_client.chat_postMessage(channel=row["slack_channel"], thread_ts=row["slack_ts"],
                text=escape(event["text"]), client_msg_id=message_id("message", task_id, event["id"]))
    if row["state"] == "review" and (returned or not card):
        if card:
            await ctx.slack_client.chat_update(channel=card.get("channel", row["slack_channel"]), ts=card["ts"],
                text="The holder returned a newer result.",
                blocks=task_return_card(row, card["message"], [], final="The holder returned a newer result."))
        message = returned["text"] if returned else "All pieces are done. Confirm the whole task."
        proof = await ledger.proof(conn, ctx.workspace, row, ctx)
        blocks = task_return_card(row, message, proof)
        delivery_channel = await _recipient_channel(ctx, conn, row, person)
        if delivery_channel != row["slack_channel"]:
            await ctx.slack_client.chat_postMessage(channel=row["slack_channel"], thread_ts=row["slack_ts"],
                text="Returned: " + escape(row["title"]) + ". The review card waits in the reviewer's DM.",
                client_msg_id=message_id("review-notice", task_id, returned["id"] if returned else events[-1]["id"]))
        sent = await ctx.slack_client.chat_postMessage(channel=delivery_channel,
            **({"thread_ts": row["slack_ts"]} if delivery_channel == row["slack_channel"] else {}),
            text=mention + f"Task {task_id}. Returned: " + escape(row["title"]), blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": mention or " "}}, *blocks],
            client_msg_id=message_id("returned", task_id, returned["id"] if returned else events[-1]["id"]))
        card = {"ts": sent["ts"], "message": message, "channel": delivery_channel,
                "thread": row["slack_ts"] if delivery_channel == row["slack_channel"] else sent["ts"]}
        await kv.set(conn, f"task:card:{task_id}", card)
    elif card:
        proof = await ledger.proof(conn, ctx.workspace, row, ctx)
        final = None if row["state"] == "review" else ("Done." if row["state"] == "done" else "Sent back." if row["state"] in {"todo", "working"} else "Cancelled.")
        await ctx.slack_client.chat_update(channel=card.get("channel", row["slack_channel"]), ts=card["ts"],
            text=escape(row["title"]) + " · " + row["state"],
            blocks=task_return_card(row, card["message"], proof, final=final))
    if reminder and reminder_counts is not None:
        reminder_counts[target] += 1
    if events:
        await kv.set(conn, f"task:sent:{task_id}", events[-1]["id"])
    await conn.execute("DELETE FROM kv WHERE key=%s AND value=%s",
                           (f"task:pending:{task_id}", Jsonb(pending_version)))


async def tick(ctx):
    if not ctx.slack_client:
        return
    async with ctx.pool.connection() as conn:
        pending = await all_rows(conn, "SELECT key FROM kv WHERE key LIKE 'task:pending:%%' ORDER BY key LIMIT 50")
    reminder_counts = Counter()
    for value in pending:
        task_id = int(value["key"].rsplit(":", 1)[1])
        try:
            async with ctx.pool.connection() as conn:
                await _send(ctx, conn, task_id, reminder_counts)
                await sensors.ok(conn, "tasks:slack")
        except Exception:
            async with ctx.pool.connection() as conn:
                await sensors.error(conn, "tasks:slack", "A task message waits for a Slack retry.")


async def _can_review(conn, workspace, row, person):
    ref = "person:" + person["key"]
    return person["active"] and (person["role"] == "owner" or ref == await ledger.reviewer(conn, workspace, row))


async def handle(ctx, person, action):
    parts = action.get("action_id", "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"done", "back"}:
        return
    task_id, operation = int(parts[1]), parts[2]
    viewer = ledger.Viewer(person["key"], "person:" + person["key"], human=True)
    notice = None
    async with ctx.pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(12000001)")
        row = await ledger.get(conn, task_id, viewer=viewer, lock=True)
        card = await kv.get(conn, f"task:card:{task_id}")
        if not row or row["state"] != "review" or not card or not await _can_review(conn, ctx.workspace, row, person):
            return
        if action.get("channel") != card.get("channel", row["slack_channel"]) or action.get("message_ts") != card["ts"]:
            return
        if not row["slack_channel"].startswith("D") and await channel_private(ctx, row["slack_channel"], conn=conn):
            return
        try:
            if operation == "done":
                await ledger.done(conn, task_id, workspace=ctx.workspace, viewer=viewer, ctx=ctx)
            else:
                await kv.set(conn, f"task:back:{task_id}:{person['id']}", card["ts"])
                notice = "Reply in this thread with one line that says why the task needs more work."
        except ledger.TaskError as exc:
            notice = str(exc)
        await audit.write(conn, principal=viewer.actor, tool="task_" + operation,
                          args={"id": task_id}, decision="deny" if notice and operation == "done" else "allow",
                          reason=notice or "The person acts on the task card.", target=f"task {task_id}")
    if notice:
        await ctx.slack_client.chat_postMessage(channel=card.get("channel", row["slack_channel"]),
            thread_ts=card.get("thread", row["slack_ts"]), text=notice)
    await tick(ctx)


async def claim_back(ctx, event, authority):
    if event.channel != "slack" or authority.tainted or event.payload.get("contains_outside_text"):
        return None
    async with ctx.pool.connection() as conn:
        person = await people.by_slack_id(conn, event.sender)
        if not person:
            return None
        row = await _thread_task(conn, event.payload.get("channel_id"), event.payload.get("thread_ts") or event.reply_to)
        if not row or not await kv.get(conn, f"task:back:{row['id']}:{person['id']}"):
            return None
        viewer = ledger.Viewer(person["key"], "person:" + person["key"], human=True)
        task = await ledger.get(conn, row["id"], viewer=viewer)
        card = await kv.get(conn, f"task:card:{row['id']}")
        pending = await kv.get(conn, f"task:back:{row['id']}:{person['id']}")
        if not task or task["state"] != "review" or not card or pending != card["ts"] or not await _can_review(conn, ctx.workspace, task, person):
            return None
        if not event.text.strip():
            return "Give one line that says why the task needs more work."
        await ledger.reopen(conn, task["id"], workspace=ctx.workspace, viewer=viewer, reason=event.text)
        await kv.delete(conn, f"task:back:{row['id']}:{person['id']}")
        await audit.write(conn, principal=viewer.actor, tool="task_reopen", args={"id": row["id"]},
                          decision="allow", reason="The reviewer sends the task back.", target=f"task {row['id']}")
    await tick(ctx)
    return "The task goes back to its holder."


async def context(ctx, event):
    if event.payload.get("contains_outside_text") or event.payload.get("bot") or event.payload.get("outside_workspace"):
        return None
    async with ctx.pool.connection() as conn:
        person = await people.by_slack_id(conn, event.sender)
        if not person:
            return None
        viewer = ledger.Viewer(person["key"], "person:" + person["key"], human=True,
                               team_only=not event.payload["channel_id"].startswith("D"))
        ref = await _thread_task(conn, event.payload["channel_id"], event.payload.get("thread_ts") or event.reply_to)
        row = await ledger.get(conn, ref["id"], viewer=viewer) if ref else None
        if not row:
            return None
        marker = f"task:message:{event.external_id}"
        if not await kv.get(conn, marker):
            await ledger.post(conn, row["id"], workspace=ctx.workspace, viewer=viewer, text=event.text)
            await kv.set(conn, marker, True)
        row = await ledger.get(conn, row["id"], viewer=viewer)
        value = {key: row[key] for key in ("id", "title", "list_key", "holder", "state", "waits_for")}
        value["events"] = [{key: (entry[key][:200] if key == "text" else entry[key])
                            for key in ("actor", "kind", "text")}
                           for entry in await ledger.events(conn, row["id"], viewer=viewer, limit=10)]
        return data_text("task-context", json_text(value))


async def setup(ctx):
    ctx.slack_actions.register("task:", handle)
    ctx.gateway.register_claim(claim_back)

    async def check_tasks():
        await tick(ctx)

    ctx.every(2, check_tasks)
