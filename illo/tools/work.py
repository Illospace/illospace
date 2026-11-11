"""Start work through one GitHub issue and one Slack status message."""

from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

from illo.connectors.github_client import GitHubError
from illo.ledger import github as ledger
from illo.ledger import kv, lanes, people, sensors
from illo.tools.checks import Decision
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
    "open": "Open",
    "working": "Working",
    "waiting_person": "Needs a person",
    "blocked": "Blocked",
    "review": "Needs review",
    "done": "Done",
    "failed": "Failed",
    "unknown": "Unknown",
}
# GitHub refuses an assignment where the Illo sign-in has no write access. A public
# repository still accepts the ticket, so the ticket exists and has no owner.
NOT_ASSIGNED = "GitHub refused the assignment. The ticket has no owner."
# Another failure, for example a server error, does not show if GitHub did the assignment.
NOT_CONFIRMED = "The ticket was created. Illo could not confirm the assignment."


class StartArgs(CreateArgs):
    assignee: str | None = Field(default=None, min_length=1, max_length=100)


async def start_preflight(ctx: ToolContext, args: dict) -> Decision:
    verdict = await write_preflight(ctx, args)
    if verdict.outcome != "allow" or not args.get("assignee"):
        return verdict
    if ctx.run.get("person_id"):
        return Decision("draft", "An assignment needs approval")
    # The assignment is refused. Filing an unassigned ticket remains allowed.
    if ctx.run["personal_read"]:
        return Decision("deny", "Personal data cannot enter an unassigned ticket")
    result = await start(ctx, {**args, "assignee": None})
    link = (result.get("issue") or {}).get("html_url", "")
    return Decision("deny", f"The assignment needs approval. The ticket stays unassigned. {link}")


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
    note = None
    if login:
        try:
            await ctx.app.github_client.assign_issue(repo, issue["number"], [login])
        except GitHubError as exc:
            # The ticket exists. Record it, and tell the person what Illo knows about the owner.
            refused = exc.status in {403, 404} and not exc.rate_limited
            note = NOT_ASSIGNED if refused else NOT_CONFIRMED
            result = {**result, "assigned": False if refused else None, "note": note}
        else:
            issue["assignees"] = [{"login": login}]
    lane = await ledger.sync_item(ctx.conn, repo, issue, ctx.app.workspace.config.github.label_map)
    lane["last_event"] = f"State: {lane['state']}"
    if note:
        # A lane event keeps the note in the status message after the next refresh.
        await lanes.add_event(ctx.conn, lane["id"], "note", note)
        lane["last_event"] = note
    if ctx.app.workspace.config.github.work_channel or ctx.run.get("person_id"):
        await kv.set(
            ctx.conn,
            f"github:pending-status:{lane['id']}",
            {
                "channel": ctx.app.workspace.config.github.work_channel,
                "person_id": ctx.run.get("person_id"),
            },
        )
        await send_status(ctx.app, ctx.conn, lane)
    return {**result, "lane_id": lane["id"]}


async def send_status(app, conn, lane: dict) -> None:
    if not app.slack_client:
        return
    key = f"github:status:{lane['id']}"
    pending_key = f"github:pending-status:{lane['id']}"
    pending = await kv.get(conn, pending_key)
    text = status_text(lane)
    if not pending and await kv.get(conn, key) == text:
        return
    try:
        if lane.get("slack_ts"):
            await app.slack_client.chat_update(
                channel=lane["slack_channel"], ts=lane["slack_ts"], text=text, mrkdwn=False
            )
        elif pending:
            channel = pending.get("channel")
            if not channel:
                requester = next(
                    (
                        p
                        for p in await people.list_people(conn)
                        if p["id"] == pending.get("person_id")
                    ),
                    None,
                )
                if not requester or not requester["slack_id"]:
                    return
                opened = await app.slack_client.conversations_open(users=requester["slack_id"])
                channel = opened["channel"]["id"]
            sent = await app.slack_client.chat_postMessage(
                channel=channel,
                text=text,
                mrkdwn=False,
                unfurl_links=False,
                client_msg_id=str(uuid5(NAMESPACE_URL, f"illo:github:{lane['external_id']}")),
            )
            await ledger.status_sent(conn, lane["id"], channel, sent["ts"])
        await kv.set(conn, key, text)
        await kv.delete(conn, pending_key)
        await sensors.ok(conn, "github:status")
    except Exception:
        # Keep the lane and the pending status after a Slack failure.
        await sensors.error(conn, "github:status", "The work status could not be sent")


async def refresh_statuses(app) -> None:
    if not app.slack_client:
        return
    async with app.pool.connection() as conn:
        for lane in await ledger.statuses(conn):
            await send_status(app, conn, lane)


def register(registry) -> None:
    registry.add(
        Tool(
            "work_start",
            "work",
            "write_internal",
            lambda a: f"github:{a['repo']}",
            False,
            start,
            description="Start work through a GitHub issue: file it in a configured repository, track it as a lane, and post a status message. With `assignee` (a person key or GitHub login) in a person's run, the result is a draft card, and the issue is filed and assigned only when the person taps Send. Without a person to approve, the issue is filed unassigned and the result is `deny` with its link: say that a person must assign it. `signature` works as in `github_create_issue`; once per run.",
            arguments=StartArgs,
            profiles=PROFILES - {"tainted"},
            preflight=start_preflight,
        )
    )
