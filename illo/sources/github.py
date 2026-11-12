"""Poll GitHub facts. Keep a durable cursor for each configured repository."""

from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from illo.channels.slack.limits import flush_owner_notices
from illo.connectors.github_client import GitHubClient
from illo.db import one
from illo.ledger import github as ledger
from illo.ledger import kv, runs, sensors
from illo.tools.registry import ToolContext
from illo.tools.work import refresh_statuses
from illo.vault import Vault

WRITE_TOOLS = frozenset({
    "github_create_issue", "github_assign_issue", "github_comment_issue", "work_start",
})


async def prepare_draft_card(ctx, conn, draft):
    """Save the issue title and link before the card offers Send."""
    content = draft["content"]
    args = content["arguments"]
    if "number" not in args or "github_issue" in content:
        return
    from illo.tools.github import repository

    issue = await ctx.github_client.get_issue(repository(ctx, args["repo"]), args["number"])
    if not issue or not issue.get("title") or not issue.get("html_url"):
        raise ValueError("The issue title and link are unavailable")
    content["github_issue"] = {
        "title": issue["title"], "link": issue["html_url"],
        "assignees": [person["login"] for person in issue.get("assignees", [])],
    }
    await conn.execute("UPDATE drafts SET content=%s WHERE id=%s", (Jsonb(content), draft["id"]))


async def poll(ctx) -> None:
    if not ctx.github_client:
        return
    try:
        configured = [*ctx.workspace.config.github.repositories,
                      *[r for a in ctx.workspace.agents if a.wake.type == "github_issue" for r in a.wake.repos]]
        for repo in dict.fromkeys(configured):
            key = f"github:cursor:{repo.lower()}"
            async with ctx.pool.connection() as conn:
                since = await kv.get(conn, key)
            # Use the start time so changes during a long poll enter the next poll.
            until = datetime.now(UTC).isoformat()
            items = await ctx.github_client.list_changed(repo, since=since)
            async with ctx.pool.connection() as conn:
                linked = await one(conn, """SELECT array_agg(tt.url) AS urls FROM task_tickets tt
                    JOIN tasks t ON t.id=tt.task_id WHERE t.state NOT IN ('done','cancelled')""")
            from illo.connectors.github_tasks import parse

            seen = {item["number"] for item in items}
            task_numbers, detailed = set(), set()
            for url in linked["urls"] or []:
                ref = parse(url)
                if not ref or ref[0].lower() != repo.lower():
                    continue
                task_numbers.add(ref[1])
                if ref[1] not in seen:
                    detail = await ctx.github_client.get_issue(repo, ref[1])
                    if detail:
                        items.append(detail)
                        detailed.add(ref[1])
                        seen.add(ref[1])
            for item in items:
                # Linked tasks need fresh facts, also on the first poll.
                if item["number"] not in detailed and (since is not None or item["number"] in task_numbers):
                    if "pull_request" in item:
                        detail = await ctx.github_client.get_pull(repo, item["number"])
                        item = {**item, **detail, "pull_request": item["pull_request"]}
                    else:
                        item = await ctx.github_client.get_issue(repo, item["number"])
                # One short transaction for each item. A restart keeps the lanes that are done,
                # and no database connection stays open across the GitHub calls.
                async with ctx.pool.connection() as conn:
                    # Task actions take the graph lock before the repository lock.
                    await conn.execute("SELECT pg_advisory_xact_lock(12000001)")
                    await ledger.lock_repository(conn, repo.lower())
                    await ledger.sync_item(conn, repo, item, ctx.workspace.config.github.label_map)
                    from illo.tasks.connectors import changed

                    await changed(ctx, conn, "change" if "pull_request" in item else "ticket", repo=repo, item=item)
            async with ctx.pool.connection() as conn:
                await kv.set(conn, key, until)
        await refresh_statuses(ctx)
    except Exception:
        async with ctx.pool.connection() as conn:
            await sensors.error(conn, "github", "The GitHub poll failed",
                                expected_every_s=ctx.workspace.config.github.poll_seconds)
        raise
    else:
        async with ctx.pool.connection() as conn:
            await sensors.ok(conn, "github", expected_every_s=ctx.workspace.config.github.poll_seconds)


async def perform_draft(ctx, draft: dict) -> dict:
    """Only the asks approval path calls this hook. Recheck the saved action."""
    from illo.tools.github import repository, validate_assignee

    if draft["state"] != "approved":
        raise ValueError("The draft is not approved")
    content = draft["content"]
    tool = ctx.registry.tools[content["tool"]]
    if tool.name not in WRITE_TOOLS or draft["kind"] != tool.name:
        raise ValueError("The draft does not contain a GitHub write")
    args = tool.arguments.model_validate(content["arguments"]).model_dump()
    if tool.destination(args) != draft["destination"]:
        raise ValueError("The draft destination does not match the stored action")
    repository(ctx, args["repo"])
    async with ctx.pool.connection() as conn:
        run = await runs.get(conn, draft["run_id"])
        if not run or run["person_id"] != draft["approver_person_id"]:
            raise ValueError("The draft approver is not the requester")
        person = await conn.execute(
            "SELECT id FROM people WHERE id=%s AND active", (run["person_id"],)
        )
        if not await person.fetchone():
            raise ValueError("The requester is not active")
        tc = ToolContext(conn, run, ctx)
        if args.get("assignee"):
            if await validate_assignee(tc, args["assignee"]) != args["assignee"]:
                raise ValueError("The assignee no longer matches the stored action")
        return await tool.handler(tc, args)


async def setup(ctx) -> None:
    config = ctx.config
    if ctx.github_client is None:
        token = config.github_token.get_secret_value() if config.github_token else None
        key = None
        if not token and config.github_app_id and config.github_app_installation_id:
            if config.github_app_private_key_file:
                key = config.github_app_private_key_file.read_text()
            elif config.vault_key:
                async with ctx.pool.connection() as conn:
                    key = await Vault(config.vault_key.get_secret_value()).get(
                        conn, config.github_app_private_key_secret
                    )
            if not key:
                raise ValueError("The GitHub App private key is absent")
        if token or key:
            reports = []

            async def report(ok, detail):
                # A tool can hold the only pool connection. Flush reports outside it.
                reports.append((ok, detail))

            async def flush_reports():
                async with ctx.pool.connection() as conn:
                    pending = len(reports)
                    for ok, detail in reports[:pending]:
                        await (sensors.ok if ok else sensors.error)(conn, "github", detail)
                del reports[:pending]

            ctx.every(5, flush_reports)
            ctx.github_client = GitHubClient(
                token=token,
                app_id=config.github_app_id,
                private_key=key,
                installation_id=config.github_app_installation_id,
                report=report,
            )
    for name in (
        "github_create_issue",
        "github_assign_issue",
        "github_comment_issue",
        "work_start",
    ):
        ctx.draft_performers[name] = perform_draft

    async def poll_github():
        await poll(ctx)

    async def refresh_work():
        await refresh_statuses(ctx)

    async def owner_notices():
        await flush_owner_notices(ctx)

    ctx.every(60, owner_notices)
    ctx.every(15, refresh_work)
    ctx.every(ctx.workspace.config.github.poll_seconds, poll_github)
