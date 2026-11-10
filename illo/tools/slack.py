"""Slack tools use server authority and configured destinations."""

from typing import TYPE_CHECKING

from pydantic import Field

if TYPE_CHECKING:
    from illo.app import AppContext

from illo.db import Connection, one
from illo.ledger import people
from illo.tools.registry import Arguments, Registry, Tool, ToolContext


class SendDM(Arguments):
    person: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=35000)


class PostChannel(Arguments):
    channel: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=35000)
    thread_ts: str | None = None


class ReadThread(Arguments):
    channel: str = Field(min_length=1)
    thread_ts: str = Field(min_length=1)


def configured(app: "AppContext", channel: str) -> bool:
    return any(c.id == channel and c.mode != "off" for c in app.workspace.config.slack.channels)


async def send_dm(conn: Connection, app: "AppContext", args: dict) -> dict:
    person = await people.get(conn, args["person"])
    if not person or not person["slack_id"] or not app.slack_client:
        raise ValueError("The person has no Slack destination")
    opened = await app.slack_client.conversations_open(users=person["slack_id"])
    channel = opened["channel"]["id"]
    sent = await app.slack_client.chat_postMessage(
        channel=channel, text=args["text"], unfurl_links=False, unfurl_media=False
    )
    return {"channel": channel, "ts": sent["ts"]}


async def post(app: "AppContext", args: dict) -> dict:
    if not configured(app, args["channel"]) or not app.slack_client:
        raise ValueError("The channel is not configured")
    sent = await app.slack_client.chat_postMessage(**args, unfurl_links=False, unfurl_media=False)
    return {"channel": args["channel"], "ts": sent["ts"]}


async def dm_handler(ctx: ToolContext, args: dict) -> dict:
    return await send_dm(ctx.conn, ctx.app, args)


async def post_handler(ctx: ToolContext, args: dict) -> dict:
    return await post(ctx.app, args)


async def read_handler(ctx: ToolContext, args: dict) -> dict:
    if not ctx.app.slack_client:
        raise ValueError("Slack is not connected")
    if not configured(ctx.app, args["channel"]):
        event = await one(ctx.conn, "SELECT * FROM events WHERE id=%s", (ctx.run["event_id"],))
        data = event["payload"] if event and event["channel"] == "slack" else {}
        payload = data.get("payload", {})
        root = payload.get("thread_ts") or payload.get("message_ts")
        if (
            payload.get("channel_id") != args["channel"]
            or root != args["thread_ts"]
            or event["principal"] != ctx.run["principal"]
        ):
            raise ValueError("The thread is outside this conversation")
    response = await ctx.app.slack_client.conversations_replies(
        channel=args["channel"], ts=args["thread_ts"], limit=50
    )
    return {"messages": response.get("messages", []), "has_more": response.get("has_more", False)}


async def perform_dm(app: "AppContext", draft: dict) -> dict:
    args = SendDM.model_validate(draft["content"]["arguments"]).model_dump()
    if draft["destination"] != f"person:{args['person']}":
        raise ValueError("The draft destination does not match")
    async with app.pool.connection() as conn:
        return await send_dm(conn, app, args)


async def perform_post(app: "AppContext", draft: dict) -> dict:
    args = PostChannel.model_validate(draft["content"]["arguments"]).model_dump()
    if draft["destination"] != f"channel:{args['channel']}":
        raise ValueError("The draft destination does not match")
    return await post(app, args)


def register(registry: Registry) -> None:
    registry.add(
        Tool(
            "slack_send_dm",
            "slack",
            "write_internal",
            lambda a: f"person:{a['person']}",
            False,
            dm_handler,
            "Send a DM to a team member.",
            SendDM,
            profiles=frozenset({"person_dm", "routine"}),
        )
    )
    registry.add(
        Tool(
            "slack_post_channel",
            "slack",
            "write_internal",
            lambda a: f"channel:{a['channel']}",
            False,
            post_handler,
            "Post to a configured team channel.",
            PostChannel,
            profiles=frozenset({"person_dm"}),
        )
    )
    registry.add(
        Tool(
            "slack_read_thread",
            "slack",
            "read",
            lambda a: "ledger",
            False,
            read_handler,
            "Read a configured channel thread or the current conversation.",
            ReadThread,
            profiles=frozenset({"person_dm", "team_channel"}),
        )
    )
