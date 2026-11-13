"""Send deep results through the Slack adapter and its text formatter."""

from illo.channels.slack.access import channel_private
from illo.channels.slack.adapter import SlackAdapter
from illo.channels.slack.formatting import mrkdwn
from illo.db import one
from illo.gateway import InboundEvent
from illo.tools.registry import Refusal


def place(deliver):
    if "slack_thread" in deliver:
        target = deliver["slack_thread"]
        return f"Slack thread {target['channel_id']} / {target['thread_ts']}"
    if "slack_dm" in deliver:
        return "the person's Slack DM"
    if "slack_channel" in deliver:
        return "Slack channel " + deliver["slack_channel"]["channel_id"]
    return "the stored run log"


async def destination(ctx, conn, row):
    deliver = row["deliver"]
    if deliver.get("none"):
        return None, None
    if "slack_dm" in deliver:
        person = await one(
            conn,
            "SELECT slack_id FROM people WHERE id=%s AND active",
            (deliver["slack_dm"]["person_id"],),
        )
        if not person or not person["slack_id"]:
            raise Refusal("The person has no Slack DM.")
        response = await ctx.slack_client.conversations_open(users=person["slack_id"])
        return response["channel"]["id"], None
    target = deliver.get("slack_thread") or deliver["slack_channel"]
    channel, thread = target["channel_id"], target.get("thread_ts")
    if channel.startswith("D"):
        return channel, thread
    config = ctx.workspace.config.slack.channel(channel)
    if config.mode == "off":
        raise Refusal("This Slack channel is off.")
    if await channel_private(ctx, channel, conn=conn):
        origin = await one(
            conn, "SELECT payload,tainted FROM events WHERE id=%s", (row.get("origin_event_id"),)
        )
        payload = origin["payload"].get("payload", {}) if origin else {}
        if not (
            origin
            and not origin["tainted"]
            and payload.get("mentioned")
            and payload.get("channel_id") == channel
        ):
            raise Refusal("A private channel needs a mention in that channel.")
    return channel, thread


async def send(ctx, row, text):
    if row["deliver"].get("none"):
        return
    if not ctx.slack_client:
        raise Refusal("Slack is not available.")
    async with ctx.pool.connection() as conn:
        channel, thread = await destination(ctx, conn, row)
    event = InboundEvent(
        channel="slack",
        external_id=f"deep:{row['id']}",
        kind="deep",
        payload={"channel_id": channel, "delivery_key": f"deep:{row['id']}"},
        reply_to=thread,
    )
    await SlackAdapter(ctx).deliver_final(event, mrkdwn(text))
