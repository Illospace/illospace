"""Current task pages share the task's identity and transaction boundary."""

from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from illo.db import one
from illo.ledger import tasks
from illo.tasks.pages import images
from illo.tasks.pages.schema import Write


def _content(blocks):
    """Give the blocks without the check times. A write fills an absent time with the present time."""
    return [{**block, "items": [{k: v for k, v in item.items() if k != "checked_at"} for item in block["items"]]}
            if block.get("items") else block for block in blocks]


def enabled(ctx):
    return ctx.workspace.config.pages.enabled


def base_url(ctx):
    return (ctx.workspace.config.pages.url or ctx.workspace.config.connect.public_url
            or ctx.config.public_url).rstrip("/")


def url(ctx, task_id):
    return base_url(ctx) + f"/t/{task_id}"


def workspace_url(workspace, task_id):
    from illo.config import Config

    base = workspace.config.pages.url or workspace.config.connect.public_url or Config.from_env().public_url
    return base.rstrip("/") + f"/t/{task_id}"


async def read(conn, ctx, viewer, task_id):
    if not enabled(ctx):
        raise tasks.TaskError("Task pages are off.")
    row = await tasks.get(conn, task_id, viewer=viewer)
    if not row:
        raise tasks.TaskError("The task was not found.")
    page = await one(conn, "SELECT * FROM task_pages WHERE task_id=%s", (task_id,))
    return {"task": task_id, "blocks": page["blocks"] if page else [],
            "result_version": page["result_version"] if page else None,
            "current_result_version": row["result_version"], "url": url(ctx, task_id),
            "author": page["author"] if page else None,
            "written_at": page["written_at"] if page else None,
            "visibility": row["visibility"]}


@tasks._mutation
async def write(conn, ctx, viewer, args, *, holder_only=False):
    if not enabled(ctx):
        raise tasks.TaskError("Task pages are off.")
    args = Write.model_validate(args).model_dump()
    row = await tasks._require(conn, args["task"], viewer)
    if holder_only:
        tasks._working_holder(row, viewer)
    blocks = args["blocks"]
    for block in blocks:
        if block.get("image"):
            try:
                block["image"], _, _ = await images.read(ctx, block["image"])
            except ValueError as exc:
                raise tasks.TaskError(str(exc)) from None
        for item in block.get("items", []):
            if item["state"] != "open" and item["checked_at"] is None:
                item["checked_at"] = datetime.now(UTC).isoformat()
    before = await one(conn, "SELECT blocks FROM task_pages WHERE task_id=%s", (row["id"],))
    await conn.execute("""INSERT INTO task_pages(task_id,blocks,author,result_version)
        VALUES (%s,%s,%s,%s) ON CONFLICT(task_id) DO UPDATE SET blocks=EXCLUDED.blocks,
        author=EXCLUDED.author,result_version=EXCLUDED.result_version,written_at=clock_timestamp()""",
        (row["id"], Jsonb(blocks), viewer.actor, row["result_version"]))
    if (row["state"] == "review" and (row["waits_for"] or "").startswith("person:")
            and viewer.actor.startswith("agent:") and (not before or _content(before["blocks"]) != _content(blocks))):
        # An agent changed the page after the work came back. No other message tells the reviewer.
        # A person who asks Illo for a page gets the address in the answer, and a wait check sends its own message.
        await tasks._event(conn, row, "code", "message", "The page of this task is ready for your review.",
                           {"notify": True})
    return await read(conn, ctx, viewer, row["id"])
