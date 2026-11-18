"""Tools write typed task content, never HTML."""

from illo.db import one
from illo.ledger.tasks import TaskError
from illo.tasks import pages
from illo.tasks.access import run_viewer
from illo.tasks.pages.schema import Read, Write
from illo.tools.checks import Decision
from illo.tools.registry import Refusal, Tool
from illo.tools.tasks import READ

WRITE = frozenset({"person_dm", "routine", "agent_request"})
DESCRIPTION = ("Show a person the work they must review as its reader will see it, then the checks "
               "you made, each with its source. Do not repeat the brief. Replace all blocks and return the page URL.")


async def available(ctx):
    return pages.enabled(ctx.app)


async def preflight(ctx, args):
    return Decision("allow", "Task pages are on.") if await available(ctx) else Decision("deny", "Task pages are off.")


async def handle(ctx, args, *, writing):
    try:
        if writing and ctx.run.get("deep_kind") == "check":
            deep = await one(ctx.conn, "SELECT context FROM deep_runs WHERE id=%s AND run_id=%s AND kind='check'",
                             (ctx.run.get("deep_run_id"), ctx.run["id"]))
            if not deep or deep["context"].get("wait", {}).get("task_id") != args["task"]:
                raise TaskError("A wait check can write only its task page.")
        viewer = await run_viewer(ctx)
        if writing:
            result = await pages.write(ctx.conn, ctx.app, viewer, args)
            return {key: result[key] for key in ("task", "url", "result_version", "written_at")}
        return await pages.read(ctx.conn, ctx.app, viewer, args["task"])
    except TaskError as exc:
        raise Refusal(str(exc)) from None


def register(registry):
    for name, schema, profiles, risk, description in [
        ("page_write", Write, WRITE, "write_internal", DESCRIPTION),
        ("page_read", Read, READ, "read", "Read a task's page blocks, their result version, the current result version, and the page URL. Read the page before you replace it. Give the page URL to a person who must review the task or decide on it."),
    ]:
        async def call(ctx, args, writing=name == "page_write"):
            return await handle(ctx, args, writing=writing)
        registry.add(Tool(name, "tasks", risk, lambda _: "ledger", False, call,
            description=description, arguments=schema, profiles=profiles,
            available=available, preflight=preflight))
