"""Bring the answer back to the person who asked a teammate through Illo.

An agent gets its answer through `ask_pull`. A person has no collector, so Illo
sends the answer to that person in a DM, once, and then marks it delivered.
"""

import logging

from illo.asks.service import message_id
from illo.db import all_rows, one

log = logging.getLogger(__name__)

WAITING = """
SELECT a.id, a.question, a.options, a.answer_option, a.answer_text, a.answered_via,
       o.name AS owner_name, r.slack_id AS requester_slack_id
FROM asks a JOIN people o ON o.id = a.owner_person_id
JOIN people r ON r.id = a.requester_person_id AND r.active AND r.slack_id IS NOT NULL
  AND r.slack_id <> ''
WHERE a.state = 'answered' AND a.delivery_state = 'pending'
  AND a.requester_person_id IS NOT NULL AND a.requester_token_id IS NULL
  AND a.requester_person_id <> a.owner_person_id
ORDER BY a.delivery_attempts, a.answered_at, a.id
LIMIT 1
FOR UPDATE OF a SKIP LOCKED
"""


def answer_text(row: dict) -> str:
    """One short message: who answered, the answer, and the question it belongs to."""
    label = next(
        (o.get("label") for o in row["options"] or [] if o.get("id") == row["answer_option"]),
        None,
    )
    parts = [p for p in (label, row["answer_text"]) if p]
    answer = ". ".join(parts) if parts else "No text"
    how = " (by a rule)" if row["answered_via"] == "rule" else ""
    return f"*{row['owner_name']} answered{how}:* {answer}\n> {row['question']}"


async def tick(ctx) -> None:
    """Commit one send marker before each call. Never retry an uncertain post."""
    async with ctx.pool.connection() as conn:
        await conn.execute(
            """UPDATE asks a SET delivery_state='failed'
            FROM people r WHERE r.id=a.requester_person_id
              AND a.state='answered' AND a.delivery_state='pending'
              AND a.requester_token_id IS NULL AND a.requester_person_id<>a.owner_person_id
              AND (NOT r.active OR r.slack_id IS NULL OR r.slack_id='')"""
        )
        expired = await all_rows(
            conn,
            """UPDATE asks SET delivery_state='failed'
            WHERE state='answered' AND delivery_state='delivered'
              AND requester_token_id IS NULL AND acked_at IS NULL
              AND delivered_at < now() - interval '2 minutes' RETURNING id""",
        )
    for row in expired:
        log.error("The delivery result of ask %s is unknown; no retry", row["id"])
    if not ctx.slack_client:
        return
    for _ in range(20):
        async with ctx.pool.connection() as conn:
            row = await one(conn, WAITING)
            if not row:
                return
            await conn.execute(
                "UPDATE asks SET delivery_state='delivered', delivered_at=now(), "
                "delivery_attempts=delivery_attempts+1 WHERE id=%s",
                (row["id"],),
            )
        # The transaction has committed. No ask locks span a network call.
        try:
            opened = await ctx.slack_client.conversations_open(users=row["requester_slack_id"])
            channel = opened["channel"]["id"]
        except Exception:
            # No post started. This failure can be retried safely.
            async with ctx.pool.connection() as conn:
                await conn.execute(
                    "UPDATE asks SET delivery_state=CASE WHEN delivery_attempts>=5 "
                    "THEN 'failed' ELSE 'pending' END WHERE id=%s AND delivery_state='delivered'",
                    (row["id"],),
                )
            log.error("Could not open the requester DM for ask %s", row["id"])
            continue
        try:
            sent = await ctx.slack_client.chat_postMessage(
                channel=channel, text=answer_text(row),
                client_msg_id=message_id("ask-answer", row["id"]),
            )
        except Exception:
            log.error("The delivery result of ask %s is unknown", row["id"])
            continue
        if sent:
            async with ctx.pool.connection() as conn:
                await conn.execute(
                    "UPDATE asks SET delivery_state='acked', acked_at=now() "
                    "WHERE id=%s AND delivery_state='delivered'",
                    (row["id"],),
                )
