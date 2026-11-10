"""Keep private channel text out of the shared ledger."""

import asyncio


async def channel_private(ctx, channel_id: str) -> bool:
    channel = next(c for c in ctx.workspace.config.slack.channels if c.id == channel_id)
    if channel.visibility == "private":
        return True
    response = await ctx.slack_client.conversations_info(channel=channel_id)
    private = response.get("channel", {}).get("is_private")
    if type(private) is not bool:
        raise ValueError("Slack did not report channel privacy")
    return private


async def hide_index(ctx, channel_id: str) -> None:
    """Old snapshots have no membership proof. Delete this derived index."""
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "DELETE FROM items "
            "WHERE kind='slack' AND starts_with(source_ref,%s)",
            (f"slack:{channel_id}:",),
        )


async def require_member(ctx, channel_id: str, slack_id: str) -> None:
    """Read live membership on every call, including all pages."""
    cursor, seen = None, set()
    async with asyncio.timeout(15):
        while True:
            response = await ctx.slack_client.conversations_members(
                channel=channel_id, limit=200, cursor=cursor
            )
            if slack_id in response.get("members", []):
                return
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            if cursor in seen:
                raise ValueError("Slack membership pagination did not finish")
            seen.add(cursor)
    raise ValueError("The person is not a member of this private channel")
