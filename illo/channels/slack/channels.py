"""Cache the channels that Illo can use. Keep names out of private-channel errors."""

import asyncio
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

from illo.ledger import kv, sensors

KEY = "slack:member-channels"


class ChannelLookupError(ValueError):
    """The caller can show this fixed message without a channel name."""


async def member_channels(ctx, *, conn=None):
    """Read all pages at most once in ten minutes, including after an API failure."""
    failed = False
    async with (nullcontext(conn) if conn is not None else ctx.pool.connection()) as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (KEY,))
        cached = await kv.get(conn, KEY)
        if cached and datetime.fromisoformat(cached["expires_at"]) > datetime.now(UTC):
            failed = cached.get("failed", False)
            channels = cached.get("channels", {})
        else:
            channels = {}
            try:
                if not ctx.slack_client:
                    raise ValueError("Slack is not connected")
                async with asyncio.timeout(15):
                    cursor, seen = None, set()
                    while True:
                        response = await ctx.slack_client.users_conversations(
                            types="public_channel,private_channel", exclude_archived=True,
                            limit=200, cursor=cursor,
                        )
                        if response.get("ok") is False or not isinstance(response.get("channels"), list):
                            raise ValueError("Slack did not report member channels")
                        for channel in response["channels"]:
                            if channel.get("is_member") is False or channel.get("is_archived"):
                                continue
                            channels[channel["id"]] = channel["name"]
                        cursor = response.get("response_metadata", {}).get("next_cursor")
                        if not cursor:
                            break
                        if cursor in seen:
                            raise ValueError("Slack channel pagination did not finish")
                        seen.add(cursor)
            except Exception:
                failed = True
                channels = {}
                await sensors.error(conn, KEY, "Slack could not read member channels")
            else:
                await sensors.ok(conn, KEY)
            await kv.set(conn, KEY, {
                "channels": channels, "failed": failed,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            })
    if failed:
        raise ValueError("Slack could not read member channels. Try again later.")
    return channels


async def resolve_channel(ctx, value, *, conn=None):
    """Resolve a configured name or a member channel name or ID."""
    value = value.removeprefix("#")
    for entry in ctx.workspace.config.slack.channels:
        if value in {entry.id, entry.name}:
            return ctx.workspace.config.slack.channel(entry.id)
    channels = await member_channels(ctx, conn=conn)
    channel_id = next((key for key, name in channels.items() if value in {key, name}), None)
    if channel_id:
        channel = ctx.workspace.config.slack.channel(channel_id)
        channel.name = channels[channel_id]
        return channel
    message = "Illo cannot find that channel. Add Illo to the channel and check its name or ID."
    async with (nullcontext(conn) if conn is not None else ctx.pool.connection()) as conn:
        await sensors.error(conn, "slack:channel-name", message)
    raise ChannelLookupError(message)
