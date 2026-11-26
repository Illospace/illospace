"""Handle owner answers without a model run."""

import re

from illo.asks.cards import can_always
from illo.asks.service import answer, is_live, refresh_ask
from illo.ledger import ask_workflow as ledger
from illo.ledger import board, rules


def in_card_dm(person, action, row):
    return (
        person["active"]
        and row["owner_person_id"] == person["id"]
        and row["slack_channel"]
        and row["slack_channel"].startswith("D")
        and action.get("channel") == row["slack_channel"]
        and action.get("message_ts") == row["slack_ts"]
    )


async def thread_notice(ctx, row, text):
    if ctx.slack_client and row.get("slack_channel") and row.get("slack_ts"):
        await ctx.slack_client.chat_postMessage(
            channel=row["slack_channel"], thread_ts=row["slack_ts"], text=text
        )


def exact_pattern(question):
    return "".join(
        {"*": "[*]", "?": "[?]", "[": "[[]"}.get(c, c)
        for c in " ".join(question.casefold().split())
    )


async def handle(ctx, person, action):
    parts = action.get("action_id", "").split(":", 3)
    if len(parts) < 3 or not parts[1].isdigit():
        return
    ask_id, operation = int(parts[1]), parts[2]
    if not 0 < ask_id < 2**63:
        return
    from illo.board.escalate import recover_card

    await recover_card(ctx, person, ask_id, action)
    notice = None
    scout_finding = None
    async with ctx.pool.connection() as conn:
        ask = await board.lock_for_ask(conn, ask_id)
        if not ask or not in_card_dm(person, action, ask):
            return
        if not await is_live(conn, ask):
            notice = "This question is closed. No answer is needed."
        elif operation == "other":
            await ledger.await_text(conn, ask_id)
            notice = "Reply in this thread with your answer."
        elif operation in {"opt", "always"} and len(parts) == 4:
            option = next((o for o in ask["options"] if o["id"] == parts[3]), None)
            if option is None or (operation == "always" and not can_always(ask)):
                return
            try:
                async with conn.transaction():
                    answered = await answer(conn, ask_id, person["id"], option=option["id"],
                                            workspace=ctx.workspace)
            except board.BoardError as exc:
                answered = None
                notice = str(exc)
            else:
                if not answered:
                    return
                scout_finding = answered.get("scout_task_finding")
            if answered and operation == "always":
                data = await ledger.card_data(conn, ask)
                lane = data["lane"] or {}
                match = {
                    "ask_kind": ask["kind"],
                    "question_pattern": exact_pattern(ask["question"]),
                    "lane_source": lane.get("source"),
                    "repository": lane.get("repo"),
                    "requester_person_id": await rules.requester_person_id(conn, ask),
                }
                await rules.create(
                    conn,
                    owner_person_id=person["id"],
                    text=ask["question"],
                    kind="always_allow" if ask["permission"] else "standing_answer",
                    match=match,
                    answer_option_label=option["label"],
                    answer_option_id=option["id"],
                    answer_option_detail=option.get("detail", ""),
                    origin="card",
                )
                requester = await ledger.requester_name(conn, match["requester_person_id"])
                notice = (
                    f"Rule saved. Scope: source: {match['lane_source'] or 'none'}; "
                    f"repository: {match['repository'] or 'none'}; "
                    f"requester: {requester}. "
                    'Say "show my rules" to see or remove it.'
                )
        else:
            return
    if notice:
        await thread_notice(ctx, ask, notice)
    await refresh_ask(ctx, ask_id)
    if scout_finding:
        from illo.deep import findings

        await findings.trigger_task_from_finding(
            ctx, person, scout_finding, channel=ask.get("slack_channel"), ts=ask.get("slack_ts")
        )


_HELP_CUES = re.compile(
    r"(?:\b(?:what(?:'s|s| is| do you)?\s+(?:the\s+)?recommend|no idea|not sure|don't know|"
    r"dont know|can you explain|help me decide|which one should|what would you|what do you think|"
    r"tu recommandes|quelle recommandation|je ne sais pas|tu conseilles|peux[- ]tu expliquer|"
    r"ask\s+\S+\s+instead|should\s+ask|reassign|route\s+(?:this|it)\s+to|pass\s+(?:this|it)\s+to|"
    r"send\s+(?:this|it)\s+to|demande\s+[àa])\b)",
    re.IGNORECASE,
)


def looks_like_help_question(text: str) -> bool:
    """Return True when the reply asks Illo for help instead of giving a final answer."""
    stripped = text.strip()
    if not stripped:
        return False
    return stripped.endswith("?") or bool(_HELP_CUES.search(stripped))


async def claim_thread(ctx, event, authority):
    if event.channel != "slack" or authority.profile != "person_dm" or authority.tainted:
        return None
    thread = event.payload.get("thread_ts") or event.reply_to
    channel = event.payload.get("channel_id")
    if not thread or not channel or not channel.startswith("D"):
        return None
    async with ctx.pool.connection() as conn:
        ask = await ledger.thread_ask(conn, authority.person_id, channel, thread)
        if not ask or (event.payload.get("clip_unreadable") and not board.is_coordinator_ask(ask)):
            return None
        if not await is_live(conn, ask):
            text = "This question is closed. No answer is needed."
        elif not (typed := await reply_text(conn, ask, event)).strip():
            from illo.board.escalate import WRITE_TEXT

            text = WRITE_TEXT if board.is_coordinator_ask(ask) else "Reply in this thread with your answer."
        elif looks_like_help_question(typed):
            return None
        else:
            try:
                async with conn.transaction():
                    answered = await answer(conn, ask["id"], authority.person_id, text=typed,
                                            workspace=ctx.workspace)
            except board.BoardError as exc:
                text = str(exc)
            else:
                text = "Answer saved." if answered else "This question is closed. No answer is needed."
    await refresh_ask(ctx, ask["id"])
    return text


async def reply_text(conn, ask, event):
    if board.is_coordinator_ask(ask):
        from illo.board.escalate import typed_text

        return await typed_text(conn, event)
    return event.text
