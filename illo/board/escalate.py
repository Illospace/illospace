"""Deliver board escalations and claim verified replies in their Slack DM roots."""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from psycopg.types.json import Jsonb
from slack_sdk.errors import SlackApiError, SlackRequestError

from illo.asks import cards, service
from illo.board import acl, episodes, packet, policy
from illo.board.decision import unescape_slack
from illo.channels.slack.tasks import _visible
from illo.connectors.http import private_http_logs
from illo.db import all_rows, one
from illo.gateway.dispatch import ClaimRetry
from illo.ledger import agent_oauth, board, sensors
from illo.tasks import pages

log = logging.getLogger(__name__)
ROOT_CLOCK_SKEW_SECONDS = 5
EVENT_TYPE = "illo_board_escalation"
FOOTER_DM = ("_Reply in this thread and I put your answer on the board. "
             "You can also open board thread {id} on the web._")
WRITE_TEXT = "Write your answer as text in this thread. I put typed text only on the board."
REFUSALS = {"channel_not_found", "user_not_found", "not_allowed", "cannot_dm_bot",
            "not_in_channel", "invalid_auth", "missing_scope"}
TS = re.compile(r"^[0-9]{1,14}\.[0-9]{1,9}$")


class DeliveryRefused(RuntimeError):
    """Slack did not accept a DM."""


async def _delivery_lock(conn):
    return (await one(conn, "SELECT pg_try_advisory_xact_lock(12000011) AS acquired"))["acquired"]


async def _locked(conn, thread_id, escalation_id):
    row = await board.lock_thread(conn, thread_id)
    await board.lock_children(conn, thread_id)
    escalation = await one(conn, "SELECT * FROM board_escalations WHERE id=%s AND thread_id=%s",
                           (escalation_id, thread_id))
    return row, escalation


async def _check(conn, thread, row):
    live = await acl.thread_ok(conn, thread["id"], row["acl_snapshot"])
    person = await one(conn, "SELECT * FROM people WHERE id=%s", (row["person_id"],))
    episode = await one(conn, "SELECT * FROM board_episodes WHERE id=%s", (row["episode_id"],))
    if thread["state"] != "open":
        return person, episode, "thread_closed"
    if (not live
            or not await acl.recipient_ok(conn, live, person)
            or (row["recipient_slack_user"] and person["slack_id"] != row["recipient_slack_user"])):
        return person, episode, "acl_lost"
    if episode["state"] not in episodes.ACTIVE or not await episodes.is_pending(conn, episode):
        return person, episode, "no_longer_pending"
    return person, episode, None


async def _claim(ctx, seen):
    async with ctx.pool.connection() as conn:
        if not await _delivery_lock(conn):
            return None
        ref = await one(conn, """SELECT e.id,e.thread_id FROM board_escalations e
            WHERE NOT (e.id=ANY(%s)) AND (
                (e.state='pending' AND e.due_at<=clock_timestamp())
                OR (e.state IN ('sending','closed','answered') AND e.lease_until<clock_timestamp()
                    AND e.send_started_at IS NOT NULL AND e.slack_ts IS NULL AND e.ask_id IS NULL))
            ORDER BY e.due_at,e.id LIMIT 1""", (seen,))
        if not ref:
            return None
        thread, row = await _locked(conn, ref["thread_id"], ref["id"])
        if row["state"] not in {"pending", "sending", "closed", "answered"} or (
            row["lease_until"] and row["lease_until"] > datetime.now(UTC)
        ):
            return ref, None
        person, episode, reason = await _check(conn, thread, row)
        if reason and row["state"] in {"pending", "sending"}:
            if not person or not person.get("slack_id"):
                await _fail(conn, row)
                return ref, None
            await episodes.close_escalation(conn, row, reason)
            await _due_now(conn, episode)
            row["state"] = "closed"
        now = datetime.now(UTC)
        if row["state"] in {"closed", "answered"} and not row["sending_channel"]:
            await conn.execute("UPDATE board_escalations SET lease_until=NULL WHERE id=%s", (row["id"],))
            return ref, None
        if row["state"] in {"pending", "sending"}:
            due = policy.next_window(ctx.workspace, person, now)
            if due > now:
                await conn.execute("""UPDATE board_escalations SET due_at=%s,
                    lease_until=CASE WHEN state='sending' THEN %s ELSE NULL END WHERE id=%s""",
                    (due, due, row["id"]))
                return ref, None
        leased = await one(conn, """UPDATE board_escalations SET
            state=CASE WHEN state='pending' THEN 'sending' ELSE state END,
            lease_id=%s,lease_until=clock_timestamp()+interval '60 seconds',attempts=attempts+1,
            send_started_at=COALESCE(send_started_at,clock_timestamp()),
            recipient_slack_user=COALESCE(recipient_slack_user,%s),
            card_ask_id=CASE WHEN format='card'
                THEN COALESCE(card_ask_id,nextval(pg_get_serial_sequence('asks','id'))) ELSE NULL END
            WHERE id=%s RETURNING *""", (uuid4(), person.get("slack_id") if person else None, row["id"]))
        return leased, person


async def _due_now(conn, episode):
    await conn.execute("UPDATE board_episodes SET next_check_at=clock_timestamp() WHERE id=%s",
                       (episode["id"],))


async def _fail(conn, row):
    await conn.execute("""UPDATE board_escalations SET state='failed',lease_id=NULL,lease_until=NULL
        WHERE id=%s AND state IN ('pending','sending')""", (row["id"],))
    await _due_now(conn, {"id": row["episode_id"]})
    await sensors.error(conn, "board:escalation", "Slack did not accept a board escalation.")


def _metadata(row):
    return {"event_type": EVENT_TYPE, "event_payload": {"escalation_id": row["id"]}}


def _root_id(message, bot):
    if not (message.get("user") == bot["user_id"] or
            (bot.get("bot_id") and message.get("bot_id") == bot["bot_id"])):
        return None
    metadata = message.get("metadata") or {}
    if metadata.get("event_type") != EVENT_TYPE:
        return None
    value = (metadata.get("event_payload") or {}).get("escalation_id")
    return value if type(value) is int and 0 < value < 2**63 else None


async def _history(ctx, channel, **kwargs):
    with private_http_logs():
        return await ctx.slack_client.conversations_history(channel=channel, include_all_metadata=True, **kwargs)


async def _reconcile(ctx, row, channel):
    bot = await ctx.slack_client.auth_test()
    oldest = str(row["send_started_at"].timestamp() - ROOT_CLOCK_SKEW_SECONDS)
    cursor = None
    while True:
        data = await _history(ctx, channel, oldest=oldest, **({"cursor": cursor} if cursor else {}))
        for message in data.get("messages", []):
            if _root_id(message, bot) == row["id"]:
                return message["ts"]
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return None


def _card(ctx, row, person):
    card = row["card"]
    ref = f"board:{row['thread_id']}:{row['post_id'] or 0}:esc:{row['id']}"
    return {"id": row["card_ask_id"], "state": "open", "kind": "decision",
            "question": card["question"], "options": [
                opt | {"id": f"o{i}"} for i, opt in enumerate(card["options"], 1)],
            "recommended_option": f"o{card['recommended'] + 1}" if card["recommended"] is not None else None,
            "allow_rule": False, "source_ref": ref, "owner": person, "proof": {
                "text": row["text"], "thread": f"{pages.base_url(ctx)}/web/board/t/{row['thread_id']}"},
            "page_url": f"{pages.base_url(ctx)}/web/board/t/{row['thread_id']}",
            "page_action": f"web-open-board:{row['thread_id']}"}


async def _remember_channel(ctx, row, channel):
    async with ctx.pool.connection() as conn:
        thread, live = await _locked(conn, row["thread_id"], row["id"])
        if live["lease_id"] != row["lease_id"] or live["lease_until"] <= datetime.now(UTC):
            return None
        if live["state"] != "sending" or not policy.board_pages_ready(ctx):
            return None
        person, episode, reason = await _check(conn, thread, live)
        if reason:
            await episodes.close_escalation(conn, live, reason)
            await _due_now(conn, episode)
            return None
        now = datetime.now(UTC)
        due = policy.next_window(ctx.workspace, person, now)
        if due > now:
            await conn.execute("UPDATE board_escalations SET due_at=%s,lease_until=%s WHERE id=%s",
                               (due, due, row["id"]))
            return None
        if live["content_mode"] == "summary":
            cfg = ctx.workspace.config.board.coordinator
            posts = await all_rows(conn, """SELECT blocks FROM board_posts WHERE thread_id=%s
                ORDER BY id DESC LIMIT 12""", (thread["id"],)) if cfg.slack_summaries else []
            if not cfg.slack_summaries or any(packet.has_picture(post) for post in posts):
                text = await policy.notice(conn, ctx.workspace, episode)
                return await one(conn, """UPDATE board_escalations SET
                    content_mode='notice',format='link',text=%s,card=NULL,card_ask_id=NULL,sending_channel=%s
                    WHERE id=%s RETURNING *""", (text, channel, row["id"]))
        return await one(conn, """UPDATE board_escalations SET sending_channel=%s WHERE id=%s
            RETURNING *""", (channel, row["id"]))


async def _send(ctx, row, person):
    channel = row["sending_channel"]
    if row["attempts"] > 1 and channel:
        if ts := await _reconcile(ctx, row, channel):
            return channel, ts
    if row["state"] in {"closed", "answered"}:
        return None
    if row["attempts"] > 3:
        raise DeliveryRefused
    if not channel:
        opened = await ctx.slack_client.conversations_open(users=row["recipient_slack_user"])
        channel = opened["channel"]["id"]
    if not channel.startswith("D"):
        raise DeliveryRefused
    row = await _remember_channel(ctx, row, channel)
    if not row:
        return None
    if row["format"] == "card":
        ask = _card(ctx, row, person)
        sent = await service.post_dm(ctx, person, cards.fallback(ask), cards.ask_card(ask), "ask", ask["id"],
                                     metadata=_metadata(row), channel=channel)
    else:
        blocks = None
        text = row["text"]
        if row["format"] == "link":
            blocks = [cards.section(text), {"type": "actions", "elements": [
                cards.button("Open the thread", f"web-open-board:{row['thread_id']}", True)]}]
        else:
            text += "\n\n" + FOOTER_DM.format(id=row["thread_id"])
        sent = await service.post_dm(ctx, person, text, blocks, "board-escalation", row["id"],
                                     metadata=_metadata(row), channel=channel)
    if not sent or not sent.get("ts"):
        raise DeliveryRefused
    return channel, sent["ts"]


async def _insert_card(conn, ctx, row, person, channel, ts, *, cancelled=False):
    ask = _card(ctx, row, person)
    expires = datetime.now(UTC) + timedelta(hours=ctx.workspace.config.board.coordinator.escalation_timeout_hours)
    digest = cards.digest(cards.ask_card(ask))
    await conn.execute("""INSERT INTO asks
        (id,owner_person_id,question,kind,options,recommended_option,proof,proof_order,allow_rule,source_ref,
         slack_channel,slack_ts,card_sent_at,card_digest,card_state,card_delivery_state,state,cancelled_at,expires_at)
        OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,'decision',%s,%s,%s,%s,false,%s,%s,%s,
          clock_timestamp(),%s,'open','none',%s,CASE WHEN %s THEN clock_timestamp() ELSE NULL END,%s)
        ON CONFLICT (id) DO NOTHING""",
        (ask["id"], row["person_id"], ask["question"], Jsonb(ask["options"]), ask["recommended_option"],
         Jsonb(ask["proof"]), Jsonb(list(ask["proof"])), ask["source_ref"], channel, ts,
         digest, "cancelled" if cancelled else "open", cancelled, expires))
    await conn.execute("UPDATE board_escalations SET ask_id=%s WHERE id=%s", (ask["id"], row["id"]))


async def _record_send(conn, ctx, thread, row, person, channel, ts):
    if row["state"] not in {"sending", "closed", "answered"} or row["sending_channel"] != channel:
        return
    card = row["format"] == "card"
    if row["state"] == "sending":
        _, episode, reason = await _check(conn, thread, row)
        if reason:
            await episodes.close_escalation(conn, row, reason)
            await _due_now(conn, episode)
            row["state"] = "closed"
    if card:
        await _insert_card(conn, ctx, row, person, channel, ts, cancelled=row["state"] != "sending")
    result = await one(conn, """UPDATE board_escalations SET state='sent',
        slack_channel=%s,slack_ts=%s,sent_at=clock_timestamp(),lease_id=NULL,lease_until=NULL
        WHERE id=%s AND lease_id=%s AND state='sending' RETURNING id""",
        (None if card else channel, None if card else ts, row["id"], row["lease_id"]))
    if not result:
        if row["state"] in {"closed", "answered"}:
            await conn.execute("""UPDATE board_escalations SET slack_channel=%s,slack_ts=%s,
                sent_at=clock_timestamp(),lease_id=NULL,lease_until=NULL
                WHERE id=%s AND state IN ('closed','answered') AND lease_id=%s""",
                (None if card else channel, None if card else ts, row["id"], row["lease_id"]))
        return
    now = datetime.now(UTC)
    await conn.execute("UPDATE board_episodes SET next_check_at=%s WHERE id=%s",
                       (now + timedelta(hours=ctx.workspace.config.board.coordinator.escalation_timeout_hours),
                        row["episode_id"]))
    await board.system_post(conn, thread["id"], f"I asked {person['name']} in Slack at {now:%H:%M} UTC. "
                            "Their reply comes to this thread.", purpose="escalated", episode_id=row["episode_id"],
                            source_ref=f"coord:{row['episode_id']}:sent:{row['id']}")


def _terminal(exc):
    return isinstance(exc, DeliveryRefused) or (
        isinstance(exc, SlackApiError) and exc.response.get("error") in REFUSALS)


async def _deliver(ctx, row, person):
    try:
        with private_http_logs():
            async with asyncio.timeout(45):
                sent = await _send(ctx, row, person)
    except (TimeoutError, OSError, SlackApiError, SlackRequestError, DeliveryRefused, httpx.TransportError) as exc:
        if not _terminal(exc) and row["attempts"] < 3:
            return
        async with ctx.pool.connection() as conn:
            _, live = await _locked(conn, row["thread_id"], row["id"])
            if live["lease_id"] == row["lease_id"] and live["state"] == "sending":
                await _fail(conn, live)
            elif live["lease_id"] == row["lease_id"] and live["state"] in {"closed", "answered"}:
                await conn.execute("UPDATE board_escalations SET lease_until=NULL WHERE id=%s", (row["id"],))
        return
    async with ctx.pool.connection() as conn:
        thread, live = await _locked(conn, row["thread_id"], row["id"])
        if live["lease_id"] != row["lease_id"] or live["lease_until"] <= datetime.now(UTC):
            return
        if sent:
            await _record_send(conn, ctx, thread, live, person, *sent)
        elif live["state"] in {"closed", "answered"}:
            await conn.execute("UPDATE board_escalations SET lease_until=NULL WHERE id=%s", (row["id"],))


async def _sweep_cards(ctx):
    from illo.board.asks import on_answer

    async with ctx.pool.connection() as conn:
        if not await _delivery_lock(conn):
            return
        rows = await all_rows(conn, """SELECT e.id,e.thread_id,e.ask_id FROM board_escalations e
            JOIN asks a ON a.id=e.ask_id WHERE e.state IN ('sending','sent')
            AND a.state IN ('cancelled','expired','answered') ORDER BY e.id LIMIT 20""")
    for ref in rows:
        try:
            async with ctx.pool.connection() as conn:
                ask = await board.lock_for_ask(conn, ref["ask_id"])
                if not ask:
                    continue
                row = await one(conn, "SELECT * FROM board_escalations WHERE id=%s", (ref["id"],))
                if not row or row["state"] not in {"sending", "sent"}:
                    continue
                if ask["state"] in {"expired", "cancelled"}:
                    await episodes.close_escalation(conn, row, "ask_" + ask["state"])
                    await _due_now(conn, {"id": row["episode_id"]})
                elif ask["state"] == "answered":
                    await on_answer(conn, ask, workspace=ctx.workspace)
                    current = await one(conn, "SELECT state FROM board_escalations WHERE id=%s", (row["id"],))
                    if current["state"] == "sent":
                        await episodes.close_escalation(conn, row, "acl_lost")
                        await _due_now(conn, {"id": row["episode_id"]})
        except Exception as exc:
            log.error("Could not reconcile board escalation %s (%s).", ref["id"], type(exc).__name__)


async def _notices(ctx):
    async with ctx.pool.connection() as conn:
        if not await _delivery_lock(conn):
            return
        rows = await all_rows(conn, """SELECT id,thread_id FROM board_escalations
            WHERE state IN ('closed','answered') AND notice_at IS NULL
              AND (state='closed' AND (ask_id IS NOT NULL OR slack_ts IS NOT NULL)
                OR state='answered' AND EXISTS (SELECT 1 FROM asks a WHERE a.id=ask_id AND a.state='cancelled'))
            ORDER BY id LIMIT 20""")
    for ref in rows:
        async with ctx.pool.connection() as conn:
            thread, row = await _locked(conn, ref["thread_id"], ref["id"])
            if row["notice_at"] or row["state"] not in {"closed", "answered"}:
                continue
            await conn.execute("UPDATE board_escalations SET notice_at=clock_timestamp() WHERE id=%s", (row["id"],))
            person = await one(conn, "SELECT * FROM people WHERE id=%s", (row["person_id"],))
            name = await one(conn, "SELECT name FROM people WHERE id=%s", (row["closed_by_person_id"],))
            allowed = await acl.recipient_ok(conn, thread, person) and (
                person["slack_id"] == row["recipient_slack_user"])
        if row["ask_id"]:
            try:
                await service.refresh_ask(ctx, row["ask_id"])
            except (SlackApiError, SlackRequestError, TimeoutError, OSError, httpx.TransportError):
                # The ask worker can retry this idempotent card update.
                continue
        elif allowed and row["closed_reason"] in {"other_answer", "thread_closed"}:
            text = (f"{cards.escape(name['name'])} answered on the board. Nothing needed from you."
                    if name else "This board thread is closed. Nothing needed from you.")
            try:
                await ctx.slack_client.chat_postMessage(channel=row["slack_channel"], thread_ts=row["slack_ts"],
                                                        text=text, client_msg_id=service.message_id("board-close", row["id"]))
            except (SlackApiError, SlackRequestError, TimeoutError, OSError, httpx.TransportError):
                async with ctx.pool.connection() as conn:
                    await sensors.error(conn, "board:escalation", "A board close notice could not be confirmed.")


async def send_due(ctx):
    if not await policy.ready(ctx):
        return
    await _sweep_cards(ctx)
    if not ctx.slack_client:
        return
    seen = []
    for _ in range(5):
        claimed = await _claim(ctx, seen)
        if not claimed:
            break
        row, person = claimed
        seen.append(row["id"])
        if person or row.get("state") in {"closed", "answered"}:
            try:
                await _deliver(ctx, row, person)
            except Exception:
                log.error("Could not deliver board escalation %s.", row["id"])
    await _notices(ctx)


async def typed_text(conn, event):
    value = unescape_slack(event.payload.get("raw_slack_text", ""))
    names = {p["slack_id"]: p["name"] for p in await all_rows(conn,
             "SELECT slack_id,name FROM people WHERE active AND slack_id IS NOT NULL")}

    def replace(match):
        inner = match[1]
        if re.fullmatch(r"@U[A-Z0-9]+", inner):
            return "@" + names.get(inner[1:], "someone")
        if re.fullmatch(r"#C[A-Z0-9]+\|[^|]+", inner):
            return "#" + inner.split("|", 1)[1]
        if inner in {"!here", "!channel", "!everyone"}:
            return ""
        if re.match(r"(?:[a-zA-Z][a-zA-Z0-9+.-]*://|mailto:|www\.)", inner):
            url, sep, label = inner.partition("|")
            return f"{label} ({url})" if sep else url
        return match[0]

    return _visible(re.sub(r"<([^<>\n]+)>", replace, value)).strip()


async def _verified_root(ctx, channel, root):
    history = await _history(ctx, channel, latest=root, inclusive=True, limit=1)
    for message in history.get("messages", []):
        if message.get("ts") == root and (message.get("metadata") or {}).get("event_type") == EVENT_TYPE:
            bot = await ctx.slack_client.auth_test()
            return _root_id(message, bot)
    return None


async def _recover_root(ctx, person_id, sender, channel, root, *, card_ask_id=None):
    async with asyncio.timeout(10):
        escalation_id = await _verified_root(ctx, channel, root)
    if not escalation_id:
        return None
    async with ctx.pool.connection() as conn:
        ref = await one(conn, """SELECT id,thread_id FROM board_escalations WHERE id=%s AND person_id=%s
            AND recipient_slack_user=%s AND sending_channel=%s
            AND (%s::bigint IS NULL OR card_ask_id=%s)""",
            (escalation_id, person_id, sender, channel, card_ask_id, card_ask_id))
        if not ref:
            return None
        thread, row = await _locked(conn, ref["thread_id"], ref["id"])
        if (row["state"] not in {"sending", "closed", "answered"} or row["slack_ts"] or row["ask_id"]
                or row["recipient_slack_user"] != sender or row["sending_channel"] != channel):
            return ref if row["slack_ts"] == root else None
        person = await one(conn, "SELECT * FROM people WHERE id=%s AND slack_id=%s AND active", (person_id, sender))
        if not person:
            return None
        await _record_send(conn, ctx, thread, row, person, channel, root)
        return ref if row["format"] != "card" else None


async def recover_card(ctx, person, ask_id, action):
    channel, root = action.get("channel"), action.get("message_ts")
    if not (ctx.slack_client and channel and channel.startswith("D") and root and TS.fullmatch(root)):
        return
    async with ctx.pool.connection() as conn:
        pending = await one(conn, """SELECT 1 FROM board_escalations WHERE card_ask_id=%s AND ask_id IS NULL
            AND state IN ('sending','closed','answered') AND person_id=%s AND sending_channel=%s
            AND recipient_slack_user=%s""", (ask_id, person["id"], channel, person["slack_id"]))
    if pending:
        await _recover_root(ctx, person["id"], person["slack_id"], channel, root, card_ask_id=ask_id)


async def _find_root(ctx, person_id, sender, channel, root):
    stamp = float(root)
    async with ctx.pool.connection() as conn:
        # Only look in Slack when the root falls in a possible send window.
        ref = await one(conn, """SELECT id,thread_id,slack_ts FROM board_escalations WHERE person_id=%s AND (
            (slack_channel=%s AND slack_ts=%s) OR (
                %s AND recipient_slack_user=%s AND sending_channel=%s
                AND state IN ('sending','closed','answered') AND slack_ts IS NULL AND ask_id IS NULL
                AND extract(epoch FROM send_started_at)<=%s AND extract(epoch FROM lease_until)>=%s
                AND (state='sending' OR send_started_at>now()-interval '2 minutes')))
            ORDER BY slack_ts NULLS LAST LIMIT 1""",
            (person_id, channel, root, ctx.slack_client is not None, sender, channel,
             stamp + ROOT_CLOCK_SKEW_SECONDS, stamp - ROOT_CLOCK_SKEW_SECONDS))
    if not ref or ref["slack_ts"]:
        return ref
    return await _recover_root(ctx, person_id, sender, channel, root)


async def claim_reply(ctx, event, authority):
    if event.channel != "slack" or authority.profile != "person_dm" or authority.tainted:
        return None
    channel, root = event.payload.get("channel_id"), event.payload.get("thread_ts")
    message_ts = event.payload.get("message_ts")
    if not (channel and channel.startswith("D") and root and TS.fullmatch(root)
            and message_ts and TS.fullmatch(message_ts)):
        return None
    try:
        ref = await _find_root(ctx, authority.person_id, event.sender, channel, root)
    except (SlackApiError, SlackRequestError, TimeoutError, OSError, httpx.TransportError) as exc:
        if _terminal(exc):
            return None
        async with ctx.pool.connection() as conn:
            retry = await agent_oauth.limit(conn, "board_root_retry", f"{channel}:{message_ts}", 3)
        if retry:
            raise ClaimRetry from None
        return "I could not check that message. Reply again in a minute."
    if not ref:
        return None
    return await _post_reply(ctx, event, authority, ref, channel, root, message_ts)


async def _post_reply(ctx, event, authority, ref, channel, root, message_ts):
    async with ctx.pool.connection() as conn:
        thread, row = await _locked(conn, ref["thread_id"], ref["id"])
        if (row["slack_channel"], row["slack_ts"], row["person_id"], row["recipient_slack_user"]) != (
            channel, root, authority.person_id, event.sender
        ):
            return None
        if float(root) < (datetime.now(UTC) - timedelta(days=14)).timestamp():
            return "This board question is old. Reply on the thread page instead."
        if thread["state"] != "open":
            return "That board thread is closed, so I did not post your reply."
        person = await one(conn, "SELECT * FROM people WHERE id=%s AND slack_id=%s AND active",
                           (authority.person_id, event.sender))
        if (not await acl.recipient_ok(conn, thread, person) or (
            row["state"] == "closed" and row["closed_reason"] not in {"other_answer", "no_longer_pending"}
        )):
            return "I cannot put this reply on the board. Open the board to see what you can reach."
        text = await typed_text(conn, event)
        if not text:
            return WRITE_TEXT
        first = row["state"] == "sent"
        try:
            async with conn.transaction():
                receipt = await board.human_post(
                    conn, workspace=ctx.workspace, person=person, thread_id=thread["id"], text=text,
                    reply_to_post_id=await episodes.question_reply(conn, row) if first else None,
                    source_ref=f"slack:{channel}:{message_ts}", via="slack_thread")
                if first:
                    await episodes.close_escalation(conn, row, "other_answer",
                                                    person_id=person["id"], post_id=receipt["post_id"])
        except board.BoardError as exc:
            return str(exc)
        result = f"Your answer is on board thread {thread['id']}. The agents see it now."
        if row["state"] == "closed" and row["closed_reason"] == "other_answer":
            name = await one(conn, "SELECT name FROM people WHERE id=%s", (row["closed_by_person_id"],))
            result += f" {cards.escape(name['name']) if name else 'Someone'} answered first, so this is an extra note."
        if event.payload.get("files"):
            result += " Pictures stay in Slack."
        return result
