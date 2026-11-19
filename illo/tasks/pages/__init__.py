"""Current task pages share the task's identity and transaction boundary."""

from psycopg.types.json import Jsonb

from illo.db import one
from illo.ledger import tasks
from illo.tasks.pages import outside
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


async def stale(conn, row, page):
    """Say if the page can show older work: the work came back, or the result changed, after the page was written.

    A return through a ticket adds no result version, so the page keeps the id of the last event that its
    writer could see. Event times are not safe for this: an event gets the start time of its transaction.
    """
    if not page:
        return False
    if page["result_version"] < row["result_version"]:
        return True
    if page.get("last_event_id") is None:
        # A page from before this column. Its write time is the best mark.
        newer = "at>%s", page["written_at"]
    else:
        newer = "id>%s", page["last_event_id"]
    return bool(await one(conn, "SELECT 1 FROM task_events WHERE task_id=%s AND kind='returned' AND "
                          + newer[0] + " LIMIT 1", (row["id"], newer[1])))


async def read(conn, ctx, viewer, task_id):
    if not enabled(ctx):
        raise tasks.TaskError("Task pages are off.")
    row = await tasks.get(conn, task_id, viewer=viewer)
    if not row:
        raise tasks.TaskError("The task was not found.")
    page = await one(conn, "SELECT * FROM task_pages WHERE task_id=%s", (task_id,))
    return {"task": task_id, "blocks": [{k: v for k, v in block.items() if k != "image_link"}
                       for block in page["blocks"]] if page else [],
            "result_version": page["result_version"] if page else None,
            "current_result_version": row["result_version"], "stale": await stale(conn, row, page),
            "url": url(ctx, task_id),
            "author": page["author"] if page else None,
            "written_at": page["written_at"] if page else None,
            "visibility": row["visibility"]}


async def write(conn, ctx, viewer, args, *, holder_only=False):
    """Write a page for a run of Illo's own model. This path makes no request to an outside address.

    The run holds its lock here, and its model read text that Illo does not trust. A picture address
    from that model shows as a link.
    """
    if not enabled(ctx):
        raise tasks.TaskError("Task pages are off.")
    args = Write.model_validate(args).model_dump()
    try:
        files = await outside.prepare(ctx, args["blocks"], link_only=True)
    except ValueError as exc:
        raise tasks.TaskError(str(exc)) from None
    return await _write(conn, ctx, viewer, args, files, holder_only=holder_only)


@tasks._mutation
async def _write(conn, ctx, viewer, args, files, *, holder_only=False):
    row = await tasks._require(conn, args["task"], viewer)
    if holder_only:
        tasks._working_holder(row, viewer)
    blocks = args["blocks"]
    await outside.replace(conn, files, task_id=row["id"])
    before = await one(conn, "SELECT blocks FROM task_pages WHERE task_id=%s", (row["id"],))
    # The task lock is held, so no event of this task can come between this read and the write.
    last = await one(conn, "SELECT max(id) AS id FROM task_events WHERE task_id=%s", (row["id"],))
    await conn.execute("""INSERT INTO task_pages(task_id,blocks,author,result_version,last_event_id)
        VALUES (%s,%s,%s,%s,%s) ON CONFLICT(task_id) DO UPDATE SET blocks=EXCLUDED.blocks,
        author=EXCLUDED.author,result_version=EXCLUDED.result_version,
        last_event_id=EXCLUDED.last_event_id,written_at=clock_timestamp()""",
        (row["id"], Jsonb(blocks), viewer.actor, row["result_version"], last["id"]))
    if (row["state"] == "review" and (row["waits_for"] or "").startswith("person:")
            and viewer.actor.startswith("agent:") and (not before or _content(before["blocks"]) != _content(blocks))):
        # An agent changed the page after the work came back. No other message tells the reviewer.
        # A person who asks Illo for a page gets the address in the answer, and a wait check sends its own message.
        await tasks._event(conn, row, "code", "message", "The page of this task is ready for your review.",
                           {"notify": True})
    result = await read(conn, ctx, viewer, row["id"])
    return {**result, **({"notes": outside.notes(blocks)} if outside.notes(blocks) else {})}
