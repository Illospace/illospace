"""Read missed Slack messages after a connection gap."""

import asyncio
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from psycopg.types.json import Jsonb

from illo.channels.slack.channels import member_channels
from illo.channels.slack.files import private_io, slack_call
from illo.db import one
from illo.ledger import people
from illo.slackapi import dm_person

# Start a catch-up after more than one minute without a good check.
MIN_GAP_S = 60
# Read one minute before the last good check.
MARGIN_S = 60
# Read at most three days of history.
MAX_LOOKBACK_S = 3 * 86400
# Answer messages up to 24 hours old.
ANSWER_AGE_S = 24 * 3600
# Tell the owner after a gap of more than 10 minutes.
OWNER_NOTICE_GAP_S = 600
# Read up to 200 messages and replies in each conversation.
MAX_MESSAGES = 200
# Stop each Slack call after 15 seconds.
SLACK_TIMEOUT_S = 15


@dataclass
class CatchupResult:
    recent: int = 0
    answered: int = 0
    too_old: int = 0
    unread_conversations: int = 0
    discovery_failures: int = 0
    intake_failures: int = 0
    notice_failures: int = 0

    @property
    def missed(self) -> int:
        # Count messages sent to intake and old messages that qualify for a DM.
        return self.recent + self.too_old

    @property
    def failed(self) -> bool:
        return any(
            (
                self.unread_conversations,
                self.discovery_failures,
                self.intake_failures,
                self.notice_failures,
            )
        )


async def _call(method, **kwargs):
    async with asyncio.timeout(SLACK_TIMEOUT_S):
        return await slack_call(method, **kwargs)


async def _pages(method, field, *, time_bound=None, remaining=None, **kwargs):
    cursor, seen = None, set()
    while True:
        if remaining:
            kwargs["limit"] = remaining()
        response = await _call(method, cursor=cursor, **kwargs)
        entries = response.get(field)
        if response.get("ok") is False or not isinstance(entries, list):
            raise ValueError("Slack refused the read")
        yield entries
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if cursor:
            if cursor in seen:
                raise ValueError("Slack repeated a cursor")
            seen.add(cursor)
        elif response.get("has_more"):
            if not time_bound or not entries:
                raise ValueError("Slack did not finish the read")
            # Slack timestamps use microseconds. Move past the last message on this page.
            step = Decimal("0.000001")
            last = Decimal(entries[-1]["ts"])
            bound = last - step if time_bound == "latest" else last + step
            previous = Decimal(kwargs[time_bound])
            if (time_bound == "latest" and bound >= previous) or (
                time_bound == "oldest" and bound <= previous
            ):
                raise ValueError("Slack repeated a history page")
            kwargs[time_bound] = f"{bound:.6f}"
            seen.clear()
        else:
            return


async def _conversations(ctx, result):
    async with ctx.pool.connection() as conn:
        active = {p["slack_id"] for p in await people.list_people(conn) if p["slack_id"]}
    conversations = {}
    try:
        async for page in _pages(
            ctx.slack_client.users_conversations, "channels", types="im", limit=200
        ):
            conversations.update({im["id"]: "im" for im in page if im.get("user") in active})
    except Exception:
        result.discovery_failures += 1

    slack = ctx.workspace.config.slack
    channels = {entry.id for entry in slack.channels}
    try:
        channels.update(await _call(member_channels, ctx=ctx))
    except Exception:
        result.discovery_failures += 1
    conversations.update({c: "channel" for c in channels if slack.channel(c).mode != "off"})
    return conversations


async def _read_messages(method, messages, oldest, latest, *, parent_ts=None, **kwargs):
    if len(messages) >= MAX_MESSAGES:
        return
    async for page in _pages(
        method,
        "messages",
        time_bound="oldest" if parent_ts else "latest",
        oldest=f"{oldest:.6f}",
        latest=f"{latest:.6f}",
        inclusive=True,
        remaining=lambda: MAX_MESSAGES - len(messages),
        **kwargs,
    ):
        for message in page:
            ts = message.get("ts")
            if ts and ts != parent_ts and oldest <= float(ts) <= latest:
                messages[ts] = message
                if len(messages) == MAX_MESSAGES:
                    return


async def _read_conversation(client, channel, oldest, latest, result):
    messages = {}
    failed = False
    try:
        await _read_messages(
            client.conversations_history, messages, oldest, latest, channel=channel
        )
    except Exception:
        failed = True
    # Slack history omits replies to parents older than the window.
    for ts, parent in list(messages.items()):
        if len(messages) >= MAX_MESSAGES:
            break
        try:
            if parent.get("latest_reply") and float(parent["latest_reply"]) >= oldest:
                await _read_messages(
                    client.conversations_replies,
                    messages,
                    oldest,
                    latest,
                    channel=channel,
                    ts=ts,
                    parent_ts=ts,
                )
        except Exception:
            failed = True
    result.unread_conversations += int(failed)
    return messages


async def _record_old(ctx, intake, message, kind, external_id):
    # Use the checks of live intake: a mention, or a reply in a thread that Illo follows.
    if (kind == "message" and f"<@{intake.bot_id}>" not in (message.get("text") or "")
            and not (message.get("thread_ts")
                     and await intake.follows(message["channel"], message["thread_ts"]))):
        return None
    if team := message.get("user_team"):
        if not intake.team_id:
            intake.team_id = (await _call(ctx.slack_client.auth_test))["team_id"]
        if team != intake.team_id:
            return None
    async with ctx.pool.connection() as conn:
        person = await people.by_slack_id(conn, message.get("user"))
        if not person:
            return None
        payload = {
            "payload": {"channel_id": message["channel"], "message_ts": message["ts"]},
            "missed": True,
        }
        row = await one(
            conn,
            """
            INSERT INTO events (channel,external_id,kind,payload,status)
            VALUES ('slack',%s,%s,%s,'ignored')
            ON CONFLICT (channel,external_id) DO NOTHING RETURNING id
        """,
            (external_id, kind, Jsonb(payload)),
        )
        return person["key"] if row else None


async def _send_notice(ctx, person_key, text, result):
    try:
        if await _call(dm_person, ctx=ctx, person_key=person_key, text=text):
            return
    except Exception:
        pass
    result.notice_failures += 1


def _gap_times(start, end):
    return (
        f"from {datetime.fromtimestamp(start, UTC):%Y-%m-%d %H:%M:%S} to "
        f"{datetime.fromtimestamp(end, UTC):%Y-%m-%d %H:%M:%S} (UTC)"
    )


async def _notify(ctx, gap_start, gap_end, old_by_person, result):
    times = _gap_times(gap_start, gap_end)
    for person_key, count in old_by_person.items():
        messages = "1 message" if count == 1 else f"{count} messages"
        pronoun = "it" if count == 1 else "them"
        await _send_notice(
            ctx,
            person_key,
            f"Illo was not reachable {times}. "
            f"It missed {messages} from you. The {'message is' if count == 1 else 'messages are'} "
            f"older than 24 hours. Illo did not answer {pronoun}. "
            f"Send {pronoun} again if you still need an answer. "
            "A button press in that time did not reach Illo. Press it again.",
            result,
        )

    duration = gap_end - gap_start
    if duration <= OWNER_NOTICE_GAP_S:
        return
    hours, minutes = divmod(int(duration // 60), 60)
    text = (
        f"Illo was not reachable {times} ({hours} h {minutes} min). "
        f"Illo found {result.missed} missed {'message' if result.missed == 1 else 'messages'}. "
        f"It answered {result.answered} after the connection came back. "
        f"{result.too_old} {'was' if result.too_old == 1 else 'were'} too old to answer."
    )
    if result.too_old:
        text += (
            " Illo could not send a DM to every sender of an old message."
            if result.notice_failures
            else " Illo sent a DM to each sender of an old message."
        )
    if result.unread_conversations:
        count = result.unread_conversations
        text += f" Illo could not read all messages in {count} {'conversation' if count == 1 else 'conversations'}."
    if result.discovery_failures:
        text += " Illo could not get a complete list of conversations."
    text += " A button press in the gap cannot be read from the Slack history."
    await _send_notice(ctx, ctx.workspace.config.default_owner, text, result)


async def catch_up(ctx, intake, gap_start: float, gap_end: float) -> CatchupResult:
    """Send missed messages to intake and tell people about older messages."""
    oldest = max(gap_start - MARGIN_S, time.time() - MAX_LOOKBACK_S)
    result = CatchupResult()
    collected = []
    for channel, channel_type in (await _conversations(ctx, result)).items():
        messages = await _read_conversation(ctx.slack_client, channel, oldest, gap_end, result)
        for message in messages.values():
            if intake.accepts_message(message):
                event = {
                    **message,
                    "type": "message",
                    "channel": channel,
                    "channel_type": channel_type,
                }
                if "user_team" not in event:
                    event["user_team"] = event.get("team")
                collected.append(event)

    old_by_person = Counter()
    for event in sorted(collected, key=lambda m: float(m["ts"])):
        external_id = f"{event['channel']}:{event['ts']}"
        try:
            async with ctx.pool.connection() as conn:
                if await one(
                    conn,
                    "SELECT id FROM events WHERE channel='slack' AND external_id=%s",
                    (external_id,),
                ):
                    continue
            if time.time() - float(event["ts"]) > ANSWER_AGE_S:
                kind = "dm" if event["channel_type"] == "im" else "message"
                if person_key := await _record_old(ctx, intake, event, kind, external_id):
                    old_by_person[person_key] += 1
                    result.too_old += 1
                continue
            result.recent += 1
            with private_io():
                outcome = await intake.receive({"type": "event_callback", "event": event})
            if outcome and outcome.get("duplicate"):
                # A live event can reach the gateway after the check above.
                result.recent -= 1
            elif outcome and outcome.get("error"):
                result.intake_failures += 1
            elif outcome and outcome.get("text"):
                result.answered += 1
        except Exception:
            result.intake_failures += 1

    await _notify(ctx, gap_start, gap_end, old_by_person, result)
    return result
