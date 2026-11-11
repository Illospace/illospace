"""Read the last thread messages and name their authors."""

import asyncio
from collections import deque
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from illo.channels.slack.blocks import message_text
from illo.ledger import kv, people, sensors


async def read_context(ctx, event, bot_id: str, working_ts: str | None) -> list[str]:
    """Use pagination so a long thread still gives the most recent messages."""
    messages = deque(maxlen=20)
    async with asyncio.timeout(15):
        cursor = None
        while True:
            response = await ctx.slack_client.conversations_replies(
                channel=event.payload["channel_id"],
                ts=event.payload["thread_ts"],
                limit=100,
                cursor=cursor,
                latest=event.payload["message_ts"],
                inclusive=False,
            )
            messages.extend(
                message
                for message in response.get("messages", [])
                if message.get("ts") not in {event.payload["message_ts"], working_ts}
            )
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        async with ctx.pool.connection() as conn:
            tainted = bool(await kv.get(
                conn,
                f"slack:thread-tainted:{event.payload['channel_id']}:{event.payload['thread_ts']}",
            ))
            names = {
                person["slack_id"]: person["name"] for person in await people.list_people(conn)
            }
        names[bot_id] = "Illo"
        if tainted:
            event.payload["contains_outside_text"] = True
        context = []
        for message in messages:
            user = message.get("user", "")
            if user != bot_id and (
                message.get("bot_id") or message.get("subtype") or not user or user not in names
            ):
                event.payload["contains_outside_text"] = True
            if user and user not in names:
                names[user] = f"Outside user ({user})"
            context.append(f"{names.get(user, 'Automation')}: {message_text(message)}")
    return context


async def read_dm_history(ctx, event, bot_id: str, working_ts: str | None,
                          *, bot_identity: str | None = None) -> list[str]:
    """Read bounded DM context. A missing Slack scope must not stop the request."""
    try:
        async with asyncio.timeout(15):
            response = await ctx.slack_client.conversations_history(
                channel=event.payload["channel_id"],
                latest=event.payload["message_ts"], inclusive=False, limit=30,
            )
            if response.get("ok") is False:
                raise ValueError("Slack refused the history read")
            async with ctx.pool.connection() as conn:
                persons = {p["slack_id"]: p for p in await people.list_people(conn)}
            sender = persons.get(event.sender)
            timezone = ZoneInfo(sender["timezone"] if sender else "UTC")
            messages = [m for m in response.get("messages", [])
                        if m.get("ts") not in {working_ts, event.payload["message_ts"]}
                        and m.get("subtype") in {None, "bot_message", "file_share", "thread_broadcast"}]
            context = []
            outside = False
            for message in reversed(messages[:20]):
                user = message.get("user")
                own = user == bot_id or bool(bot_identity and message.get("bot_id") == bot_identity)
                person = persons.get(user)
                if own:
                    name = "Illo"
                elif person and not message.get("bot_id"):
                    name = person["name"]
                else:
                    name = "Automation"
                    outside = True
                text = message_text(message)
                if len(text) > 1500:
                    text = text[:1498] + " …"
                local = datetime.fromtimestamp(float(message["ts"]), UTC).astimezone(timezone)
                context.append(f"[{local:%Y-%m-%d %H:%M}] {name}: {text}")
            while len("\n".join(context)) > 6000:
                context.pop(0)
            if outside:
                event.payload["contains_outside_text"] = True
    except Exception:
        async with ctx.pool.connection() as conn:
            await sensors.error(conn, "slack:dm-history",
                                "Slack could not read the DM history. Check the `im:history` scope.")
        return []
    async with ctx.pool.connection() as conn:
        await sensors.ok(conn, "slack:dm-history")
    return context
