"""Apply an explicit card answer to its board thread."""

from illo.board import acl, episodes
from illo.db import one
from illo.ledger import board


async def can_answer(conn, ask, person_id):
    thread_id = await board.ask_thread_id(conn, ask["id"])
    if thread_id is None:
        return True
    thread = await acl.thread_access(conn, thread_id)
    person = await one(conn, "SELECT * FROM people WHERE id=%s", (person_id,))
    if not thread or thread["state"] != "open" or not await acl.recipient_ok(conn, thread, person, slack=False):
        return False
    row = await one(conn, "SELECT state FROM board_escalations WHERE ask_id=%s", (ask["id"],))
    return not row or row["state"] == "sent"


async def on_answer(conn, answered, *, workspace) -> int | None:
    """Use the locks already taken by lock_for_ask."""
    from illo.board.decision import unescape_slack

    escalation = await one(conn, "SELECT * FROM board_escalations WHERE ask_id=%s", (answered["id"],))
    if not escalation or escalation["answer_post_id"] or answered["answered_via"] != "person":
        return None
    if not await can_answer(conn, answered, answered["answered_by_person_id"]):
        return None
    person = await one(conn, "SELECT * FROM people WHERE id=%s AND active",
                       (answered["answered_by_person_id"],))
    option = next((o for o in answered["options"] if o["id"] == answered["answer_option"]), {})
    label = unescape_slack(option.get("label", ""))
    detail = unescape_slack(option.get("detail", ""))
    text = "\n".join(part for part in (label + (". " + detail if detail else ""),
                                       answered["answer_text"]) if part)
    receipt = await board.human_post(
        conn, workspace=workspace, person=person, thread_id=escalation["thread_id"], text=text,
        reply_to_post_id=await episodes.question_reply(conn, escalation),
        source_ref=f"ask:{answered['id']}", via="slack_card", ask_id=answered["id"],
    )
    await episodes.close_escalation(conn, escalation, "other_answer",
                                    person_id=person["id"], post_id=receipt["post_id"])
    return receipt["post_id"]
