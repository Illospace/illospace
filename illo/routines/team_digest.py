"""Send the team brief once per local day with a stable delivery key."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

from illo.asks.cards import escape
from illo.db import one
from illo.ledger import ask_workflow, kv
from illo.ledger.digest_snapshot import TEAM_STATE_KEY, team_snapshot
from illo.routines.digest import linked, write_brief
from illo.routines.scheduler import deliver_routine

DEFAULT_INSTRUCTIONS = """Write the team brief for {team}. Call `team_snapshot` first and use only its data.

Write like a colleague who respects the team's time.

- Start with a short morning greeting to the team, for example "Morning, team." Then say the one thing that matters most today, with the reason.
- Then, in this order: what is stuck and who owns it, which decisions and reviews wait and for whom, what shipped.
- Name the owner of each stuck item and each thing that waits. Use a Slack mention (<@SLACK_ID>) only for a person whose item waits more than 24 hours. Use the plain name in all other cases.
- One short sentence or one bullet for each item. Make the title a link if the item has a link. Rewrite a raw title into plain words.
- Maximum 8 lines. If a group has more than 3 items, give the count and the 3 most important ones.
- `recent_openings` shows how your last briefs started. Start in a different way, and do not reuse a comparison or a joke from them.
- Shipped work needs its link. Do not call work finished if it is not in `shipped`.
- Mention a fault only if `faults` is not empty. Say what does not work and who can repair it.
- End with one line that invites the team: a person who wants Illo to coordinate something can reply in the thread.
- No headers, no table, no counts line."""


def fallback_text(data):
    lines = ["Morning, team."]
    if stuck := data["stuck"]:
        lines.append("Stuck:")
        lines.extend(
            f"• {linked(item)} — {escape(item['owner'])}, no signal for {int(item['hours_since_signal'])} h"
            for item in stuck[:3]
        )
        if len(stuck) > 3:
            lines.append(f"and {len(stuck) - 3} more")
    if data["waiting"]:
        lines.append("Waiting:")
        for person in data["waiting"]:
            parts = []
            for count, word in (
                (person["asks"], "decision"),
                (len(person["reviews"]), "review"),
                (person["your_moves"], "thread"),
            ):
                if count:
                    parts.append(f"{count} {word}{'s' if count != 1 else ''}")
            lines.append(f"• {escape(person['name'])}: {', '.join(parts)}")
    if data["shipped_total"]:
        lines.append(f"Shipped: {data['shipped_total']}.")
        lines.extend("• " + linked(item) for item in data["shipped"][:3])
    if data["faults"]:
        lines.append("Not working:")
        lines.extend(escape(item["what"]) for item in data["faults"])
    lines.append("Reply in this thread if you want me to coordinate something.")
    return "\n".join(lines)


async def send_team_due(ctx, now=None):
    config = ctx.workspace.config
    team = config.digest.team
    if not team or not ctx.slack_client:
        return
    now = now or datetime.now(UTC)
    zone = ZoneInfo(config.timezone)
    local = now.astimezone(zone)
    due = croniter(team.schedule, local + timedelta(microseconds=1)).get_prev(datetime)
    if local - due > timedelta(minutes=2):
        return
    channel = next(c for c in config.slack.channels if c.name == team.channel)
    async with ctx.pool.connection() as conn:
        await ask_workflow.lock_digest(conn, TEAM_STATE_KEY)
        state = await one(
            conn, "SELECT last_run_at FROM routine_state WHERE key=%s", (TEAM_STATE_KEY,)
        )
        last = state["last_run_at"] if state else None
        if last and (last >= due or last.astimezone(zone).date() >= local.date()):
            return
        key = f"digest-team:{due.isoformat()}"
        # A failed send must retry the event even if the current work is now empty.
        event = await one(
            conn,
            "SELECT payload FROM events WHERE channel='team_digest' AND external_id=%s",
            (key,),
        )
        data = (
            event["payload"]["payload"]["team_snapshot"]
            if event
            else await team_snapshot(conn, config, now)
        )
        active = any(data[k] for k in ("shipped", "stuck", "waiting", "faults"))
        if active or event:
            prompt = (ctx.workspace.team_digest_instructions or DEFAULT_INSTRUCTIONS).replace(
                "{team}", config.team_name
            )
            text = await write_brief(
                ctx, "team_digest", key, prompt, "team", "team_snapshot", data, fallback_text, channel_id=channel.id
            )
            sent = await deliver_routine(ctx, "channel:" + channel.id, text, delivery_key=key)
            await kv.set(
                conn,
                f"slack:team-brief:{channel.id}:{sent['ts']}",
                (now + timedelta(hours=24)).isoformat(),
            )
        await conn.execute(
            """INSERT INTO routine_state (key,last_run_at,last_status) VALUES (%s,%s,%s)
            ON CONFLICT (key) DO UPDATE SET last_run_at=EXCLUDED.last_run_at,last_status=EXCLUDED.last_status""",
            (TEAM_STATE_KEY, now, "ok" if active or event else "skipped"),
        )
