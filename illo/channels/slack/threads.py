"""Read the last thread messages and name their authors."""

import asyncio
from collections import deque

from illo.ledger import kv, people


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
            context.append(f"{names.get(user, 'Automation')}: {message.get('text', '')}")
    return context
