"""One narrow result tool for a code-started wait check."""

from typing import Literal

from pydantic import Field, field_validator

from illo.tasks import waits
from illo.tools.registry import Arguments, Tool


class Result(Arguments):
    outcome: Literal["complete", "restart", "person"]
    message: str = Field(min_length=1, max_length=900, pattern=r"\S")

    @field_validator("message")
    @classmethod
    def short_message(cls, value):
        if len(value.split()) > 90:
            raise ValueError("A wait result has 90 words at most.")
        return value


async def result(ctx, args):
    return await waits.apply(ctx.conn, ctx=ctx.app, run=ctx.run, **args)


def register(registry):
    registry.add(Tool("wait_result", "waits", "write_internal", lambda _: "ledger", False,
        result, "Record the one result of this wait check: complete, restart, or person. Give the proof or the one action in message. Use plain web addresses, no markup. 90 words at most.",
        Result, profiles=frozenset()))
