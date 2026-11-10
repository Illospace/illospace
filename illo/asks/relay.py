"""Bring the answer back to the person who asked a teammate through Illo.

An agent gets its answer through `ask_pull`. A person has no collector, so Illo
sends the answer to that person in a DM, once, and then marks it delivered.
"""

import logging

from illo.asks.service import post_dm
from illo.db import all_rows

log = logging.getLogger(__name__)

WAITING = """
SELECT a.id, a.question, a.options, a.answer_option, a.answer_text, a.answered_via,
       o.name AS owner_name, r.slack_id AS requester_slack_id
FROM asks a JOIN people o ON o.id = a.owner_person_id
JOIN people r ON r.id = a.requester_person_id AND r.active
WHERE a.state = 'answered' AND a.delivery_state = 'pending'
  AND a.requester_person_id IS NOT NULL AND a.requester_token_id IS NULL
  AND a.requester_person_id <> a.owner_person_id
ORDER BY a.answered_at
LIMIT 20
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
    """Send each waiting answer once. The row lock keeps two timers apart."""
    if not ctx.slack_client:
        return
    async with ctx.pool.connection() as conn:
        async with conn.transaction():
            for row in await all_rows(conn, WAITING):
                requester = {"slack_id": row["requester_slack_id"]}
                try:
                    sent = await post_dm(
                        ctx, requester, answer_text(row), None, "ask-answer", row["id"]
                    )
                except Exception:
                    log.error("Could not send the answer of ask %s to its requester", row["id"])
                    continue
                if sent:
                    await conn.execute(
                        "UPDATE asks SET delivery_state='acked', delivered_at=now(), "
                        "acked_at=now(), delivery_attempts=delivery_attempts+1 WHERE id=%s",
                        (row["id"],),
                    )
