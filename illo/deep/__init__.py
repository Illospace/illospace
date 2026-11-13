"""Queue deep work with the authority of the person who requests it."""

from psycopg.types.json import Jsonb

from illo.db import one
from illo.tools.registry import Refusal


def value(authority, key, default=None):
    return (
        authority.get(key, default)
        if isinstance(authority, dict)
        else getattr(authority, key, default)
    )


def validate_delivery(value):
    if not isinstance(value, dict) or len(value) != 1:
        raise Refusal("The deep work has no valid destination.")
    key, fields = next(iter(value.items()))
    required = {
        "slack_thread": {"channel_id", "thread_ts"},
        "slack_dm": {"person_id"},
        "slack_channel": {"channel_id"},
    }
    if key == "none" and fields is True:
        return value
    if key not in required or not isinstance(fields, dict) or set(fields) != required[key]:
        raise Refusal("The deep work has no valid destination.")
    if not all(fields.values()):
        raise Refusal("The deep work has no valid destination.")
    return value


async def start(
    ctx, conn, *, kind, job, authority, deliver, origin_event_id=None, dedupe_key=None
) -> int | None:
    config = ctx.workspace.config.deep
    if not config.enabled:
        raise Refusal("Deep work is off for this team.")
    if (
        value(authority, "tainted", False)
        or value(authority, "outside_read", False)
        or value(authority, "web_read", False)
        or value(authority, "profile") in {"tainted", "outsider"}
        or value(authority, "principal") == "outsider"
    ):
        raise Refusal("Deep work needs a team request that has not read outside text.")
    if kind not in {"request", "scout", "brief"} or not job.strip():
        raise Refusal("Give a job for the deep work.")
    principal, profile = value(authority, "principal"), value(authority, "profile")
    person_id = value(authority, "person_id")
    origin = (
        await one(conn, "SELECT * FROM events WHERE id=%s", (origin_event_id,))
        if origin_event_id
        else None
    )
    context = origin["payload"].get("payload", {}).copy() if origin else {}
    if origin and (origin["tainted"] or context.get("contains_outside_text")):
        raise Refusal("Deep work needs a team request that has not read outside text.")
    if kind == "request":
        if profile not in {"person_dm", "team_channel"}:
            raise Refusal("A person must request the deep work.")
        if principal == "team" and origin:
            actor = await one(
                conn,
                "SELECT id,key FROM people WHERE key=%s AND active",
                (origin["payload"].get("task_actor"),),
            )
            person_id = actor["id"] if actor else None
            if actor:
                context["task_actor"] = actor["key"]
        if not person_id:
            raise Refusal("A known active person must request the deep work.")
    if person_id and not await one(
        conn, "SELECT id FROM people WHERE id=%s AND active", (person_id,)
    ):
        raise Refusal("The person is missing or inactive.")
    deliver = validate_delivery(deliver)
    if principal and principal.startswith("person:") and deliver != {"none": True}:
        if deliver != {"slack_dm": {"person_id": person_id}}:
            # A private run may use the original DM thread, never a shared channel.
            dest = deliver.get("slack_thread", {})
            if not (
                origin
                and origin["kind"] in {"dm", "message.im"}
                and dest.get("channel_id") == context.get("channel_id")
                and dest.get("thread_ts")
                == (
                    context.get("thread_ts")
                    or origin["payload"].get("reply_to")
                    or context.get("message_ts")
                )
            ):
                raise Refusal("Personal deep work goes to that person's DM.")
    # Serialize admission so concurrent callers cannot pass the same limits.
    await conn.execute("SELECT pg_advisory_xact_lock(15001500)")
    if dedupe_key and await one(
        conn, "SELECT id FROM deep_runs WHERE dedupe_key=%s", (dedupe_key,)
    ):
        return None
    queued = await one(conn, "SELECT count(*) AS n FROM deep_runs WHERE state='queued'")
    if queued["n"] >= config.max_queue:
        raise Refusal("The deep work queue is full. Try again later.")
    if person_id:
        count = await one(
            conn,
            "SELECT count(*) AS n FROM deep_runs WHERE person_id=%s AND created_at >= (date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')",
            (person_id,),
        )
        if count["n"] >= config.max_per_person_per_day:
            raise Refusal("You reached the daily limit for deep work.")
    row = await one(
        conn,
        """INSERT INTO deep_runs
        (kind,job,principal,person_id,profile,deliver,origin_event_id,dedupe_key,context)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (
            kind,
            job,
            principal,
            person_id,
            profile,
            Jsonb(deliver),
            origin_event_id,
            dedupe_key,
            Jsonb(context),
        ),
    )
    return row["id"]


async def setup(ctx):
    from illo.deep.worker import tick
    from illo.routines.scout import setup as scout_setup

    async def work():
        await tick(ctx)

    ctx.every(2, work)
    await scout_setup(ctx)
