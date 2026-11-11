"""Keep private channel text out of the shared ledger."""

import asyncio
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

from illo.ledger import kv, people, sensors


async def team_members(ctx, conn, channel_id, bot_id, verdicts):
    """The humans in the channel must be exactly the active people. Read every page."""
    known = {p["slack_id"] for p in await people.list_people(conn) if p.get("slack_id")}
    cursor, seen, present = None, set(), set()
    while True:
        response = await ctx.slack_client.conversations_members(channel=channel_id, limit=200, cursor=cursor)
        if response.get("ok") is False or not isinstance(response.get("members"), list):
            raise ValueError("Slack did not report channel members")
        for member in response["members"]:
            if member in known:
                present.add(member)
                continue
            if member == bot_id:
                continue
            if member not in verdicts:
                user = await ctx.slack_client.users_info(user=member)
                if user.get("ok") is False:
                    raise ValueError("Slack did not report the user")
                verdicts[member] = user.get("user", {}).get("is_bot") is True
            if not verdicts[member]:
                return False
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            # A person who is not a member must not read the channel through the team index.
            return present == known
        if cursor in seen:
            raise ValueError("Slack membership pagination did not finish")
        seen.add(cursor)


async def channel_private(ctx, channel_id: str, *, conn=None, bot_id=None) -> bool:
    channel = next(c for c in ctx.workspace.config.slack.channels if c.id == channel_id)
    if channel.visibility == "private":
        if channel.team_members_only:
            await hide_index(ctx, channel_id, conn=conn)
        return True
    key = f"slack:privacy:{channel_id}"
    async with (nullcontext(conn) if conn is not None else ctx.pool.connection()) as conn:
        # One probe per channel also covers concurrent messages in an alert storm.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
        cached = await kv.get(conn, key)
        # A config change must not reuse a public verdict from another policy.
        if (cached and cached.get("team_members_only", False) == channel.team_members_only
                and datetime.fromisoformat(cached["expires_at"]) > datetime.now(UTC)):
            return cached["private"]
        verdicts = dict((cached or {}).get("bots", {}))
        bot_user = bot_id or (cached or {}).get("bot_user")
        try:
            async with asyncio.timeout(15):
                response = await ctx.slack_client.conversations_info(channel=channel_id)
                private = response.get("channel", {}).get("is_private")
                if response.get("ok") is False or type(private) is not bool:
                    raise ValueError("Slack did not report channel privacy")
                if private and channel.team_members_only:
                    if not bot_user:
                        bot_user = (await ctx.slack_client.auth_test())["user_id"]
                    private = not await team_members(ctx, conn, channel_id, bot_user, verdicts)
        except Exception:
            # No verdict and no cache. The caller records the missed event, and the index stays.
            await sensors.error(conn, key, "Slack could not verify channel privacy")
            failed = True
        else:
            failed = False
        if not failed:
            await kv.set(conn, key, {
                "private": private, "team_members_only": channel.team_members_only,
                "bots": verdicts, "bot_user": bot_user,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            })
            if private and channel.team_members_only:
                await sensors.error(conn, key,
                    f"The members of #{channel.name} are not exactly the people in people.yaml. Illo treats it as private.")
                await hide_index(ctx, channel_id, conn=conn)
            else:
                await sensors.ok(conn, key)
    if failed:
        raise ValueError("Slack could not verify channel privacy")
    return private


async def hide_index(ctx, channel_id: str, *, conn=None) -> None:
    """Old snapshots have no membership proof. Delete this derived index."""
    async with (nullcontext(conn) if conn is not None else ctx.pool.connection()) as conn:
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
