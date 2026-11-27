"""Slack tools use server authority and configured destinations."""

import re
from typing import TYPE_CHECKING

from pydantic import Field

from illo.channels.slack.access import channel_private, require_member
from illo.channels.slack.blocks import message_text
from illo.channels.slack.channels import (
    ChannelLookupError,
    cached_public_member_channels,
    public_member_channels,
    resolve_channel,
)

if TYPE_CHECKING:
    from illo.app import AppContext

from illo.db import Connection, all_rows, one
from illo.ledger import people
from illo.tools import _comms_guard as comms_guard
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


async def _resolve_person(conn: Connection, value: str) -> dict | None:
    person = await people.get(conn, value)
    if person and person.get("active"):
        return person
    lower = value.strip().lower()
    if not lower:
        return None
    members = await all_rows(conn, "SELECT * FROM people WHERE active")
    matches = [m for m in members if lower in {m["key"].lower(), m["name"].lower()}]
    return matches[0] if len(matches) == 1 else None


async def dm_preflight(ctx: ToolContext, args: dict) -> Decision:
    if ctx.run.get("profile") != "agent_request":
        return Decision("allow", "The person destination is checked on send")
    if ctx.run.get("personal_read"):
        return Decision("allow", "Personal data checks handle draft approval")
    person = await _resolve_person(ctx.conn, args["person"])
    if not person or not person.get("slack_id") or not ctx.app.slack_client:
        return Decision("deny", "The person has no Slack destination")
    args["person"] = person["key"]
    return await comms_guard.evaluate(
        ctx,
        kind="dm",
        tool_name="slack_send_dm",
        target=f"person:{person['key']}",
        target_label=f"DM to {person['name']} ({person['key']})",
        args=args,
        text=args["text"],
        policy=ctx.app.workspace.config.slack.agent_dms,
    )


async def dm_handler(ctx: ToolContext, args: dict) -> dict:
    sent = await send_dm(ctx.conn, ctx.app, args)
    await comms_guard.record_sent(
        ctx, tool_name="slack_send_dm", target=f"person:{args['person']}", text=args["text"]
    )
    return sent


def _configured_public_names(app: "AppContext") -> list[str]:
    return [
        f"#{entry.name}"
        for entry in app.workspace.config.slack.channels
        if entry.mode != "off" and entry.visibility != "private"
    ]


def _unconfigured_reason(app: "AppContext") -> str:
    names = _configured_public_names(app)
    if names:
        return (
            "The channel is not configured. Configured channels: "
            + ", ".join(names)
            + ". Call slack_list_channels to see available team channels."
        )
    return "The channel is not configured. Call slack_list_channels to see available team channels."


async def _resolve_channel_id(ctx: ToolContext, channel: str, *, allow_api: bool) -> str | None:
    raw = channel.strip()
    name = raw.removeprefix("#")
    name_lower = name.lower()
    for entry in ctx.app.workspace.config.slack.channels:
        if raw == entry.id or name_lower == entry.name.lower():
            return entry.id
    if re.fullmatch(r"[CG][A-Z0-9_]+", raw):
        return raw
    if not allow_api:
        cached = await cached_public_member_channels(ctx.app, conn=ctx.conn)
        for ch_id, ch_name in cached.items():
            if name_lower == ch_name.lower() and configured(ctx.app, ch_id):
                return ch_id
        return None
    try:
        resolved = await resolve_channel(ctx.app, name_lower, conn=ctx.conn)
    except (ChannelLookupError, ValueError):
        return None
    return resolved.id


async def post_preflight(ctx: ToolContext, args: dict) -> Decision:
    # A draft after personal_read does not contact Slack. Approval rechecks the channel.
    if ctx.run.get("personal_read"):
        channel_id = await _resolve_channel_id(ctx, args["channel"], allow_api=False)
        if not channel_id:
            return Decision("deny", _unconfigured_reason(ctx.app))
        args["channel"] = channel_id
        return Decision("allow", "The approved send checks channel visibility")
    channel_id = await _resolve_channel_id(ctx, args["channel"], allow_api=bool(ctx.app.slack_client))
    if not channel_id or not configured(ctx.app, channel_id) or not ctx.app.slack_client:
        return Decision("deny", _unconfigured_reason(ctx.app))
    args["channel"] = channel_id
    if await channel_private(ctx.app, channel_id, conn=ctx.conn):
        return Decision("deny", "The channel is private")
    if ctx.run.get("profile") == "agent_request":
        entry = ctx.app.workspace.config.slack.channel(channel_id)
        policy = entry.agent_posts or ctx.app.workspace.config.slack.agent_posts
        label = f"#{entry.name}" if entry.listed else channel_id
        return await comms_guard.evaluate(
            ctx,
            kind="channel",
            tool_name="slack_post_channel",
            target=f"channel:{channel_id}",
            target_label=label,
            args=args,
            text=args["text"],
            policy=policy,
        )
    return Decision("allow", "The channel is visible to the team")


async def post_handler(ctx: ToolContext, args: dict) -> dict:
    sent = await post(ctx.app, args, conn=ctx.conn)
    await comms_guard.record_sent(
        ctx, tool_name="slack_post_channel", target=f"channel:{args['channel']}", text=args["text"]
    )
    return sent


async def list_channels_handler(ctx: ToolContext, args: dict) -> dict:
    entries: dict[str, dict] = {}
    for entry in ctx.app.workspace.config.slack.channels:
        if entry.mode == "off" or entry.visibility == "private":
            continue
        if ctx.app.slack_client and entry.team_members_only:
            try:
                if await channel_private(ctx.app, entry.id, conn=ctx.conn):
                    continue
            except ValueError:
                continue
        entries[entry.id] = {"id": entry.id, "name": entry.name, "configured": True}
    if ctx.app.slack_client and not ctx.run.get("personal_read"):
        try:
            public = await public_member_channels(ctx.app, conn=ctx.conn)
        except ValueError:
            public = {}
    else:
        public = await cached_public_member_channels(ctx.app, conn=ctx.conn)
    for ch_id, ch_name in sorted(public.items(), key=lambda item: item[1].lower()):
        if ch_id not in entries and configured(ctx.app, ch_id):
            entries[ch_id] = {"id": ch_id, "name": ch_name, "configured": False}
    channels = list(entries.values())
    if channels:
        lines = [
            f"#{c['name']} ({c['id']}, {'configured' if c['configured'] else 'public'})"
            for c in channels
        ]
        summary = "\n".join(lines)
    else:
        summary = "No team Slack channels are available."
    return {
        "channels": channels,
        "text": data_text("slack", summary),
        "visibility": "team",
    }


async def read_handler(ctx: ToolContext, args: dict) -> dict:
    if not ctx.app.slack_client:
        raise ValueError("Slack is not connected")
    private = True
    same_channel = False
    if configured(ctx.app, args["channel"]):
        private = await channel_private(ctx.app, args["channel"], conn=ctx.conn)
        if private:
            person = None
            if ctx.principal.startswith("person:") and ctx.run.get("profile") != "agent_request":
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
            "Send a Slack DM to one team member (`person`: a person key or name). For a question that needs an answer, use `asks_create` instead. Routine updates in agent requests send automatically after safety review; risky messages or posts after a personal read become a draft card. At most 3 per run.",
            SendDM,
            profiles=frozenset({"person_dm", "routine", "agent_request"}),
            preflight=dm_preflight,
        )
    )
    registry.add(
        Tool(
            "slack_list_channels",
            "slack",
            "read",
            lambda a: "ledger",
            False,
            list_channels_handler,
            "List configured and public team Slack channels where Illo can post (returns channel ids and #names). Call this before `slack_post_channel` when the channel is not specified.",
            Arguments,
            profiles=frozenset({"person_dm", "team_channel", "routine", "agent_request"}),
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
            "Post in a configured team channel (`channel`: its id or #name; `thread_ts` to answer in a thread). Call `slack_list_channels` first if the channel is not specified. Routine updates in agent requests post automatically after safety review; risky messages or posts after a personal or private read become a draft card.",
            PostChannel,
            profiles=frozenset({"person_dm", "agent_request"}),
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
            profiles=frozenset({"person_dm", "team_channel", "agent_request", "board_coordinator"}),
        )
    )
