"""Map authenticated Slack clicks to the shared action router."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from illo.app import AppContext

from illo.db import one
from illo.ledger import people


async def dispatch(ctx: "AppContext", payload: dict) -> None:
    user_id = payload.get("user", {}).get("id", "")
    async with ctx.pool.connection() as conn:
        person = await people.by_slack_id(conn, user_id)
    if not person:
        return
    channel = payload.get("channel", {}).get("id") or payload.get("container", {}).get("channel_id")
    message = payload.get("message", {})
    message_ts = message.get("ts") or payload.get("container", {}).get("message_ts")
    for raw in payload.get("actions", []):
        action = {
            "action_id": raw.get("action_id", ""),
            "value": raw.get("value", ""),
            "channel": channel,
            "message_ts": message_ts,
            "thread_ts": message.get("thread_ts"),
            "trigger_id": payload.get("trigger_id", ""),
            "user_id": user_id,
        }
        # Slack retries the same click with a new envelope. Claim its stable timestamp.
        key = f"slack:action:{user_id}:{channel}:{message_ts}:{action['action_id']}:{raw.get('action_ts', payload.get('trigger_id', ''))}"
        async with ctx.pool.connection() as conn:
            claimed = await one(
                conn,
                "INSERT INTO kv (key,value) VALUES (%s,'true') "
                "ON CONFLICT (key) DO NOTHING RETURNING key",
                (key,),
            )
        if not claimed:
            continue
        try:
            await ctx.slack_actions.dispatch(ctx, person, action)
        except Exception:
            async with ctx.pool.connection() as conn:
                await conn.execute("DELETE FROM kv WHERE key=%s", (key,))
            raise
