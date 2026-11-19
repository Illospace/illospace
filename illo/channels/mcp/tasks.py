"""Direct task tools for a person's agents. No model run starts here."""

from pydantic import Field

from illo.db import one
from illo.ledger import mcp
from illo.ledger import tasks as ledger
from illo.tasks import progress
from illo.tasks.access import token_viewer
from illo.tasks.views import compact, full
from illo.tools import tasks as shared
from illo.tools.registry import Arguments

NAMES = frozenset({"task_mine", "task_get", "task_write", "task_post", "task_create", "task_lists", "task_page"})


class Mine(Arguments):
    open: bool = True
    limit: int = Field(default=50, ge=1, le=100)


class Question(Arguments):
    id: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=30000)
    to: str | None = Field(default=None, pattern=r"^person:[a-z][a-z0-9_-]*$")


async def handle(ctx, name, args):
    workspace = ctx.app.workspace
    viewer = await token_viewer(ctx.conn, workspace, ctx.token)
    await ledger._identity(ctx.conn, viewer)
    if name == "task_lists":
        entries = ledger.visible_lists(workspace, viewer)
        if args.get("key"):
            entries = [ledger.list_config(workspace, args["key"], viewer)]
        return {"agents": [agent.model_dump() for agent in workspace.agents],
                "lists": [entry.model_dump(exclude={"path"}) for entry in entries]}
    if name == "task_mine":
        holders = [viewer.actor] if ctx.token.get("agent_key") else [
            "person:" + viewer.person, *["agent:" + a.key for a in workspace.agents if a.owner == viewer.person]]
        rows = []
        for holder in holders:
            rows.extend(await ledger.search(ctx.conn, viewer=viewer, holder=holder, **args))
        rows.sort(key=lambda row: (row["updated_at"], row["id"]), reverse=True)
        return {"tasks": [{**compact(row), "progress": await progress.read(ctx.conn, row, viewer),
                           "new_for_you": row["last_actor"] != viewer.actor}
                          for row in rows[:args["limit"]]]}
    common = {"workspace": workspace, "viewer": viewer}
    if name == "task_page":
        from illo.tasks.pages import outside

        row = await ledger.get(ctx.conn, args["task"], viewer=viewer)
        if not row:
            raise ledger.TaskError("The task was not found.")
        ledger._working_holder(row, viewer)
        files = await outside.prepare(ctx.app, args["blocks"])
    if name in {"task_write", "task_post", "task_create", "task_page"}:
        # The task lock makes the hourly cap exact across simultaneous calls.
        await mcp.lock_token(ctx.conn, ctx.token["id"], "tasks")
        count = await one(ctx.conn, """SELECT count(*) AS n FROM audit WHERE token_id=%s
            AND tool = ANY(%s) AND decision='allow' AND at>now()-interval '1 hour'""",
            (ctx.token["id"], ["task_write", "task_post", "task_create", "task_page"]))
        if count["n"] >= 60:
            raise ledger.TaskError("The token has reached its limit of 60 task changes per hour.")
    if name == "task_page":
        from illo.tasks.pages import _write

        return await _write(ctx.conn, ctx.app, viewer, args, files, holder_only=True)
    if name == "task_get":
        row = await ledger.get(ctx.conn, args["id"], viewer=viewer)
        if not row:
            raise ledger.TaskError("The task was not found.")
    elif name == "task_create":
        row = await shared.create_for(ctx.conn, workspace, viewer, args)
    elif name == "task_write":
        row = await ledger.write_result(
            ctx.conn, args["id"], **common, **{key: value for key, value in args.items() if key != "id"}, ctx=ctx)
    elif name == "task_post":
        row = await ledger.post(ctx.conn, args["id"], **common, text=args["text"], question=True, to=args["to"])
    return await full(ctx.conn, workspace, row, viewer, limit=args.get("events", 20), ctx=ctx)


def tools(factory):
    from illo.tasks.pages.schema import Write as PageWrite

    entries = [
        ("task_page", PageWrite, "write_internal", "Show a person the work they must review as its reader will see it, then the checks you made, each with its source. Do not repeat the brief. Replace the page of a task you hold and return its URL."),
        ("task_mine", Mine, "read", "Read your tasks with state, waits_for, and new_for_you. Read task_get before you work. An unnamed token reads the tasks of its person and their agents."),
        ("task_get", shared.Get, "read", "Read the brief, result version, links, pieces, checks, messages, silent_hours, and last wake result before you act."),
        ("task_write", shared.Write, "write_internal", "Write your result below the brief. Give either result with the version you read, or append. A message must say what changed and what you need. Set returned true on the last write. Never say done."),
        ("task_post", Question, "write_internal", "Ask one blocking question on your task. Optionally name a person in to. If the task has another holder, change nothing and end the run."),
        ("task_create", shared.Create, "write_internal", "Capture work the person stated. Hold it yourself or use the list default. You may give an allowed holder a piece under your own task. You cannot hand over an existing task."),
        ("task_lists", shared.Lists, "read", "Read the visible lists, their instructions, holders, and Done rules. Read these instructions before you split work into pieces."),
    ]
    result = []
    for name, arguments, risk, description in entries:
        async def call(ctx, args, name=name):
            return await handle(ctx, name, args)
        result.append(factory(name, arguments, call, risk, description))
    return result
