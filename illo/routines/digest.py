"""Send one model-written morning brief in each person's time zone."""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi.encoders import jsonable_encoder

from illo.asks.cards import escape, section
from illo.asks.service import post_dm
from illo.channels.slack.formatting import mrkdwn
from illo.db import one
from illo.gateway.events import InboundEvent
from illo.ledger import ask_workflow as ledger
from illo.ledger import briefs, people

DEFAULT_INSTRUCTIONS = """Write the morning brief for {name}. Call `digest_snapshot` first and use only its data.

Write like a colleague who respects {name}'s time.

- Start with "Morning, {name}." Then say the one thing that matters most, with the reason and the action.
- Order: first what needs {name}, then what is stuck, then what finished.
- An item with kind "your_move" is a thread where the agent stopped. Say that it waits for {name}, not that it is finished.
- One short sentence or one bullet for each item. Make the title a link if the item has a link. If not, write the title with no link. Rewrite a raw title into plain words: remove prefixes such as "docs(agents):" and tracker numbers that add nothing.
- Maximum 8 lines. If a group has more than 3 items, give the count and the 3 most important ones.
- Write "Nothing needs you" only if `needs_you` and `stuck` are both empty.
- Mention a fault only if `faults` is not empty. Say what does not work and what {name} can do. Never show a sensor key or an internal id.
- If nothing happened, send one line: "Morning, {name}. Nothing needs you, and nothing changed since the last brief."
- End with one short question that offers help: ask if {name} wants Illo to coordinate something today. Change the words each day.
- No headers, no table, no counts line."""


FIRST_BRIEF = (
    "I send you a short brief each morning. You can change the time, or turn it off: tell me here."
)


def linked(item):
    title = escape(item["title"])
    return f"<{escape(item['link'])}|{title}>" if item.get("link") else title


def fallback_text(data):
    """Use the same snapshot when the model cannot write the brief."""
    needs, stuck = data.get("needs_you", []), data.get("stuck", [])
    shown_moves = sum(item["kind"] == "your_move" for item in needs)
    needs_total = len(needs) + max(0, data.get("your_move_total", shown_moves) - shown_moves)
    count = needs_total + len(stuck)
    first = (
        "Nothing needs you."
        if not count
        else ("1 thing needs you." if count == 1 else f"{count} things need you.")
    )
    lines = [f"Morning, {escape(data['name'])}. {first}"]
    for item in needs[:5]:
        text = linked(item)
        if item["kind"] == "ask":
            text += ": " + escape(item["question"])
        elif item["kind"] == "failed_answer":
            text += ": The answer did not reach its agent. " + escape(item["question"])
        elif item["kind"] == "your_move":
            text += ": the agent stopped and waits for you."
        else:
            text += ": Please review."
        lines.append("• " + text)
    if needs_total > 5:
        lines.append(f"and {needs_total - 5} more")
    if stuck:
        lines.append("Stuck:")
        lines.extend(
            f"• {linked(item)} — no signal for {int(item['hours_since_signal'])} h"
            for item in stuck[:3]
        )
        if len(stuck) > 3:
            lines.append(f"and {len(stuck) - 3} more")
    if data.get("finished_total", 0):
        lines.append(f"Finished: {data['finished_total']}.")
        lines.extend("• " + linked(item) for item in data["finished"][:3])
    if data.get("faults"):
        lines.append("Not working:")
        lines.extend(escape(item["what"]) for item in data["faults"])
    lines.append("Do you want me to coordinate something today? Tell me here.")
    return "\n".join(lines)


def build_digest(data):
    """Keep one Slack message. Blocks contain the full fallback text."""
    return text_blocks(fallback_text(data))


def text_blocks(text):
    return [section(text[i : i + 3000]) for i in range(0, len(text), 3000)][:50]


async def snapshot(conn, person, ctx, now=None):
    return await ledger.digest_data(
        conn,
        {**person, "stale_minutes": ctx.workspace.config.stale_minutes.model_dump(by_alias=True)},
        now or datetime.now(UTC),
    )


class BriefAdapter:
    """Capture the final reply. Only send_due owns delivery and its retry key."""

    def __init__(self):
        self.text = None

    async def show_working(self, event):
        pass

    async def deliver_final(self, event, text):
        self.text = text

    async def deliver_error(self, event, *, category="internal"):
        pass


async def brief_text(ctx, person, data, due):
    instructions = ctx.workspace.digest_instructions
    if instructions is None:
        instructions = next((r.body for r in ctx.workspace.routines if r.name == "daily-digest"), None)
    prompt = (instructions or DEFAULT_INSTRUCTIONS).replace("{name}", person["name"])
    return await write_brief(ctx, "digest", f"{person['id']}:{due.isoformat()}", prompt,
                             f"person:{person['key']}", "digest_snapshot", data, fallback_text)


async def write_brief(ctx, channel, external_id, prompt, principal, snapshot_name, data, fallback):
    adapter = BriefAdapter()
    event = InboundEvent(
        channel=channel,
        external_id=external_id,
        kind="scheduled",
        text=prompt,
        payload={"principal": principal, snapshot_name: jsonable_encoder(data)},
    )
    try:
        async with asyncio.timeout(ctx.workspace.config.brain.timeout_s):
            result = await ctx.gateway.receive(event, adapter)
    except Exception:
        return fallback(data)
    if not result.get("error") and adapter.text and adapter.text.strip():
        return mrkdwn(adapter.text)
    # A process can stop after the model reply and before Slack delivery.
    if result.get("duplicate"):
        async with ctx.pool.connection() as conn:
            row = await one(
                conn,
                "SELECT status,payload FROM events WHERE channel=%s AND external_id=%s",
                (channel, event.external_id),
            )
        if row:
            if row["status"] == "handled" and row["payload"].get("final_answer"):
                return mrkdwn(row["payload"]["final_answer"])
            data = row["payload"]["payload"][snapshot_name]
    return fallback(data)


def is_digest(routine):
    return routine.name == "daily-digest" or routine.path in {
        "routines/digest.md", "routines/team-digest.md"
    }


async def send_due(ctx, now=None):
    if not ctx.slack_client:
        return
    now = now or datetime.now(UTC)
    from illo.routines.team_digest import send_team_due

    await send_team_due(ctx, now)
    async with ctx.pool.connection() as conn:
        members = await people.list_people(conn)
    for person in members:
        zone = ZoneInfo(person["timezone"])
        local = now.astimezone(zone)
        async with ctx.pool.connection() as conn:
            await ledger.lock_digest(conn, person["key"])
            preferences = await briefs.settings(conn, person)
            if not preferences["enabled"]:
                continue
            due = croniter(
                briefs.schedule(ctx.workspace.config, preferences), local + timedelta(microseconds=1)
            ).get_prev(datetime)
            if local - due > timedelta(minutes=2):
                continue
            last = await ledger.digest_last_sent(conn, person["key"])
            if last and (last >= due or last.astimezone(zone).date() >= local.date()):
                continue
            stored = await one(
                conn, "SELECT payload FROM events WHERE channel='digest' AND external_id=%s",
                (f"{person['id']}:{due.isoformat()}",),
            )
            data = (stored["payload"]["payload"]["digest_snapshot"] if stored
                    else await snapshot(conn, person, ctx, now))
            if ctx.workspace.config.digest.personal == "needs" and not any(
                data.get(key) for key in ("needs_you", "stuck", "faults")
            ):
                await ledger.digest_sent(conn, person["key"], now)
                await conn.execute(
                    "UPDATE routine_state SET last_status='skipped' WHERE key=%s",
                    (f"digest:{person['key']}",),
                )
                continue
            text = await brief_text(ctx, person, data, due)
            if not preferences["announced"]:
                text += "\n\n" + FIRST_BRIEF
            sent = await post_dm(
                ctx, person, text, text_blocks(text), "digest", f"{person['id']}:{due.isoformat()}"
            )
            if sent:
                await ledger.digest_sent(conn, person["key"], now)
                await conn.execute(
                    "UPDATE brief_preferences SET announced=true WHERE person_id=%s", (person["id"],)
                )


async def setup(ctx):
    async def check_digests():
        await send_due(ctx)

    ctx.every(30, check_digests)
