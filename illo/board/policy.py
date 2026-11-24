"""Apply board timing, recipient budgets and fixed Slack copy."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from illo.board import acl, packet
from illo.board.decision import clean_slack
from illo.board.labels import actor_label
from illo.db import one
from illo.ledger import sensors
from illo.tasks import pages
from illo.tasks.release import in_window

PAGE = "/web/board/t/{id}"


def board_pages_ready(ctx):
    return pages.enabled(ctx) and any(
        getattr(route, "path", None) == PAGE and "GET" in (getattr(route, "methods", None) or ())
        for route in ctx.app.routes)


async def ready(ctx):
    cfg = ctx.workspace.config.board
    if not (cfg.enabled and cfg.coordinator.enabled):
        return False
    if board_pages_ready(ctx):
        return True
    async with ctx.pool.connection() as conn:
        await sensors.error(conn, "board:coordinator", "The board coordinator waits for the board thread page.")
    return False


async def lock_budget(conn):
    await conn.execute("SELECT pg_advisory_xact_lock(12000012)")


def next_window(workspace, person, now=None):
    now = now or datetime.now(UTC)
    if not workspace.config.board.coordinator.quiet_hours or in_window(workspace, person, now):
        return now
    local = now.astimezone(ZoneInfo(person["timezone"]))
    start, _ = workspace.config.tasks.reminder_window
    due = local.replace(hour=start, minute=0, second=0, microsecond=0)
    return (due + timedelta(days=int(due <= local))).astimezone(UTC)


async def notice(conn, workspace, episode):
    thread_id = episode["thread_id"]
    if episode["reason"] == "turn_cap":
        cap = workspace.config.board.max_consecutive_agent_posts
        text = (f"The agents on board thread {thread_id} stopped after {cap} turns. "
                "They need a person to go on.")
    else:
        author = await packet.asker_post(conn, episode)
        key = author["provenance"].get("person_key") if author else None
        person = await one(conn, "SELECT key,name FROM people WHERE key=%s", (key,))
        name = actor_label(workspace, {key: person}, author).name if author else "An agent"
        action = "has a question for you" if episode["reason"] == "question" else "mentioned you"
        name = clean_slack(name, 100, escaped_limit=100) or "An agent"
        text = f"{name} {action} on board thread {thread_id}."
    return text + " Open the thread to read it and reply, or reply in this Slack thread."


async def reserve(conn, workspace, thread, episode, person, *, choice=None, run_id=None):
    """Reserve one escalation and its budget in the same transaction."""
    cfg = workspace.config.board.coordinator
    await lock_budget(conn)
    count = await one(conn, """SELECT count(*) AS n FROM board_escalations
        WHERE person_id=%s AND created_at>now()-interval '24 hours'""", (person["id"],))
    now = datetime.now(UTC)
    if count["n"] >= cfg.max_escalations_per_person_per_day:
        return "budget"
    if choice and thread["escalated_at"] and (
        now - thread["escalated_at"] < timedelta(minutes=cfg.thread_cooldown_minutes)
    ):
        return "budget"
    due = next_window(workspace, person, now)
    text = choice.text if choice and choice.text else await notice(conn, workspace, episode)
    card = (Jsonb({"question": choice.question, "options": [o.model_dump() for o in choice.options],
                   "recommended": choice.recommended}) if choice and choice.format == "card" else None)
    row = await one(conn, """INSERT INTO board_escalations
        (thread_id,episode_id,post_id,reason,origin,coordinator_run_id,person_id,
         format,content_mode,text,card,due_at,acl_snapshot,recipient_slack_user)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (episode_id,person_id) DO NOTHING RETURNING id""",
        (thread["id"], episode["id"], episode["post_id"], episode["reason"],
         "model" if choice else "fallback", run_id, person["id"], choice.format if choice else "link",
         "summary" if choice and choice.text else "notice", text, card, due,
         Jsonb(acl.scope(thread)), person["slack_id"]))
    if not row:
        return "already_escalated"
    await conn.execute("UPDATE board_threads SET escalated_at=clock_timestamp() WHERE id=%s",
                       (thread["id"],))
    await conn.execute("""UPDATE board_episodes SET state='escalated',version=version+1,waits=0,
        next_check_at=%s WHERE id=%s""", (due + timedelta(hours=cfg.escalation_timeout_hours), episode["id"]))
    return None
