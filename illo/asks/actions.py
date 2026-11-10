"""Handle owner answers without a model run."""

from illo.asks.service import is_live, refresh_ask
from illo.ledger import ask_workflow as ledger
from illo.ledger import asks, rules


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
    notice = None
    async with ctx.pool.connection() as conn:
        ask = await ledger.lock_ask(conn, ask_id)
        if not ask or not in_card_dm(person, action, ask):
            return
        if not await is_live(conn, ask):
            notice = "This question is closed. No answer is needed."
        elif operation == "other":
            await ledger.await_text(conn, ask_id)
            notice = "Reply in this thread with your answer."
        elif operation in {"opt", "always"} and len(parts) == 4:
            option = next((o for o in ask["options"] if o["id"] == parts[3]), None)
            if option is None or (operation == "always" and not ask["allow_rule"]):
                return
            answered = await asks.answer(conn, ask_id, person_id=person["id"], option=option["id"])
            if not answered:
                return
            if operation == "always":
                data = await ledger.card_data(conn, ask)
                lane = data["lane"] or {}
                match = {
                    "ask_kind": ask["kind"],
                    "question_pattern": exact_pattern(ask["question"]),
                }
                if lane.get("source"):
                    match["lane_source"] = lane["source"]
                if lane.get("repo"):
                    match["repository"] = lane["repo"]
                await rules.create(
                    conn,
                    owner_person_id=person["id"],
                    text=ask["question"],
                    kind="always_allow" if ask["permission"] else "standing_answer",
                    match=match,
                    answer_option_label=option["label"],
                    origin="card",
                )
                notice = 'Rule saved. I answer this question for you from now on. Say "show my rules" to see or remove it.'
            await ledger.decision(conn, answered)
        else:
            return
    if notice:
        await thread_notice(ctx, ask, notice)
    await refresh_ask(ctx, ask_id)


async def claim_thread(ctx, event, authority):
    if event.channel != "slack" or authority.profile != "person_dm" or authority.tainted:
        return None
    thread = event.payload.get("thread_ts") or event.reply_to
    channel = event.payload.get("channel_id")
    if not thread or not channel or not channel.startswith("D"):
        return None
    async with ctx.pool.connection() as conn:
        ask = await ledger.thread_ask(conn, authority.person_id, channel, thread)
        if not ask:
            return None
        if not await is_live(conn, ask):
            text = "This question is closed. No answer is needed."
        elif not event.text.strip():
            text = "Reply in this thread with your answer."
        else:
            answered = await asks.answer(
                conn, ask["id"], person_id=authority.person_id, text=event.text
            )
            if answered:
                await ledger.decision(conn, answered)
            text = "Answer saved." if answered else "This question is closed. No answer is needed."
    await refresh_ask(ctx, ask["id"])
    return text
