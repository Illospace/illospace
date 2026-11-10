"""Start work through one GitHub issue and one Slack status message."""

from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

from illo.ledger import github as ledger
from illo.ledger import kv, people
from illo.tools.github import (
    PROFILES,
    CreateArgs,
    create_issue,
    repository,
    validate_assignee,
    write_preflight,
)
from illo.tools.registry import Tool, ToolContext

LABELS = {
    "working": "Working",
    "waiting_person": "Needs a person",
    "blocked": "Blocked",
    "review": "Needs review",
    "done": "Done",
    "failed": "Failed",
    "unknown": "Unknown",
}


class StartArgs(CreateArgs):
    assignee: str | None = Field(default=None, min_length=1, max_length=100)


def status_text(lane: dict) -> str:
    links = lane.get("links") or []
    link = next((v["url"] for v in links if v.get("kind") == "github"), "")
    text = f"{LABELS[lane['state']]} · {lane['title']} · {link}"
    if lane.get("last_event"):
        text += "\n" + lane["last_event"]
    return text


async def start(ctx: ToolContext, args: dict) -> dict:
    repo = repository(ctx.app, args["repo"])
    login = await validate_assignee(ctx, args["assignee"]) if args.get("assignee") else None
    result = await create_issue(ctx, args)
    if not result["created"]:
        return result
    issue = result["issue"]
    if login:
        await ctx.app.github_client.assign_issue(repo, issue["number"], [login])
        issue["assignees"] = [{"login": login}]
    lane = await ledger.sync_item(ctx.conn, repo, issue, ctx.app.workspace.config.github.label_map)
    lane["last_event"] = f"State: {lane['state']}"
    if ctx.app.slack_client:
        channel = ctx.app.workspace.config.github.work_channel
        if not channel and ctx.run.get("person_id"):
            requester = next(
                (p for p in await people.list_people(ctx.conn) if p["id"] == ctx.run["person_id"]),
                None,
            )
            if requester and requester["slack_id"]:
                opened = await ctx.app.slack_client.conversations_open(users=requester["slack_id"])
                channel = opened["channel"]["id"]
        if channel:
            text = status_text(lane)
            sent = await ctx.app.slack_client.chat_postMessage(
                channel=channel,
                text=text,
                mrkdwn=False,
                unfurl_links=False,
                client_msg_id=str(uuid5(NAMESPACE_URL, f"illo:github:{lane['external_id']}")),
            )
            await ledger.status_sent(ctx.conn, lane["id"], channel, sent["ts"])
            await kv.set(ctx.conn, f"github:status:{lane['id']}", text)
    return {**result, "lane_id": lane["id"]}


async def refresh_statuses(app) -> None:
    if not app.slack_client:
        return
    async with app.pool.connection() as conn:
        for lane in await ledger.statuses(conn):
            text = status_text(lane)
            key = f"github:status:{lane['id']}"
            if await kv.get(conn, key) == text:
                continue
            await app.slack_client.chat_update(
                channel=lane["slack_channel"], ts=lane["slack_ts"], text=text, mrkdwn=False
            )
            await kv.set(conn, key, text)


def register(registry) -> None:
    registry.add(
        Tool(
            "work_start",
            "work",
            "write_internal",
            lambda a: f"github:{a['repo']}",
            False,
            start,
            arguments=StartArgs,
            profiles=PROFILES,
            preflight=write_preflight,
        )
    )
