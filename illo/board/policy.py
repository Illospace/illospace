"""Apply board timing, recipient budgets and fixed Slack copy."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from illo.board import acl, packet
from illo.board.decision import clean_slack, clean_summary
from illo.board.labels import actor_label
from illo.db import one
from illo.tasks import pages
from illo.tasks.release import in_window

PAGE = "/web/board/t/{id}"


def board_pages_ready(ctx):
    return pages.enabled(ctx) and any(
        getattr(route, "path", None) == PAGE and "GET" in (getattr(route, "methods", None) or ())
        for route in ctx.app.routes)


async def ready(ctx):
    cfg = ctx.workspace.config.board
    return bool(cfg.enabled and cfg.coordinator.enabled)


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
        if episode["reason"] == "decision_needed":
            action = "needs a decision from you"
        elif episode["reason"] == "question":
            action = "has a question for you"
        else:
            action = "mentioned you"
        name = clean_slack(name, 100, escaped_limit=100) or "An agent"
        text = f"{name} {action} on board thread {thread_id}."
    return text + " Open the thread to read it and reply, or reply in this Slack thread."


async def _ask_card_meta(conn, ask):
    from illo.ledger import asks

    requester = (
        await one(
            conn,
            "SELECT t.label,p.id AS person_id,p.name AS person_name,p.key AS person_key "
            "FROM tokens t JOIN people p ON p.id=t.person_id WHERE t.id=%s",
            (ask["requester_token_id"],),
        )
        if ask.get("requester_token_id")
        else None
    )
    links = (ask.get("proof") or {}).get("links")
    return {
        "kind": ask["kind"],
        "use_ask_page": bool(ask.get("blocks") and not await asks.is_board_native(conn, ask)),
        **({"requester": requester} if requester else {}),
        **({"links": links} if isinstance(links, list) and links else {}),
    }


async def _fallback_payload(conn, workspace, thread, episode):
    """Build a verbatim fallback escalation so a model/limit error never leaves a person unnotified."""
    author = await packet.asker_post(conn, episode)
    if episode["reason"] == "decision_needed" and author and author.get("ask_id"):
        ask = await one(conn, "SELECT * FROM asks WHERE id=%s", (author["ask_id"],))
        if ask:
            summary = clean_summary(author.get("text") or "", 1800, escaped_limit=2000)
            if not summary:
                summary = await notice(conn, workspace, episode)
            card = {
                "question": ask["question"],
                "options": list(ask["options"]),
                "recommended": None,
                "recommended_id": ask["recommended_option"],
                "source_ask_id": ask["id"],
                "allow_rule": bool(ask["allow_rule"]),
                **(await _ask_card_meta(conn, ask)),
            }
            return "card", "summary", summary, Jsonb(card), ask["id"]
    if author and author.get("text"):
        key = author["provenance"].get("person_key")
        person = await one(conn, "SELECT key,name FROM people WHERE key=%s", (key,))
        name = clean_slack(actor_label(workspace, {key: person}, author).name, 100, escaped_limit=100) or "An agent"
        title = clean_slack(thread["title"], 120, escaped_limit=140)
        excerpt = clean_summary(author["text"], 1500, escaped_limit=1700)
        if excerpt:
            prefix = f"{name} on {title} (thread {thread['id']}):" if title else f"{name} on board thread {thread['id']}:"
            return "dm", "summary", f"{prefix}\n\n{excerpt}", None, None
    return "link", "notice", await notice(conn, workspace, episode), None, None


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
    if episode["post_id"] is not None and await one(
        conn,
        """SELECT 1 FROM board_escalations
        WHERE post_id=%s AND person_id=%s AND state IN ('pending','sending','sent')""",
        (episode["post_id"], person["id"]),
    ):
        return "already_escalated"
    due = next_window(workspace, person, now)
    if choice:
        fmt = choice.format
        content_mode = "summary" if choice.text else "notice"
        text = choice.text if choice.text else await notice(conn, workspace, episode)
        if choice.format == "card":
            source_meta = {}
            if choice.source_ask_id is not None:
                source_ask = await one(conn, "SELECT * FROM asks WHERE id=%s", (choice.source_ask_id,))
                if source_ask:
                    source_meta = await _ask_card_meta(conn, source_ask)
            card = Jsonb({
                "question": choice.question,
                "options": [o.model_dump(exclude_none=True) for o in choice.options],
                "recommended": choice.recommended,
                **({"recommended_id": choice.recommended_id} if choice.recommended_id is not None else {}),
                **({"source_ask_id": choice.source_ask_id} if choice.source_ask_id is not None else {}),
                "allow_rule": bool(choice.allow_rule),
                "kind": choice.kind,
                "use_ask_page": bool(choice.use_ask_page),
                **source_meta,
            })
        else:
            card = None
        card_ask_id = choice.source_ask_id if choice.format == "card" else None
    else:
        fmt, content_mode, text, card, card_ask_id = await _fallback_payload(conn, workspace, thread, episode)
    row = await one(conn, """INSERT INTO board_escalations
        (thread_id,episode_id,post_id,reason,origin,coordinator_run_id,person_id,
         format,content_mode,text,card,card_ask_id,due_at,acl_snapshot,recipient_slack_user)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (episode_id,person_id) DO NOTHING RETURNING id""",
        (thread["id"], episode["id"], episode["post_id"], episode["reason"],
         "model" if choice else "fallback", run_id, person["id"], fmt,
         content_mode, text, card, card_ask_id, due,
         Jsonb(acl.scope(thread)), person["slack_id"]))
    if not row:
        return "already_escalated"
    await conn.execute("UPDATE board_threads SET escalated_at=clock_timestamp() WHERE id=%s",
                       (thread["id"],))
    await conn.execute("""UPDATE board_episodes SET state='escalated',version=version+1,waits=0,
        next_check_at=%s WHERE id=%s""", (due + timedelta(hours=cfg.escalation_timeout_hours), episode["id"]))
    return None
