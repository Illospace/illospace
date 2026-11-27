"""Queue one silent nightly dream reflection per configured schedule slot."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from illo.deep import start
from illo.gateway.principal import Authority
from illo.ledger import dream_snapshot, people
from illo.tools.registry import Refusal

DEFAULT_INSTRUCTIONS = """Run the nightly reflection and self-improvement dream for {team}. This run is completely silent — nobody receives a message from it. Your job is to quietly become a sharper, more human, more deeply informed Digital Chief of Staff overnight.

First call `dream_snapshot` to inspect the day's untainted team interactions, team briefs, board threads, metrics, tool denials, memory changes, recent reflections, and workspace files.

What success looks like for a Digital Chief of Staff:
1. **Real human engagement** — teammates genuinely read, enjoy, and reply to your messages, and pull you into conversations organically rather than ignoring stiff status dumps.
2. **Decision velocity** — when a call or walkthrough is needed, you package the context and a clear recommendation so a teammate can answer in one line instead of asking "what is this?" or "what do you want me to do?"
3. **Trust & zero friction** — high signal-to-noise, low memory corrections/forgets, no robotic IDs or stale reminders, and no dead-end bot moments.
4. **Business & team continuity** — you deeply understand the product, strategy, customers, ownership, and recent decisions across repositories so nobody has to explain the same context twice.

How to reflect:
1. Review `interactions`, `recent_team_briefs`, `recent_board_threads`, `metrics`, `tool_denials`, and `memory_changes` from `dream_snapshot`.
   - Look closely at how humans actually responded: what sparked positive engagement, where someone had to clarify or correct you, and where a message landed flat.
   - Check `recent_reflections` so you know what you already changed on previous nights and whether it helped. Do not oscillate or redo the same edit.
2. When `/repos/` is available in `shell`, explore recent commits, business/strategy docs, architecture docs, or product updates across the team's repositories to deepen your understanding of the business, priorities, and how the team ships.
3. Read any existing workspace file with `dream_read_file` before editing it.

How to improve autonomously (within `max_changes`):
- **Consolidate lasting knowledge (`notes/*.md`)**: capture genuinely durable business context, product concepts, architecture/release norms, partner/project purpose, or team decisions you verified in `dream_snapshot` or `/repos/`. Update existing notes rather than scattering duplicates, and prune stale or superseded notes with `dream_delete_note`. Keep YAML front matter (`title`, `about`, `created`).
- **Sharpen voice, norms, and routines (`rules/*.md`, `routines/*.md`, `skills/*/SKILL.md`)**: only edit when tonight's evidence shows a concrete, repeatable improvement (e.g. a pattern that caused human confusion, a phrasing habit that felt robotic, a recurring gap in team digests, or a reusable procedure worth turning into a skill). Keep edits concise, high-leverage, and faithful to the team's style.
- **Exercise restraint**: never edit files just to look busy. Zero edits on a quiet or already-strong day is a great outcome. Never store secrets, credentials, one-off chatter, or unverified guesses.

Before finishing, always call `dream_record_reflection` with:
- `summary`: a concise, honest 2 to 5 sentence reflection on what went well, what fell short, what you learned about the business/team, and what you changed (or why you left files untouched).
- `scores`: integer ratings from 1 to 5 for `engagement`, `decision_velocity`, `trust_and_signal`, and `continuity` (omit any pillar with no signal today).

Keep your final answer to a short internal log of the dream."""


async def queue(ctx, conn, *, now=None, dedupe_key=None):
    config = ctx.workspace.config
    if not config.deep.enabled:
        raise Refusal("Deep work is off for this team.")
    now = now or datetime.now(UTC)
    snapshot = await dream_snapshot.build(conn, ctx.workspace, now)
    prompt = (ctx.workspace.dream_instructions or DEFAULT_INSTRUCTIONS).replace(
        "{team}", config.team_name
    )
    deep_id = await start(
        ctx,
        conn,
        kind="dream",
        job=prompt,
        authority=Authority("team", "routine"),
        deliver={"none": True},
        origin_event_id=None,
        dedupe_key=dedupe_key,
    )
    if deep_id:
        await conn.execute(
            "UPDATE deep_runs SET context=context || %s WHERE id=%s",
            (Jsonb({"dream_snapshot": jsonable_encoder(snapshot), "dream_changes": []}), deep_id),
        )
    return deep_id


async def send_due(ctx, now=None):
    config = ctx.workspace.config
    if not config.deep.enabled or not config.dream.enabled:
        return None
    now = now or datetime.now(UTC)
    tz = ZoneInfo(config.dream.timezone or config.timezone)
    local = now.astimezone(tz)
    due = croniter(config.dream.schedule, local + timedelta(microseconds=1)).get_prev(datetime)
    if local - due > timedelta(minutes=2):
        return None
    async with ctx.pool.connection() as conn:
        return await queue(
            ctx,
            conn,
            now=now,
            dedupe_key=f"dream:{due.astimezone(UTC).isoformat()}",
        )


async def manual(ctx, event, authority):
    if event.channel != "slack" or event.kind not in {"dm", "message.im"} or authority.tainted:
        return None
    phrase = re.sub(r"[.!?]+$", "", event.text.strip()).casefold()
    if phrase not in {"run the dream", "run dream now"}:
        return None
    if event.payload.get("contains_outside_text"):
        return None
    async with ctx.pool.connection() as conn:
        person = await people.by_slack_id(conn, event.sender)
        if not person:
            return None
        if person["role"] != "owner":
            return "Only an owner can start the nightly dream."
        try:
            await queue(ctx, conn)
        except Refusal as exc:
            return str(exc)
    return "Running a silent dream cycle now. No pings when it finishes — ask me what I learned whenever you like."


async def setup(ctx):
    ctx.gateway.register_claim(manual)

    async def check():
        await send_due(ctx)

    ctx.every(30, check)
