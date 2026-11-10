"""Build a person's digest without a model, and send it in their time zone."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

from illo.asks.cards import escape, links_text, relative, section
from illo.asks.service import post_dm
from illo.ledger import ask_workflow as ledger
from illo.ledger import people


def card_link(row: dict) -> str:
    if row.get("slack_channel") and row.get("slack_ts"):
        return f"<slack://channel?id={escape(row['slack_channel'])}&message={escape(row['slack_ts'])}|Card>"
    return ""


def build_digest(person: dict) -> list[dict]:
    """Build blocks from a snapshot. This function has no IO or clock reads."""
    asks = person.get("asks", [])
    running = person.get("running", 0)
    first = (
        f"{len(asks)} need you · {running} run fine"
        if asks
        else f"Nothing needs you · {running} run fine"
    )
    blocks = [section(first)]
    now = person.get("now")
    for ask in asks:
        details = [escape(ask["question"]), escape(ask.get("lane_title") or "Ask")]
        if now and ask.get("created_at"):
            details.append(relative(ask["created_at"], now))
        details.append(card_link(ask))
        blocks.append(section(" · ".join(value for value in details if value)))
    finished = person.get("finished", [])[:5]
    blocks.append(section("Finished since the last digest:"))
    for lane in finished:
        blocks.append(
            section(f"{escape(lane['title'])} {links_text(lane.get('links', []))}".strip())
        )
    problems = [
        f"{escape(s['key'])}: {escape(s.get('detail') or 'No recent good report.')}"
        for s in person.get("sensors", [])
    ]
    problems += [
        f"Answer {a['id']} did not reach its agent: {escape(a['question'])}"
        for a in person.get("failed", [])
    ]
    if problems:
        blocks.append(section("Not healthy:\n" + "\n".join(problems)))
    # Slack permits 50 blocks. Keep the complete digest as text when it needs more.
    if len(blocks) > 50:
        text = "\n".join(block["text"]["text"] for block in blocks)
        blocks = [section(text[i : i + 3000]) for i in range(0, len(text), 3000)][:50]
    return blocks


async def snapshot(conn, person, ctx, now=None):
    return await ledger.digest_data(
        conn,
        {**person, "stale_minutes": ctx.workspace.config.stale_minutes.model_dump(by_alias=True)},
        now or datetime.now(UTC),
    )


async def send_due(ctx, now=None):
    if not ctx.slack_client:
        return
    now = now or datetime.now(UTC)
    async with ctx.pool.connection() as conn:
        members = await people.list_people(conn)
    for person in members:
        local = now.astimezone(ZoneInfo(person["timezone"]))
        # A 30-second timer can recover a missed minute, but never yesterday's digest.
        due = croniter(
            ctx.workspace.config.digest.schedule, local + timedelta(microseconds=1)
        ).get_prev(datetime)
        if local - due > timedelta(minutes=2):
            continue
        async with ctx.pool.connection() as conn:
            await ledger.lock_digest(conn, person["key"])
            last = await ledger.digest_last_sent(conn, person["key"])
            if last and last >= due:
                continue
            data = await snapshot(conn, person, ctx, now)
            blocks = build_digest(data)
            sent = await post_dm(
                ctx,
                person,
                blocks[0]["text"]["text"],
                blocks,
                "digest",
                f"{person['id']}:{due.isoformat()}",
            )
            if sent:
                await ledger.digest_sent(conn, person["key"], now)


async def setup(ctx):
    async def check_digests():
        await send_due(ctx)

    ctx.every(30, check_digests)
