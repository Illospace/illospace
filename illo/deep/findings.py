"""Save scoped findings and route their buttons through the normal gateway."""

import re
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from illo.asks.cards import escape
from illo.asks.service import message_id
from illo.channels.slack.access import require_member
from illo.channels.slack.adapter import SlackAdapter
from illo.db import all_rows, one
from illo.deep.delivery import destination
from illo.gateway import InboundEvent
from illo.ledger import people, sensors
from illo.tools.registry import Refusal


def scope(run):
    personal = run["principal"].startswith("person:")
    return ("person", run["person_id"]) if personal else ("team", None)


async def list_findings(ctx):
    kind, person_id = scope(ctx.run)
    return await all_rows(
        ctx.conn,
        """SELECT key,title,state,created_at FROM scout_findings
        WHERE scope=%s AND person_id IS NOT DISTINCT FROM %s
        AND created_at>clock_timestamp()-interval '90 days' ORDER BY created_at DESC""",
        (kind, person_id),
    )


def links_text(row):
    return " · ".join(f"<{escape(link['url'])}|{escape(link['title'])}>" for link in row["links"])


def blocks(row, text, *, controls=True):
    result = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if controls:
        result.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": label},
                        "action_id": f"scout:{row['id']}:{choice}",
                        "value": str(row["id"]),
                    }
                    for choice, label in [("task", "Make a task"), ("dismiss", "Not useful")]
                ],
            }
        )
    return result


async def message(ctx, conn, row, channel):
    prefix = ""
    if row["for_person"]:
        person = await people.get(conn, row["for_person"])
        if person:
            prefix = escape(person["name"]) + ": "
            if not channel.startswith("D") and person["slack_id"]:
                try:
                    await require_member(ctx, channel, person["slack_id"])
                except Exception:
                    pass
                else:
                    prefix = f"<@{person['slack_id']}>: "
    return prefix + escape(row["message"]) + "\n" + links_text(row)


async def deliver(ctx, conn, row):
    deep = await one(conn, "SELECT * FROM deep_runs WHERE id=%s", (row["deep_run_id"],))
    target = deep["context"].get("scout_destination")
    if row["scope"] == "person":
        # The stored scope decides the destination, including after a config reload.
        target = {"slack_dm": {"person_id": row["person_id"]}}
    if not target:
        from illo.routines.scout import scout_destination

        target = await scout_destination(ctx, conn)
    if not ctx.slack_client:
        raise Refusal("Slack is not available.")
    channel, thread = await destination(ctx, conn, {**deep, "deliver": target})
    text = await message(ctx, conn, row, channel)
    sent = await ctx.slack_client.chat_postMessage(
        channel=channel,
        thread_ts=thread,
        text=text,
        blocks=blocks(row, text),
        client_msg_id=message_id("scout", row["id"]),
        unfurl_links=False,
        unfurl_media=False,
    )
    await conn.execute(
        "UPDATE scout_findings SET state='sent',slack_channel=%s,slack_ts=%s WHERE id=%s",
        (channel, sent["ts"], row["id"]),
    )
    return sent


async def add(ctx, args):
    kind, person_id = scope(ctx.run)
    if args.get("person") and not await people.get(ctx.conn, args["person"]):
        raise Refusal("The finding needs an active person.")
    previous = await one(
        ctx.conn,
        "SELECT state FROM scout_findings WHERE scope=%s AND person_id IS NOT DISTINCT FROM %s AND key=%s",
        (kind, person_id, args["key"]),
    )
    if previous:
        raise Refusal("This finding already exists. State: " + previous["state"] + ".")
    count = await one(
        ctx.conn,
        "SELECT count(*) AS n FROM scout_findings WHERE deep_run_id=%s",
        (ctx.run["deep_run_id"],),
    )
    if count["n"] >= ctx.app.workspace.config.scout.max_findings:
        raise Refusal("This scout reached its finding limit.")
    row = await one(
        ctx.conn,
        """INSERT INTO scout_findings(scope,person_id,key,title,message,links,for_person,deep_run_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *""",
        (
            kind,
            person_id,
            args["key"],
            args["title"],
            args["message"],
            Jsonb(args["links"]),
            args.get("person"),
            ctx.run["deep_run_id"],
        ),
    )
    if not row:
        previous = await one(
            ctx.conn,
            "SELECT state FROM scout_findings WHERE scope=%s AND person_id IS NOT DISTINCT FROM %s AND key=%s",
            (kind, person_id, args["key"]),
        )
        raise Refusal("This finding already exists. State: " + previous["state"] + ".")
    try:
        await deliver(ctx.app, ctx.conn, row)
    except Exception:
        await sensors.error(ctx.conn, "scout", "The finding is saved. Slack delivery will retry.")
    return {
        "id": row["id"],
        "state": "sent"
        if row.get("slack_ts")
        else (await one(ctx.conn, "SELECT state FROM scout_findings WHERE id=%s", (row["id"],)))[
            "state"
        ],
    }


def expired(row, now=None):
    return (now or datetime.now(UTC)) >= row["created_at"] + timedelta(days=14)


async def action(ctx, person, action):
    match = re.fullmatch(r"scout:([1-9][0-9]*):(task|dismiss)", action.get("action_id", ""))
    if not match or not ctx.slack_client:
        return
    finding_id, choice = match.groups()
    async with ctx.pool.connection() as conn:
        row = await one(
            conn, "SELECT * FROM scout_findings WHERE id=%s FOR UPDATE", (int(finding_id),)
        )
        active = await people.get(conn, person["key"])
        if (
            not row
            or not active
            or active["id"] != person["id"]
            or row["slack_channel"] != action.get("channel")
            or row["slack_ts"] != action.get("message_ts")
            or row["state"] not in {"new", "sent"}
            or row["scope"] == "person"
            and row["person_id"] != person["id"]
        ):
            return
        if expired(row):
            await ctx.slack_client.chat_update(
                channel=row["slack_channel"],
                ts=row["slack_ts"],
                text=await message(ctx, conn, row, row["slack_channel"]),
                blocks=[],
            )
            await ctx.slack_client.chat_postEphemeral(
                channel=row["slack_channel"],
                user=active["slack_id"],
                text="These buttons expired after 14 days. Tell Illo the request in words.",
            )
            return
        await conn.execute(
            "UPDATE scout_findings SET state=%s,decided_by=%s,decided_at=now() WHERE id=%s",
            ("task" if choice == "task" else "dismissed", person["id"], row["id"]),
        )
        text = await message(ctx, conn, row, row["slack_channel"])
    await ctx.slack_client.chat_update(
        channel=row["slack_channel"],
        ts=row["slack_ts"],
        text=text,
        blocks=blocks(row, text, controls=False),
    )
    if choice == "task":
        text = f"Make a task from this finding: {row['title']}. {row['message']} " + links_text(row)
        event = InboundEvent(
            channel="slack",
            external_id=f"scout-task:{row['id']}",
            kind="dm" if row["slack_channel"].startswith("D") else "message",
            sender=active["slack_id"],
            text=text,
            reply_to=row["slack_ts"],
            payload={
                "channel_id": row["slack_channel"],
                "thread_ts": row["slack_ts"],
                "mentioned": True,
            },
        )
        await ctx.gateway.receive(event, SlackAdapter(ctx))


async def retry(ctx):
    async with ctx.pool.connection() as conn:
        rows = await all_rows(
            conn,
            "SELECT * FROM scout_findings WHERE state='new' AND created_at>now()-interval '14 days' ORDER BY id FOR UPDATE SKIP LOCKED",
        )
        for row in rows:
            try:
                await deliver(ctx, conn, row)
            except Exception:
                await sensors.error(conn, "scout", "The saved finding could not be sent.")
