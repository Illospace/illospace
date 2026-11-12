"""Code decides whether a new ticket has a trusted machine source."""

from illo.channels.slack.limits import lock, now
from illo.db import one
from illo.ledger import audit, kv, people


async def source_event(ctx):
    if not ctx.run.get("event_id"):
        return None
    return await one(ctx.conn, "SELECT * FROM events WHERE id=%s", (ctx.run["event_id"],))


def trusted(ctx, event):
    if not event:
        return False
    config = ctx.app.workspace.config
    payload = event["payload"].get("payload", {})
    if event["channel"] == "signals" and event["kind"] == "signal":
        return payload.get("source") in config.triage.auto_assign.signal_sources
    if event["channel"] != "slack" or event["kind"] != "message":
        return False
    channel = config.slack.channel(payload.get("channel_id") or "unknown")
    # A stored thread taint marker has no author proof. Keep these tickets for the owner.
    return bool(
        payload.get("from_automation") is True
        and payload.get("bot_id") in config.triage.auto_assign.slack_bots
        and payload.get("outside_workspace") is False
        and not payload.get("contains_outside_text")
        and not payload.get("thread_ts")
        and not payload.get("form")
        and channel.listed and channel.mode != "off" and channel.visibility == "team"
    )


async def auto_assign(ctx, event, repo, number):
    if not trusted(ctx, event):
        return None
    owner = await people.get(ctx.conn, ctx.app.workspace.config.default_owner)
    if not owner or not owner.get("github_login"):
        return None
    key = "triage:auto-assign"
    await lock(ctx.conn, key)
    timestamp = now().timestamp()
    sent = [at for at in (await kv.get(ctx.conn, key) or []) if at > timestamp - 3600]
    if len(sent) >= ctx.app.workspace.config.triage.max_per_hour:
        return None
    # Count attempts too. An uncertain GitHub response must not permit an alert storm.
    await kv.set(ctx.conn, key, [*sent, timestamp])
    try:
        await ctx.app.github_client.assign_issue(repo, number, [owner["github_login"]])
    except Exception:
        return None
    await audit.write(ctx.conn, run_id=ctx.run["id"], principal=ctx.run["principal"],
                      tool="github_assign_issue", args={"repo": repo, "number": number,
                      "assignee": owner["github_login"]}, decision="allow",
                      reason="Automatic assignment for a trusted monitor")
    return owner["github_login"]
