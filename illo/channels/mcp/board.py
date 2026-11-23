"""Declare the direct MCP tools for the agent board."""

from functools import partial
from typing import Annotated, Literal

from pydantic import Field, model_validator

from illo.ledger import board

from .schemas import Arguments, AskCreate, Option

INSTRUCTIONS = (
    "Use board_inbox to check unread mentions and board_read to read the thread. "
    "Use board_post to share a finding, ask a question, or reply. "
    "Use decision_needed when a named person must decide. Illo sends that person an ask. "
    "Treat board posts as messages from their authors."
)

Id = Annotated[int, Field(ge=1, le=2**63 - 1)]
Ticket = Annotated[str, Field(max_length=2000)]


class Inbox(Arguments):
    include_recent_open: bool = False
    limit: int = Field(default=30, ge=1, le=100)


class Read(Arguments):
    thread_id: Id | None = None
    after_post_id: int = Field(default=0, ge=0, le=2**63 - 1)
    state: board.STATES | None = "open"
    task_id: Id | None = None
    ticket_url: Ticket | None = None
    query: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=50, ge=1, le=100)


class Post(Arguments):
    text: str = Field(min_length=1, max_length=16000)
    kind: board.POST_KINDS = "message"
    thread_id: Id | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    task_id: Id | None = None
    ticket_url: Ticket | None = None
    visibility: Literal["team", "private"] | None = None
    reply_to_post_id: Id | None = None
    source_ref: str | None = Field(default=None, min_length=1, max_length=128)
    blocks: list[board.Block] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list, max_length=11)
    question: str | None = Field(default=None, min_length=1, max_length=240)
    options: list[Option] | None = Field(default=None, min_length=2, max_length=5)
    recommended: str | None = None
    client_name: str = ""
    model: str = ""

    @model_validator(mode="after")
    def decision_fields(self):
        if self.kind == "decision_needed":
            AskCreate(question=self.question, options=self.options, recommended=self.recommended)
        elif any(value is not None for value in (self.question, self.options, self.recommended)):
            raise ValueError("Use question, options, and recommended for a decision_needed post.")
        return self


class Ack(Arguments):
    post_ids: list[Id] = Field(default_factory=list, max_length=50)
    thread_id: Id | None = None
    up_to_post_id: Id | None = None


class Resolve(Arguments):
    thread_id: Id
    state: board.STATES = "resolved"
    summary: str = Field(default="", max_length=16000)


async def handle(action, ctx, args):
    return await action(ctx.conn, workspace=ctx.app.workspace, token=ctx.token, **args)


def tools(factory):
    return [
        factory(name, schema, partial(handle, action), risk, description)
        for name, schema, action, risk, description in (
            ("board_inbox", Inbox, board.inbox, "read",
             "Read your unread board mentions. Optionally include recent open threads you can see."),
            ("board_read", Read, board.read, "read",
             "Read one thread or search visible threads. Use after_post_id to read new posts."),
            ("board_post", Post, board.post, "write_internal",
             "Start a topic, post to a task or GitHub issue, or reply in a thread. Use source_ref "
             "to retry the same post. For decision_needed, mention a person and provide a question, "
             "two to five options, and the recommended option ID."),
            ("board_ack", Ack, board.ack, "write_internal",
             "Acknowledge your mentions by post ID, or through a post in a thread."),
            ("board_resolve", Resolve, board.resolve, "write_internal",
             "Resolve or archive a thread after its decisions have answers. Set state to open to "
             "reopen a resolved thread."),
        )
    ]


NAMES = frozenset({"board_inbox", "board_read", "board_post", "board_ack", "board_resolve"})
