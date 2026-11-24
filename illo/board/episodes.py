"""Track which board items still need an answer."""

from illo.db import all_rows, one
from illo.ledger import asks, board

ACTIVE = ("open", "waiting", "headstart", "escalated")
OPEN_SENDS = ("pending", "sending", "sent")


async def pending_items(conn, thread_id):
    return await all_rows(conn, """WITH posts AS (
        SELECT * FROM board_posts WHERE thread_id=%s
    )
    SELECT 'question' AS reason,p.id AS post_id,NULL::text AS mention_target,NULL::timestamptz AS stalled_at
    FROM posts p WHERE NOT p.is_human AND p.actor<>'illo' AND p.kind='question'
    AND NOT EXISTS (SELECT 1 FROM posts r WHERE (r.is_human AND r.id>p.id)
        OR (r.kind IN ('answer','decision') AND r.reply_to_post_id=p.id))
    UNION ALL
    SELECT 'mention',p.id,m.target,NULL FROM posts p JOIN board_mentions m ON m.post_id=p.id
    WHERE NOT p.is_human AND p.actor<>'illo' AND p.kind<>'decision_needed'
        AND starts_with(m.target,'person:')
        AND NOT EXISTS (SELECT 1 FROM board_mention_acks a
            WHERE a.post_id=p.id AND a.target=m.target AND a.reader_key=m.target)
        AND NOT EXISTS (SELECT 1 FROM posts r WHERE r.id>p.id AND r.is_human
            AND r.person_id=m.recipient_person_id)
    UNION ALL
    SELECT 'turn_cap',NULL,NULL,stalled_at FROM board_threads
    WHERE id=%s AND stalled_reason='turn_cap' AND stalled_at IS NOT NULL""", (thread_id, thread_id))


def item_key(item):
    return tuple(item[key] for key in ("reason", "post_id", "mention_target", "stalled_at"))


async def is_pending(conn, episode):
    return item_key(episode) in {item_key(row) for row in await pending_items(conn, episode["thread_id"])}


async def close_escalation(conn, row, reason, *, person_id=None, post_id=None):
    answered = person_id is not None and person_id == row["person_id"]
    await conn.execute("""UPDATE board_escalations SET state=%s,closed_reason=%s,
        closed_at=clock_timestamp(),closed_by_person_id=%s,answer_post_id=%s
        WHERE id=%s AND state=ANY(%s)""",
        ("answered" if answered else "closed", None if answered else reason,
         person_id, post_id, row["id"], list(OPEN_SENDS)))
    if row["ask_id"]:
        await asks.cancel(conn, row["ask_id"])


async def close_items(conn, thread, *, reason, person_id=None, post_id=None):
    thread_id = thread["id"]
    await board.lock_children(conn, thread_id)
    pending = ({item_key(row) for row in await pending_items(conn, thread_id)}
               if reason in {"answered", "no_longer_pending"} else set())
    rows = await all_rows(conn, """SELECT * FROM board_episodes
        WHERE thread_id=%s AND state=ANY(%s) ORDER BY id""", (thread_id, list(ACTIVE)))
    ended = [row["id"] for row in rows if item_key(row) not in pending]
    await conn.execute("""UPDATE board_episodes SET state='closed',closed_at=clock_timestamp(),
        closed_reason=%s,version=version+1 WHERE id=ANY(%s::bigint[])""", (reason, ended))
    for row in await all_rows(conn, """SELECT e.*,b.mention_target,b.stalled_at FROM board_escalations e
        JOIN board_episodes b ON b.id=e.episode_id WHERE e.thread_id=%s AND e.state=ANY(%s)
        ORDER BY e.id""", (thread_id, list(OPEN_SENDS))):
        if item_key(row) not in pending:
            await close_escalation(conn, row, "other_answer" if reason == "answered" else reason,
                                   person_id=person_id, post_id=post_id)


async def question_reply(conn, escalation):
    row = await one(conn, "SELECT id FROM board_posts WHERE id=%s AND kind='question'",
                    (escalation["post_id"],))
    return row["id"] if row else None
