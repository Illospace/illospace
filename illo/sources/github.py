"""Poll GitHub facts. Keep a durable cursor for each configured repository."""

from datetime import UTC, datetime

from illo.connectors.github_client import GitHubClient
from illo.ledger import github as ledger
from illo.ledger import kv, runs, sensors
from illo.tools.registry import ToolContext
from illo.tools.work import refresh_statuses
from illo.vault import Vault


async def poll(ctx) -> None:
    if not ctx.github_client:
        return
    try:
        for repo in ctx.workspace.config.github.repositories:
            key = f"github:cursor:{repo.lower()}"
            async with ctx.pool.connection() as conn:
                since = await kv.get(conn, key)
            # Use the start time so changes during a long poll enter the next poll.
            until = datetime.now(UTC).isoformat()
            items = await ctx.github_client.list_changed(repo, since=since)
            for item in items:
                # The first load of a repository uses the list data only: one call for each
                # 100 items. A later poll reads the details of the few items that changed.
                if since is not None:
                    if "pull_request" in item:
                        detail = await ctx.github_client.get_pull(repo, item["number"])
                        item = {**item, **detail, "pull_request": item["pull_request"]}
                    else:
                        item = await ctx.github_client.get_issue(repo, item["number"])
                # One short transaction for each item. A restart keeps the lanes that are done,
                # and no database connection stays open across the GitHub calls.
                async with ctx.pool.connection() as conn:
                    await ledger.lock_repository(conn, repo.lower())
                    await ledger.sync_item(conn, repo, item, ctx.workspace.config.github.label_map)
            async with ctx.pool.connection() as conn:
                await kv.set(conn, key, until)
        await refresh_statuses(ctx)
    except Exception:
        async with ctx.pool.connection() as conn:
            await sensors.error(conn, "github", "The GitHub poll failed")
        raise
    else:
        async with ctx.pool.connection() as conn:
            await sensors.ok(conn, "github")


async def perform_draft(ctx, draft: dict) -> dict:
    """Only the asks approval path calls this hook. Recheck the saved action."""
    from illo.tools.github import repository, validate_assignee

    if draft["state"] != "approved":
        raise ValueError("The draft is not approved")
    content = draft["content"]
    tool = ctx.registry.tools[content["tool"]]
    if tool.name not in {
        "github_create_issue",
        "github_assign_issue",
        "github_comment_issue",
        "work_start",
    }:
        raise ValueError("The draft does not contain a GitHub write")
    args = tool.arguments.model_validate(content["arguments"]).model_dump()
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
            await validate_assignee(tc, args["assignee"])
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

    ctx.every(15, refresh_work)
    ctx.every(ctx.workspace.config.github.poll_seconds, poll_github)
