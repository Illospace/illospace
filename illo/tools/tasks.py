"""Tools for the Operator's task list. A task change needs no approval card."""

import builtins
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from illo.ledger import tasks as ledger
from illo.tasks.access import run_viewer
from illo.tasks.views import compact, full
from illo.tools.registry import Arguments, Refusal, Tool

READ = frozenset({"person_dm", "team_channel", "routine", "agent_request"})
WRITE = frozenset({"person_dm", "team_channel"})
CAPTURE = WRITE | {"agent_request"}


class Link(Arguments):
    url: str = Field(min_length=1, max_length=2000, pattern=r"^https?://[^\s<>]+$")
    kind: str = Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_-]*$")
    title: str = Field(default="", max_length=300)


class Lists(Arguments):
    key: str | None = None


class Piece(Arguments):
    title: str = Field(min_length=1, max_length=300)
    brief: str = Field(default="", max_length=30000)
    holder: str | None = None
    due: str | None = None
    links: builtins.list[Link] = Field(default_factory=builtins.list, max_length=30)


class Create(Piece):
    list: str = "tasks"
    parent: int | None = Field(default=None, ge=1)
    pieces: builtins.list[Piece] = Field(default_factory=builtins.list, max_length=20)


class ListArgs(Arguments):
    mine: bool = False
    list: str | None = None
    holder: str | None = None
    state: Literal["todo", "working", "review", "done", "cancelled"] | None = None
    waits_for: str | None = None
    parent: int | None = Field(default=None, ge=1)
    query: str = ""
    open: bool = True
    limit: int = Field(default=30, ge=1, le=100)


class Get(Arguments):
    id: int = Field(ge=1)
    events: int = Field(default=20, ge=1, le=100)


class Update(Arguments):
    id: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    brief: str | None = Field(default=None, max_length=30000)
    due: str | None = None
    list: str | None = None
    links: builtins.list[Link] = Field(default_factory=builtins.list, max_length=30)


class Hand(Arguments):
    id: int = Field(ge=1)
    holder: str | None
    links: builtins.list[Link] = Field(default_factory=builtins.list, max_length=30)


class Post(Arguments):
    id: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=30000)
    question: bool = False
    to: str | None = Field(default=None, pattern=r"^person:[a-z][a-z0-9_-]*$")


class Done(Arguments):
    id: int = Field(ge=1)
    force: bool = False
    reason: str = ""


class Reason(Arguments):
    id: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=3000)


class Write(Arguments):
    id: int = Field(ge=1)
    result: str | None = Field(default=None, max_length=60000)
    append: str | None = Field(default=None, max_length=30000)
    result_version: int | None = Field(default=None, ge=0)
    message: str = Field(min_length=1, max_length=30000, pattern=r"\S")
    links: builtins.list[Link] = Field(default_factory=builtins.list, max_length=30)
    returned: bool = False

    @model_validator(mode="after")
    def require_text(self):
        if (self.result is None) == (self.append is None):
            raise ValueError("Give either a result or text to append")
        if self.result is not None and self.result_version is None:
            raise ValueError("A full result needs the version you read")
        return self


def due(value):
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ledger.TaskError("Use an ISO date and time with a time zone.") from None
    if result.tzinfo is None:
        raise ledger.TaskError("Use an ISO date and time with a time zone.")
    return result


async def lists(ctx, args):
    viewer = await run_viewer(ctx)
    await ledger._identity(ctx.conn, viewer)
    entries = ledger.visible_lists(ctx.app.workspace, viewer)
    if args.get("key"):
        entries = [ledger.list_config(ctx.app.workspace, args["key"], viewer)]
    return {"agents": [agent.model_dump() for agent in ctx.app.workspace.agents],
            "lists": [entry.model_dump(exclude={"path"} | (set() if args.get("key") else {"body"})) for entry in entries]}


async def create_for(conn, workspace, viewer, args):
    async with conn.transaction():
        row = await ledger.create(conn, workspace=workspace, viewer=viewer, list_key=args["list"],
            title=args["title"], brief=args.get("brief", ""), holder=args.get("holder"),
            due_at=due(args.get("due")), parent_id=args.get("parent"), links=args.get("links"))
        for piece in args.get("pieces", []):
            await ledger.create(conn, workspace=workspace, viewer=viewer, list_key=args["list"],
                title=piece["title"], brief=piece.get("brief", ""), holder=piece.get("holder"),
                due_at=due(piece.get("due")), parent_id=row["id"], links=piece.get("links"))
        return row


async def handle(ctx, name, args):
    try:
        if name == "lists":
            return await lists(ctx, args)
        viewer = await run_viewer(ctx)
        common = {"workspace": ctx.app.workspace, "viewer": viewer}
        if name == "task_create":
            row = await create_for(ctx.conn, ctx.app.workspace, viewer, args)
        elif name == "task_list":
            values = {("list_key" if k == "list" else "parent_id" if k == "parent" else k): v for k, v in args.items()}
            if values.pop("mine", False):
                if not viewer.person:
                    return {"tasks": []}
                values["mine_refs"] = ["person:" + viewer.person, *[
                    "agent:" + a.key for a in ctx.app.workspace.agents if a.owner == viewer.person]]
            return {"tasks": [compact(r) for r in await ledger.search(ctx.conn, viewer=viewer, **values)]}
        elif name == "task_get":
            row = await ledger.get(ctx.conn, args["id"], viewer=viewer)
            if not row:
                raise ledger.TaskError("The task was not found.")
        elif name == "task_update":
            changes = {("list_key" if k == "list" else "due_at" if k == "due" else k):
                       (due(v) if k == "due" else v) for k, v in args.items()
                       if k not in {"id", "links"} and (v is not None or k == "due")}
            row = await ledger.update(ctx.conn, args["id"], **common, **changes)
            if args.get("links"):
                row = await ledger.add_link(ctx.conn, args["id"], **common, links=args["links"], ctx=ctx)
        else:
            fn = {"task_hand": ledger.hand, "task_post": ledger.post, "task_done": ledger.done,
                  "task_reopen": ledger.reopen, "task_wake": ledger.wake, "task_cancel": ledger.cancel}[name]
            values = {k: v for k, v in args.items() if k not in {"id", "events"}}
            row = await fn(ctx.conn, args["id"], **common, **values, **({"ctx": ctx} if name == "task_done" else {}))
        return await full(ctx.conn, ctx.app.workspace, row, viewer, limit=args.get("events", 20), ctx=ctx)
    except ledger.TaskError as exc:
        raise Refusal(str(exc)) from None


def register(registry):
    entries = [
        ("lists", Lists, READ, "read", "Read the visible lists and their Done rules. Give a key to read the list instructions before you split or hand over work."),
        ("task_create", Create, CAPTURE, "write_internal", "Create a task from the person's words. Give a list, title, and brief. A holder is `person:<key>` or `agent:<key>`. A pieces array creates the parent and its pieces in one call."),
        ("task_list", ListArgs, READ, "read", "Read filtered tasks with the holder, state, waits_for, silent_hours, and open piece count. Use mine=true for tasks that wait for you, tasks you hold, and tasks your agents hold."),
        ("task_get", Get, READ, "read", "Read the task brief, result, links, pieces, checks, events, silent_hours, and last wake result before you answer."),
        ("task_update", Update, WRITE, "write_internal", "Change a task's title, brief, due time, or list. Add proof links with links. Private tasks keep their list and visibility."),
        ("task_hand", Hand, WRITE, "write_internal", "Hand a task to a person or named agent allowed by its list. A null holder leaves it unassigned. The state becomes todo. Give required links in the same call."),
        ("task_post", Post, CAPTURE, "write_internal", "Add a task message. A holder can ask a blocking question with question true and an optional person in to. The answer returns the next move to the holder."),
        ("task_done", Done, WRITE, "write_internal", "Confirm a task for the person who made this request. The Done checks must pass. Use force true and a reason to override failed checks."),
        ("task_reopen", Reason, WRITE, "write_internal", "Send a task back to its holder with a reason. The state becomes todo and waits_for names the holder. The record keeps the reason."),
        ("task_wake", Get, WRITE, "write_internal", "Wake the agent holder and record the person who requests the restart."),
        ("task_cancel", Reason, WRITE, "write_internal", "Cancel a task with a reason. It has no next move. The task and its full record stay in the ledger for later reads."),
    ]
    for name, arguments, profiles, risk, description in entries:
        async def call(ctx, args, name=name):
            return await handle(ctx, name, args)
        registry.add(Tool(name, "tasks", risk, lambda _: "ledger", False, call,
                          description=description, arguments=arguments, profiles=profiles,
                          exclude_unset=name == "task_update"))
