"""Queue one scout for each local schedule slot."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

from illo.db import one
from illo.deep import start
from illo.deep.delivery import place
from illo.gateway.principal import Authority
from illo.ledger import people
from illo.tools.registry import Refusal

TEAM_JOB = """You are the scout of the team. Find what will hurt this team in the next 30 days and is on no list yet.

Look where a careful colleague looks:
• Outside things that our code or our work depends on: a model, an API version, a service, a library, a price, a rule. Is one of them due to stop, to change, or to expire? Find the dependency in the repositories first. Then check its state on the web.
• Dates in our own work: a promise, a launch, a renewal, a certificate, a trial, a token.
• Signals that nobody followed: a failed schedule, a ticket that disagrees with the code, a warning in a config comment.

First call `findings_list` and `task_list`. Do not repeat a finding or an open task.
A fix counts only when it is live where the problem is. A change that is merged on one branch and not released is still a finding: say what is left, and the date.
Then explore. Follow what looks wrong. Today's date is in the run facts.

A finding needs all of these:
• What happens, and when.
• The outside source, with its link.
• The exact place in our repository or list, with its link.
• What you could not verify.
• One next step.
• The person who should check, from the team rules.

Save each finding with `finding_add`. Save {max} findings at most, the most important first.
A weak finding costs the team's trust. If you are not sure that it matters, do not save it.
If nothing matters today, save nothing and answer "No finding today."
Your final answer is a short log for the owner: where you looked, and what you rejected."""

PERSON_JOB = """You are this person's scout. Find what will hurt them or the team in the next 30 days and is on no list yet.
Read their recent mail, calendar, and files with your tools, if they are connected. Look for notices with a date: a service that stops, a payment that failed, a renewal, a deadline, a rule change.
Then check in the repositories if the notice touches our code or our work.
Private text stays with this person. Do the web reading first: after you read private data, code refuses the web.

First call `findings_list` and `task_list`. Do not repeat a finding or an open task.
A fix counts only when it is live where the problem is. A change that is merged on one branch and not released is still a finding: say what is left, and the date.
A finding needs all of these: what happens and when; the source; the exact place in our repository or list, when there is one; what you could not verify; one next step.
Save each finding with `finding_add`. Save {max} findings at most. If you are not sure that it matters, do not save it.
If nothing matters today, save nothing and answer "No finding today."""


async def scout_destination(ctx, conn, person=None):
    if person:
        return {"slack_dm": {"person_id": person["id"]}}
    config = ctx.workspace.config
    channel = config.scout.destination or (
        config.digest.team.channel if config.digest.team else None
    )
    if channel:
        channel = next(
            (c.id for c in config.slack.channels if channel in {c.id, c.name, "#" + c.name}),
            channel,
        )
        return {"slack_channel": {"channel_id": channel}}
    owner = await people.get(conn, config.default_owner)
    if not owner:
        raise Refusal("The scout needs an active owner.")
    return {"slack_dm": {"person_id": owner["id"]}}


async def queue(ctx, conn, *, person_key=None, dedupe_key=None, origin_event_id=None):
    person = await people.get(conn, person_key) if person_key else None
    if person_key and not person:
        raise Refusal("The scout needs an active person.")
    authority = (
        Authority("person:" + person["key"], "person_dm", person["id"])
        if person
        else Authority("team", "routine")
    )
    deep_id = await start(
        ctx,
        conn,
        kind="scout",
        job=(PERSON_JOB if person else TEAM_JOB).replace(
            "{max}", str(ctx.workspace.config.scout.max_findings)
        ),
        authority=authority,
        deliver={"none": True},
        origin_event_id=origin_event_id,
        dedupe_key=dedupe_key,
    )
    if deep_id:
        from psycopg.types.json import Jsonb

        target = await scout_destination(ctx, conn, person)
        await conn.execute(
            "UPDATE deep_runs SET context=context || %s WHERE id=%s",
            (Jsonb({"scout_destination": target}), deep_id),
        )
    return deep_id


async def send_due(ctx, now=None):
    if not ctx.workspace.config.deep.enabled:
        return
    now = now or datetime.now(UTC)
    config = ctx.workspace.config
    async with ctx.pool.connection() as conn:
        scopes = ([None] if config.scout.team == "on" else []) + config.scout.people
        for key in dict.fromkeys(scopes):
            person = await people.get(conn, key) if key else None
            if key and not person:
                continue
            local = now.astimezone(ZoneInfo(person["timezone"] if person else config.timezone))
            due = croniter(config.scout.schedule, local + timedelta(microseconds=1)).get_prev(
                datetime
            )
            if local - due <= timedelta(minutes=2):
                await queue(
                    ctx,
                    conn,
                    person_key=key,
                    dedupe_key=f"scout:{key or 'team'}:{due.astimezone(UTC).isoformat()}",
                )


async def manual(ctx, event, authority):
    if event.channel != "slack" or event.kind not in {"dm", "message.im"} or authority.tainted:
        return None
    phrase = re.sub(r"[.!?]+$", "", event.text.strip()).casefold()
    if phrase not in {"run the scout", "run the scout now"}:
        return None
    if event.payload.get("contains_outside_text"):
        return None
    async with ctx.pool.connection() as conn:
        person = await people.by_slack_id(conn, event.sender)
        if not person or person["role"] != "owner":
            return "Only an owner can start the team scout."
        origin = await one(
            conn,
            "SELECT id FROM events WHERE channel='slack' AND external_id=%s",
            (event.external_id,),
        )
        try:
            await queue(ctx, conn, origin_event_id=origin["id"] if origin else None)
            target = await scout_destination(ctx, conn)
        except Refusal as exc:
            return str(exc)
    return "The scout starts now. Findings come to " + place(target) + "."


async def setup(ctx):
    from illo.deep import findings

    ctx.gateway.register_claim(manual)
    ctx.slack_actions.register("scout:", findings.action)

    async def check():
        await send_due(ctx)
        await findings.retry(ctx)

    ctx.every(30, check)
