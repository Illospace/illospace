"""Only the saver or an owner can correct or forget a memory change."""

import re
from datetime import UTC, datetime, timedelta

from illo.asks.cards import escape
from illo.db import one
from illo.ledger import audit, kv, people, sensors
from illo.memory import files, saves
from illo.notes.files import file_path
from illo.tools.registry import Refusal

CORRECT = "Reply in this thread with the correct text."
EXPIRED = "These buttons expired after 7 days. Tell Illo the change in words."


def card(row):
    text = f"Saved to memory: {row['title']}."
    return text, [
        {"type": "section", "text": {"type": "plain_text", "text": text[:3000]}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": label},
             "action_id": f"memory:{row['id']}:{choice}", "value": str(row["id"])}
            for choice, label in [("correct", "Correct"), ("forget", "Forget")]
        ]},
    ]


async def notice(ctx, conn, row, channel, thread):
    text, blocks = card(row)
    try:
        sent = await ctx.slack_client.chat_postMessage(
            channel=channel, thread_ts=thread, text=escape(text), blocks=blocks,
            unfurl_links=False, unfurl_media=False,
        )
    except Exception:
        await sensors.error(conn, "memory", "The memory is saved. The notice could not be sent.")
        return
    await conn.execute("""UPDATE memory_changes SET slack_channel=%s,slack_ts=%s,thread_ts=%s
        WHERE id=%s""", (channel, sent["ts"], thread or sent["ts"], row["id"]))
    await kv.set(conn, f"slack:thread:{channel}:{thread or sent['ts']}", True)


async def edit(ctx, conn, row, text, *, controls=False):
    try:
        await ctx.slack_client.chat_update(channel=row["slack_channel"], ts=row["slack_ts"],
                                            text=text, blocks=card(row)[1] if controls else [])
    except Exception:
        await sensors.error(conn, "memory", "The memory changed. The notice could not be updated.")


async def reply(ctx, row, text):
    await ctx.slack_client.chat_postMessage(channel=row["slack_channel"],
        thread_ts=row["thread_ts"] or row["slack_ts"], text=text)


async def authorized(conn, row, person):
    active = await people.get(conn, person["key"])
    if not active or active["id"] != person["id"]:
        return None
    return active if active["id"] == row["person_id"] or active["role"] == "owner" else None


async def refusal(conn, row):
    saver = await one(conn, "SELECT name FROM people WHERE id=%s", (row["person_id"],))
    return f"Only {saver['name']} or an owner can change this."


def expired(row):
    return datetime.now(UTC) >= row["created_at"] + timedelta(days=7)


def current(ctx, row):
    folder, name = ("notes", row["target"]) if row["kind"] == "note" else ("lists", row["target"] + ".md")
    path = file_path(ctx.workspace.root, folder, name)
    if not path.exists() or path.read_text() != row["after_text"]:
        raise Refusal("This memory changed after this save. Tell Illo the change in words.")


async def forget(ctx, conn, row, person):
    async with ctx.reload_lock:
        current(ctx, row)
        text = None if row["kind"] == "note" else row["before_text"]
        if row["kind"] == "list" and text is None:
            used = await one(conn, """SELECT id FROM tasks WHERE list_key=%s
                AND state NOT IN ('done','cancelled') LIMIT 1""", (row["target"],))
            if used:
                raise Refusal("This list has open tasks. Move or finish them before you forget it.")
        await saves.persist(ctx, conn, row["kind"], row["target"], text,
                            f"memory: forget {row['kind']} {row['title'] if row['kind'] == 'note' else row['target']}")
        await conn.execute("""UPDATE memory_changes SET state='forgotten',correction_ts=NULL,
            updated_at=now() WHERE id=%s""", (row["id"],))
        await audit.write(conn, principal="person:" + person["key"], tool="memory_forget",
                          args={}, decision="allow", reason="The memory change is forgotten.",
                          target=f"memory:{row['id']}")
    return "Forgotten." if text is None else "Restored the earlier text."


async def action(ctx, person, action):
    match = re.fullmatch(r"memory:([1-9][0-9]*):(correct|forget)", action.get("action_id", ""))
    if not match or not ctx.slack_client:
        return
    change_id, choice = match.groups()
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM memory_changes WHERE id=%s FOR UPDATE", (int(change_id),))
        if not row or row["slack_channel"] != action.get("channel") or row["slack_ts"] != action.get("message_ts"):
            return
        active = await authorized(conn, row, person)
        if not active:
            await ctx.slack_client.chat_postEphemeral(channel=row["slack_channel"],
                user=person["slack_id"], text=await refusal(conn, row))
            return
        if expired(row):
            await reply(ctx, row, EXPIRED)
            return
        if row["state"] == "forgotten":
            return
        if choice == "forget":
            try:
                text = await forget(ctx, conn, row, active)
            except Refusal as exc:
                await reply(ctx, row, str(exc))
            else:
                await edit(ctx, conn, row, text)
        else:
            if row["correction_ts"]:
                return
            thread = row["thread_ts"] or row["slack_ts"]
            # Serialize two Correct buttons in the same thread.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                               (row["slack_channel"] + ":" + thread,))
            pending = await one(conn, """SELECT id FROM memory_changes
                WHERE slack_channel=%s AND correction_ts=%s""", (row["slack_channel"], thread))
            if pending:
                await reply(ctx, row, "Finish the current correction in this thread first.")
                return
            await conn.execute("UPDATE memory_changes SET correction_ts=%s WHERE id=%s", (thread, row["id"]))
            await reply(ctx, row, CORRECT)


async def claim(ctx, event, authority):
    if (event.channel != "slack" or authority.tainted or not event.text.strip()
            or authority.profile not in {"person_dm", "team_channel"}
            or any(event.payload.get(k) for k in ("bot", "bot_id", "webhook", "form", "outside_workspace", "contains_outside_text"))):
        return None
    thread = event.payload.get("thread_ts") or event.reply_to
    channel = event.payload.get("channel_id")
    if not thread or not channel:
        return None
    async with ctx.pool.connection() as conn:
        person = await people.by_slack_id(conn, event.sender)
        if not person:
            return None
        row = await one(conn, """SELECT * FROM memory_changes WHERE slack_channel=%s
            AND correction_ts=%s AND state<>'forgotten' FOR UPDATE""", (channel, thread))
        if not row:
            return None
        if not await authorized(conn, row, person):
            return await refusal(conn, row)
        if expired(row):
            await conn.execute("UPDATE memory_changes SET correction_ts=NULL WHERE id=%s", (row["id"],))
            return EXPIRED
        if len(event.text) > 20000:
            return "The text is too long. Use at most 20000 characters."
        try:
            await saves.limit(conn, person)
            async with ctx.reload_lock:
                current(ctx, row)
                front, _ = files.split(row["after_text"])
                after = files.render(front, event.text)
                await saves.persist(ctx, conn, row["kind"], row["target"], after,
                    f"memory: correct {row['kind']} {row['title'] if row['kind'] == 'note' else row['target']}")
                await saves.record(ctx, conn, kind=row["kind"], target=row["target"], title=row["title"],
                    before=row["after_text"], after=after, person=person, run_id=None,
                    state="corrected", tool="memory_correct")
                await conn.execute("""UPDATE memory_changes SET after_text=%s,state='corrected',
                    correction_ts=NULL,updated_at=now() WHERE id=%s""", (after, row["id"]))
        except Refusal as exc:
            return str(exc)
        await edit(ctx, conn, {**row, "after_text": after}, "Saved the correct text.", controls=True)
        return "Saved the correct text."
