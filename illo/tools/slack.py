"""Slack tools use server authority and configured destinations."""

from typing import TYPE_CHECKING

from pydantic import Field

from illo.channels.slack.access import channel_private, require_member
from illo.channels.slack.blocks import message_text

if TYPE_CHECKING:
    from illo.app import AppContext

from illo.db import Connection, one
from illo.ledger import people
from illo.tools._format import data_text, header, line
from illo.tools.checks import Decision
from illo.tools.registry import Arguments, Registry, Tool, ToolContext


class SendDM(Arguments):
    person: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=35000)


class PostChannel(Arguments):
    channel: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=35000)
    thread_ts: str | None = None


class ReadThread(Arguments):
    limit: int = Field(default=15, ge=1, le=40)
    offset: int = Field(default=0, ge=0)
    channel: str = Field(min_length=1)
    thread_ts: str = Field(min_length=1)


def configured(app: "AppContext", channel: str) -> bool:
    return channel.startswith(("C", "G")) and app.workspace.config.slack.channel(channel).mode != "off"


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


async def post(app: "AppContext", args: dict, *, conn=None) -> dict:
    if not configured(app, args["channel"]) or not app.slack_client:
        raise ValueError("The channel is not configured")
    if await channel_private(app, args["channel"], conn=conn):
        raise ValueError("The channel is private")
    sent = await app.slack_client.chat_postMessage(**args, unfurl_links=False, unfurl_media=False)
    return {"channel": args["channel"], "ts": sent["ts"]}


async def dm_handler(ctx: ToolContext, args: dict) -> dict:
    return await send_dm(ctx.conn, ctx.app, args)


async def post_preflight(ctx: ToolContext, args: dict) -> Decision:
    # A draft does not contact Slack. Approval rechecks the channel.
    if ctx.run.get("personal_read"):
        return Decision("allow", "The approved send checks channel visibility")
    if not configured(ctx.app, args["channel"]) or not ctx.app.slack_client:
        return Decision("deny", "The channel is not configured")
    if await channel_private(ctx.app, args["channel"], conn=ctx.conn):
        return Decision("deny", "The channel is private")
    return Decision("allow", "The channel is visible to the team")


async def post_handler(ctx: ToolContext, args: dict) -> dict:
    return await post(ctx.app, args, conn=ctx.conn)


async def read_handler(ctx: ToolContext, args: dict) -> dict:
    if not ctx.app.slack_client:
        raise ValueError("Slack is not connected")
    private = True
    same_channel = False
    if configured(ctx.app, args["channel"]):
        private = await channel_private(ctx.app, args["channel"], conn=ctx.conn)
        if private:
            person = None
            if ctx.principal.startswith("person:"):
                person = await people.get(ctx.conn, ctx.principal[7:])
                if person and person["id"] != ctx.run["person_id"]:
                    person = None
            elif ctx.run.get("profile") == "team_channel" and ctx.run.get("event_id"):
                event = await one(ctx.conn, "SELECT * FROM events WHERE id=%s", (ctx.run["event_id"],))
                payload = (event or {}).get("payload", {}).get("payload", {})
                if (event and event["channel"] == "slack" and event["principal"] == "team"
                        and payload.get("channel_id") == args["channel"]
                        and not event["tainted"]):
                    person = await people.by_slack_id(ctx.conn, payload.get("author_slack_id", ""))
                    same_channel = True
            if not person or not person["slack_id"]:
                raise ValueError("A private channel needs a run of its member")
            await require_member(ctx.app, args["channel"], person["slack_id"])
    else:
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
    messages = response.get("messages", [])
    page = messages[args["offset"] : args["offset"] + args["limit"]]
    text = [header(len(page), len(messages), "Pass offset= / limit= to read more messages.")]
    text.extend(line(m.get("user"), m.get("ts"), message_text(m)) for m in page)
    if response.get("has_more"):
        text.append("The thread has more messages at the source.")
    return {
        "text": data_text("slack", "\n".join(text)),
        "has_more": response.get("has_more", False),
        "visibility": "members" if same_channel else "private" if private else "team",
    }


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
            "Send a Slack DM to one team member (`person`: a person key). For a question that needs an answer, use `asks_create` instead. It becomes a draft card after a personal or private read. At most 3 per run.",
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
            "Post in a configured team channel (`channel`: its id; `thread_ts` to answer in a thread). It becomes a draft card after a personal or private read.",
            PostChannel,
            profiles=frozenset({"person_dm"}),
            preflight=post_preflight,
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
            "Read the current Slack thread, or a thread in a configured channel: `channel` is the channel id and `thread_ts` the timestamp of its first message. Pass `offset` for more messages.",
            ReadThread,
            profiles=frozenset({"person_dm", "team_channel"}),
        )
    )
