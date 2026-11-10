"""Manage only the ChatGPT sign-in of the person of this DM."""

from dataclasses import replace

from illo.connectors import chatgpt_oauth
from illo.db import one
from illo.gateway import InboundEvent
from illo.ledger import chatgpt as grants
from illo.tools.registry import Tool


async def owner(ctx):
    person = await one(
        ctx.conn, "SELECT id,key FROM people WHERE id=%s AND active", (ctx.run["person_id"],)
    )
    if (
        ctx.run["profile"] != "person_dm"
        or ctx.run["tainted"]
        or not person
        or ctx.principal != "person:" + person["key"]
    ):
        raise ValueError("The tool needs the person's own DM")
    return person


async def connect(ctx, args):
    person = await owner(ctx)
    row = await one(
        ctx.conn, "SELECT channel,kind,payload FROM events WHERE id=%s", (ctx.run["event_id"],)
    )
    if (
        not row
        or row["channel"] != "slack"
        or row["kind"] not in {"dm", "message.im"}
        or not row["payload"].get("payload", {}).get("channel_id")
    ):
        raise ValueError("Connect ChatGPT in your Slack DM")
    event = InboundEvent(
        channel="slack",
        external_id="tool",
        kind="dm",
        payload=row["payload"].get("payload", {}),
        reply_to=row["payload"].get("reply_to"),
    )
    link = await chatgpt_oauth.issue(ctx.app, person["id"], event, conn=ctx.conn)
    # Send the one-time link directly. A model never needs the state or the device code.
    await ctx.app.slack_client.chat_postMessage(
        channel=event.payload["channel_id"],
        thread_ts=event.reply_to,
        text="Open this one-time link to connect your ChatGPT:\n"
        + link
        + "\nIf you use the paste flow, paste the final local address here, in this same DM and thread.",
        unfurl_links=False,
        unfurl_media=False,
    )
    return {"message": "I sent the sign-in link to your DM."}


async def status(ctx, args):
    person = await owner(ctx)
    return {"message": chatgpt_oauth.status_text((await grants.status(ctx.conn, person["id"]))[0])}


async def disconnect(ctx, args):
    person = await owner(ctx)
    await grants.disconnect(ctx.conn, person["id"])
    return {"message": "ChatGPT is disconnected from Illo."}


def register(registry):
    for name, risk, handler in (
        ("chatgpt_connect", "write_internal", connect),
        ("chatgpt_status", "read", status),
        ("chatgpt_disconnect", "write_internal", disconnect),
    ):
        tool = Tool(
            name,
            "chatgpt",
            risk,
            lambda a: "person:<self>",
            False,
            handler,
            description="Manage your own ChatGPT sign-in in this DM.",
            profiles=frozenset({"person_dm"}),
        )

        def resolve(ctx, args, tool=tool):
            return replace(tool, destination=lambda a: ctx.principal)

        registry.add(replace(tool, resolve=resolve))
