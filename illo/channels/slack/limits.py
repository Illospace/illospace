"""Bound automation runs and owner notices with durable, serialized counters."""

from datetime import UTC, datetime

from illo.db import all_rows, one
from illo.ledger import drafts, kv, people, runs, sensors

NOTICE_PREFIX = "slack:owner-notices:"


def now():
    return datetime.now(UTC)


async def send_owner(ctx, conn, owner, text):
    """Reuse the caller's connection, including when the pool has one connection."""
    person = await people.get(conn, owner)
    if not ctx.slack_client or not person or not person.get("slack_id"):
        raise ValueError("Slack is not connected to the owner")
    opened = await ctx.slack_client.conversations_open(users=person["slack_id"])
    return await ctx.slack_client.chat_postMessage(channel=opened["channel"]["id"], text=text)


async def lock(conn, key):
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))


async def admit_tainted_run(ctx, event, authority, event_id):
    """Count admissions from events, including concurrent events and thread taint."""
    if event.channel != "slack" or not authority.tainted:
        return True
    channel = event.payload["channel_id"]
    key = f"slack:automation:{channel}"
    async with ctx.pool.connection() as conn:
        await lock(conn, key)
        current = await one(conn, "SELECT payload FROM events WHERE id=%s", (event_id,))
        if current["payload"].get("automation_admitted"):
            return True
        count = await one(conn, """
            SELECT count(*) AS n FROM events
            WHERE channel='slack' AND tainted AND created_at > now()-interval '1 hour'
              AND payload->'payload'->>'channel_id'=%s
              AND payload->>'automation_admitted'='true'
        """, (channel,))
        if count["n"] >= 30:
            await conn.execute("UPDATE events SET status='ignored' WHERE id=%s", (event_id,))
            reported = await one(conn, """
                SELECT 1 FROM sensors WHERE key=%s AND last_error_at>now()-interval '1 hour'
            """, (key,))
            if not reported:
                await sensors.error(conn, key, "The channel reached 30 tainted runs in one hour")
            return False
        await conn.execute("""
            UPDATE events SET payload=payload || '{"automation_admitted":true}'::jsonb
            WHERE id=%s
        """, (event_id,))
    return True


def fresh_state(state, timestamp):
    state = state or {"sent": [], "pending": [], "due_at": None}
    state["sent"] = [at for at in state["sent"] if at > timestamp - 3600]
    return state


async def owner_draft(ctx, conn, run, issue, repo, event):
    """Code proposes one fixed assignment. The owner still approves the stored action."""
    from illo.asks.drafts import sync_card

    person = await people.get(conn, ctx.workspace.config.default_owner)
    if not person or not person.get("github_login"):
        raise ValueError("The default owner needs a GitHub login")
    source = {"kind": "unknown"}
    if event and event["channel"] == "signals":
        source = {"kind": "signal", "name": event["payload"]["payload"].get("source", "unknown")}
    elif event and event["channel"] == "slack":
        payload = event["payload"]["payload"]
        channel = (ctx.workspace.config.slack.channel(payload["channel_id"])
                   if payload.get("channel_id", "").startswith(("C", "G")) else None)
        author = await people.by_slack_id(conn, payload.get("author_slack_id", ""))
        if not author and event.get("person_id"):
            author = await one(conn, "SELECT name FROM people WHERE id=%s AND active", (event["person_id"],))
        from illo.channels.slack.channels import member_channels

        try:
            names = await member_channels(ctx, conn=conn) if channel else {}
        except ValueError:
            names = {}
        source = {"kind": "slack", "name": names.get(channel.id, channel.name) if channel else "DM",
                  "author": author["name"] if author and not payload.get("from_automation") else "Automation"}
        try:
            link = await ctx.slack_client.chat_getPermalink(
                channel=payload["channel_id"], message_ts=payload["message_ts"])
            source["link"] = link["permalink"]
        except Exception:
            pass
    # A separate, tokenless run keeps every existing requester/approver check intact.
    owner_run = await runs.create(conn, principal=f"person:{person['key']}",
                                  person_id=person["id"], profile="person_dm",
                                  runtime="code", model="code", event_id=run.get("event_id"))
    await runs.finish(conn, owner_run["id"])
    draft = await drafts.create(
        conn, run_id=owner_run["id"], approver_person_id=person["id"],
        kind="github_assign_issue", destination=f"github:{repo}",
        content={"tool": "github_assign_issue", "arguments": {
            "repo": repo, "number": issue["number"], "assignee": person["github_login"]},
            "github_issue": {"title": issue["title"], "link": issue["html_url"],
                             "assignees": [p["login"] for p in issue.get("assignees", [])]},
            "outside_ticket": {"owner": person["name"], "source": source, "run_id": run["id"]}},
    )
    if not await sync_card(ctx, conn, draft):
        raise ValueError("Slack is not connected to the owner")
    return draft


async def notify_unassigned(ctx, conn, run, issue, repo, event=None):
    """Send one immediate card per run, with at most six cards per hour."""
    owner = ctx.workspace.config.default_owner
    key = NOTICE_PREFIX + owner
    await lock(conn, key)
    timestamp = now().timestamp()
    state = fresh_state(await kv.get(conn, key), timestamp)
    ticket = {"url": issue["html_url"], "title": issue["title"],
              "repo": repo, "number": issue["number"]}
    run_key = f"slack:owner-notified-run:{run['id']}"
    deferred = bool(await kv.get(conn, run_key)) or len(state["sent"]) >= 6
    if not deferred:
        await kv.set(conn, run_key, True)
        state["sent"].append(timestamp)
        await kv.set(conn, key, state)
        try:
            await owner_draft(ctx, conn, run, issue, repo, event)
            if not ctx.slack_client:
                raise ValueError("Slack is not connected")
        except Exception:
            await sensors.error(conn, "github:owner-notice", "The owner card could not be sent")
            deferred = True
    if deferred:
        if not any((p if isinstance(p, str) else p["url"]) == ticket["url"] for p in state["pending"]):
            state["pending"].append(ticket)
        if state["due_at"] is None:
            state["due_at"] = min(state["sent"], default=timestamp) + 3600
    await kv.set(conn, key, state)


def pending_line(ticket):
    from illo.asks.cards import escape

    if isinstance(ticket, str):
        parts = ticket.rstrip("/").split("/")
        ticket = {"url": ticket, "repo": parts[-3] if len(parts) >= 4 else "ticket",
                  "number": parts[-1], "title": ""}
    label = f"{ticket['repo'].split('/')[-1]}#{ticket['number']}"
    return f"• <{escape(ticket['url']).replace('|', '%7C')}|{escape(label)}> {escape(ticket['title'])}".rstrip()


async def flush_owner_notices(ctx):
    """Send one digest when the notice window ends. Keep its links after a failure."""
    async with ctx.pool.connection() as conn:
        owners = await all_rows(conn, "SELECT key FROM kv WHERE starts_with(key,%s)", (NOTICE_PREFIX,))
    for row in owners:
        async with ctx.pool.connection() as conn:
            key = row["key"]
            await lock(conn, key)
            timestamp = now().timestamp()
            state = fresh_state(await kv.get(conn, key), timestamp)
            if not state["pending"] or state["due_at"] > timestamp or len(state["sent"]) >= 6:
                continue
            state["sent"].append(timestamp)
            try:
                sent = await send_owner(ctx, conn, key[len(NOTICE_PREFIX):],
                                       "Tickets that wait for an owner:\n"
                                       + "\n".join(pending_line(t) for t in state["pending"])
                                       + "\nTell me here which ones to assign.")
                if not sent:
                    raise ValueError("Slack is not connected")
            except Exception:
                await sensors.error(conn, "github:owner-notice", "The owner digest could not be sent")
                state["due_at"] = timestamp + 3600
                await kv.set(conn, key, state)
                continue
            state["pending"] = []
            state["due_at"] = None
            await kv.set(conn, key, state)
