"""Ledger queries for cards, routing, and the decision log."""

from psycopg.types.json import Jsonb

from illo.db import Connection, all_rows, one
from illo.ledger import asks, items


async def route(conn: Connection, ask, default_owner):
    """Keep an explicit owner. Otherwise use area, lane, token, then default."""
    if ask["owner_person_id"]:
        return ask
    owner = None
    area = ask.get("area") or ask["proof"].get("area")
    if area and ask["kind"] in {"action", "decision"}:
        owner = await one(
            conn,
            "SELECT id FROM people WHERE active AND %s=ANY(areas) ORDER BY key LIMIT 1",
            (area,),
        )
    if not owner and ask["lane_id"]:
        owner = await one(
            conn,
            "SELECT p.id FROM people p JOIN lanes l ON l.owner_person_id=p.id WHERE l.id=%s AND p.active",
            (ask["lane_id"],),
        )
    if not owner and ask["requester_token_id"]:
        owner = await one(
            conn,
            "SELECT p.id FROM people p JOIN tokens t ON t.person_id=p.id WHERE t.id=%s AND p.active",
            (ask["requester_token_id"],),
        )
    if not owner and ask["requester_person_id"]:
        owner = await one(
            conn, "SELECT id FROM people WHERE id=%s AND active", (ask["requester_person_id"],)
        )
    if not owner:
        owner = await one(conn, "SELECT id FROM people WHERE key=%s AND active", (default_owner,))
    if not owner:
        return ask
    # A private lane never sends its contents to another person through routing.
    lane = (
        await one(conn, "SELECT * FROM lanes WHERE id=%s", (ask["lane_id"],))
        if ask["lane_id"]
        else None
    )
    if lane and lane["visibility"] == "private" and owner["id"] != lane["owner_person_id"]:
        owner = {"id": lane["owner_person_id"]}
    return await one(
        conn, "UPDATE asks SET owner_person_id=%s WHERE id=%s RETURNING *", (owner["id"], ask["id"])
    )


async def card_data(conn: Connection, ask):
    row = dict(ask)
    row["lane"] = (
        await one(conn, "SELECT * FROM lanes WHERE id=%s", (ask["lane_id"],))
        if ask["lane_id"]
        else None
    )
    row["owner"] = await one(
        conn, "SELECT * FROM people WHERE id=%s AND active", (ask["owner_person_id"],)
    )
    row["requester"] = (
        await one(
            conn,
            "SELECT t.label,p.name AS person_name,p.key AS person_key "
            "FROM tokens t JOIN people p ON p.id=t.person_id WHERE t.id=%s",
            (ask["requester_token_id"],),
        )
        if ask["requester_token_id"]
        else None
    )
    row["rule"] = (
        await one(conn, "SELECT * FROM rules WHERE id=%s", (ask["rule_id"],))
        if ask["rule_id"]
        else None
    )
    return row


async def close_stale(conn: Connection):
    """A source reference follows the lane's terminal state."""
    await conn.execute("""
        UPDATE asks a SET state='cancelled' FROM lanes l WHERE a.lane_id=l.id
        AND a.source_ref IS NOT NULL AND a.state='open'
        AND (l.state IN ('done','failed') OR l.closed_at IS NOT NULL)
    """)


async def expire(conn: Connection):
    await asks.expire(conn)
    await conn.execute("""
        UPDATE drafts SET state='expired',decided_at=now()
        WHERE state='pending' AND created_at<=now()-interval '24 hours'
    """)


async def decision(conn: Connection, ask):
    """Upsert a complete decision. Preserve the lane's visibility."""
    data = await card_data(conn, ask)
    lane = data["lane"] or {}
    option = next((o["label"] for o in ask["options"] if o["id"] == ask["answer_option"]), "")
    owner = data["owner"] or {}
    body = "\n".join(
        [
            ask["question"],
            "Options:",
            *[f"{o['id']}: {o['label']}. {o.get('detail', '')}" for o in ask["options"]],
            f"Answer: {option} {ask['answer_text'] or ''}",
            f"Who: {owner.get('name', 'Unknown')}",
            f"When: {ask['answered_at'].isoformat()}",
            f"How: {ask['answered_via']}",
            f"Lane: {lane.get('title', '')}",
            *[str(link) for link in lane.get("links", [])],
        ]
    )
    return await items.upsert(
        conn,
        source_ref=f"ask:{ask['id']}",
        title=ask["question"],
        kind="decision",
        body=body,
        summary=f"{option} {ask['answer_text'] or ''}".strip(),
        owner_person_id=(lane.get("owner_person_id") or ask["owner_person_id"])
        if lane
        else (ask["requester_person_id"] or ask["owner_person_id"]),
        visibility=lane.get("visibility", "private"),
        links=lane.get("links", []),
        source_at=ask["answered_at"],
        meta={"ask_id": ask["id"], "via": ask["answered_via"], "rule_id": ask["rule_id"]},
    )


async def record_decisions(conn: Connection):
    for ask in await all_rows(
        conn,
        "SELECT a.* FROM asks a LEFT JOIN items i ON i.source_ref='ask:' || a.id WHERE a.state='answered' AND i.id IS NULL",
    ):
        await decision(conn, ask)
    for lane in await all_rows(
        conn,
        "SELECT l.* FROM lanes l LEFT JOIN items i ON i.source_ref='lane:' || l.id || ':end' WHERE l.end_summary IS NOT NULL AND (l.closed_at IS NOT NULL OR l.state IN ('done','failed')) AND i.id IS NULL",
    ):
        await items.upsert(
            conn,
            source_ref=f"lane:{lane['id']}:end",
            title=lane["title"],
            kind="lane_summary",
            body=lane["end_summary"],
            summary=lane["end_summary"],
            owner_person_id=lane["owner_person_id"],
            visibility=lane["visibility"],
            links=lane["links"],
            source_at=lane["closed_at"],
        )


async def digest_data(conn: Connection, person, now):
    """Only include this person's private lanes. Shared digests never use this snapshot."""
    person = dict(person)
    person["now"] = now
    person["asks"] = await all_rows(
        conn,
        """
        SELECT a.*, l.title AS lane_title FROM asks a LEFT JOIN lanes l ON l.id=a.lane_id
        WHERE a.owner_person_id=%s AND a.state='open'
        AND (l.id IS NULL OR l.visibility='team' OR l.owner_person_id=%s)
        ORDER BY a.due_at NULLS LAST,a.id
    """,
        (person["id"], person["id"]),
    )
    person["running"] = (
        await one(
            conn,
            "SELECT count(*) AS n FROM lanes WHERE owner_person_id=%s AND state='working' AND last_signal_at>=%s - COALESCE((%s::jsonb ->> source)::integer,30)*interval '1 minute'",
            (person["id"], now, Jsonb(person.pop("stale_minutes", {}))),
        )
    )["n"]
    state = await one(
        conn, "SELECT last_run_at FROM routine_state WHERE key=%s", (f"digest:{person['key']}",)
    )
    person["finished"] = await all_rows(
        conn,
        "SELECT * FROM lanes WHERE owner_person_id=%s AND state IN ('done','failed') AND closed_at>COALESCE(%s,%s-interval '1 day') ORDER BY closed_at DESC LIMIT 5",
        (person["id"], state["last_run_at"] if state else None, now),
    )
    person["sensors"] = await all_rows(
        conn,
        "SELECT * FROM sensors WHERE last_ok_at IS NULL OR last_error_at>last_ok_at OR last_ok_at<%s-interval '30 minutes' ORDER BY key",
        (now,),
    )
    person["failed"] = await all_rows(
        conn,
        """SELECT a.id,a.question FROM asks a LEFT JOIN lanes l ON l.id=a.lane_id
        WHERE a.owner_person_id=%s AND a.delivery_state='failed'
        AND (l.id IS NULL OR l.visibility='team' OR l.owner_person_id=%s) ORDER BY a.id""",
        (person["id"], person["id"]),
    )
    return person


async def asks_to_check(conn: Connection):
    return await all_rows(
        conn,
        "SELECT id FROM asks WHERE state='open' OR (slack_ts IS NOT NULL AND (state IS DISTINCT FROM card_state OR delivery_state IS DISTINCT FROM card_delivery_state)) ORDER BY id",
    )


async def lock_ask(conn: Connection, ask_id):
    return await one(conn, "SELECT * FROM asks WHERE id=%s FOR UPDATE", (ask_id,))


async def save_ask_card(conn: Connection, ask_id, sent, card_digest):
    await conn.execute(
        "UPDATE asks SET slack_channel=%s,slack_ts=%s,card_digest=%s,card_sent_at=COALESCE(card_sent_at,now()),card_state=state,card_delivery_state=delivery_state WHERE id=%s",
        (sent["channel"], sent["ts"], card_digest, ask_id),
    )


async def await_text(conn: Connection, ask_id):
    await conn.execute("UPDATE asks SET awaiting_text=true WHERE id=%s AND state='open'", (ask_id,))


async def thread_ask(conn: Connection, person_id, channel, thread):
    return await one(
        conn,
        "SELECT * FROM asks WHERE owner_person_id=%s AND slack_channel=%s AND slack_ts=%s AND awaiting_text=true FOR UPDATE",
        (person_id, channel, thread),
    )


async def due_asks(conn: Connection):
    return await all_rows(
        conn,
        "SELECT id FROM asks WHERE state='open' AND due_at<=now() AND reminded_at IS NULL AND slack_ts IS NOT NULL ORDER BY id",
    )


async def mark_reminded(conn: Connection, ask_id):
    await conn.execute("UPDATE asks SET reminded_at=now() WHERE id=%s", (ask_id,))


async def drafts_to_check(conn: Connection):
    return await all_rows(
        conn,
        "SELECT id FROM drafts WHERE state='pending' OR (slack_ts IS NOT NULL AND (state IS DISTINCT FROM card_state OR send_error IS DISTINCT FROM card_send_error)) ORDER BY id",
    )


async def lock_draft(conn: Connection, draft_id):
    draft = await one(conn, "SELECT * FROM drafts WHERE id=%s FOR UPDATE", (draft_id,))
    if draft and draft["kind"] == "asks_answer":
        args = draft["content"]["arguments"]
        ask = await one(conn, "SELECT * FROM asks WHERE id=%s", (args["id"],))
        if ask and ask["owner_person_id"] == draft["approver_person_id"]:
            option = next((o for o in ask["options"] if o["id"] == args["option"]), None)
            draft["answer_preview"] = {
                "question": ask["question"],
                "option": option,
                "text": args["text"],
            }
    return draft


async def draft_owner(conn: Connection, draft):
    return await one(
        conn, "SELECT * FROM people WHERE id=%s AND active", (draft["approver_person_id"],)
    )


async def requester_name(conn: Connection, person_id: int | None) -> str:
    row = await one(conn, "SELECT name FROM people WHERE id=%s", (person_id,))
    return row["name"] if row else "none"


async def save_draft_card(conn: Connection, draft_id, sent, card_digest):
    await conn.execute(
        "UPDATE drafts SET slack_channel=%s,slack_ts=%s,card_digest=%s,card_state=state,card_send_error=send_error WHERE id=%s",
        (sent["channel"], sent["ts"], card_digest, draft_id),
    )


async def draft_failed(conn: Connection, draft_id):
    await conn.execute(
        "UPDATE drafts SET send_error=true WHERE id=%s AND state='approved'", (draft_id,)
    )


async def digest_sent(conn: Connection, person_key, now):
    await conn.execute(
        "INSERT INTO routine_state (key,last_run_at,last_status) VALUES (%s,%s,'ok') ON CONFLICT (key) DO UPDATE SET last_run_at=EXCLUDED.last_run_at,last_status='ok'",
        (f"digest:{person_key}", now),
    )


async def digest_last_sent(conn: Connection, person_key):
    row = await one(
        conn, "SELECT last_run_at FROM routine_state WHERE key=%s", (f"digest:{person_key}",)
    )
    return row["last_run_at"] if row else None


async def stats(conn: Connection):
    return {
        "cards": await all_rows(conn, "SELECT * FROM ask_daily_stats ORDER BY day,person"),
        "rules": await all_rows(conn, "SELECT * FROM ask_rule_stats ORDER BY person"),
    }


async def lock_lane_state(conn: Connection, lane_id):
    return await one(conn, "SELECT state,closed_at FROM lanes WHERE id=%s FOR SHARE", (lane_id,))


async def expire_draft(conn: Connection, draft_id):
    await conn.execute(
        "UPDATE drafts SET state='expired',decided_at=now() WHERE id=%s AND state='pending' AND created_at<=now()-interval '24 hours'",
        (draft_id,),
    )


async def lock_digest(conn: Connection, person_key):
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"illo:digest:{person_key}",)
    )


async def expire_ask(conn: Connection, ask_id: int) -> None:
    await conn.execute(
        "UPDATE asks SET state='expired' WHERE id=%s AND state='open' AND expires_at<=now()",
        (ask_id,),
    )
