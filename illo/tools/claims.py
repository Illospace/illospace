"""Atomic claims prevent two coordinators from handling the same alert."""

from pydantic import Field

from illo.ledger import claims
from illo.tools.registry import Arguments, Tool, ToolContext


class TakeArgs(Arguments):
    key: str = Field(min_length=1, max_length=500)
    note: str = Field(default="", max_length=2000)


async def take(ctx: ToolContext, args: dict) -> bool:
    return await claims.take(ctx.conn, "model:" + args["key"], ctx.run["id"], args["note"])


async def expire_model_claims(conn) -> None:
    """Keep a model claim for at most 24 hours. Preserve server claims."""
    await conn.execute(
        "DELETE FROM claims WHERE key LIKE 'model:%' AND created_at < now()-interval '24 hours'"
    )


def register(registry) -> None:
    # A claim writes no outside text and reads no personal data.
    registry.add(
        Tool(
            "claims_take",
            "claims",
            "write_internal",
            lambda a: "ledger",
            False,
            take,
            arguments=TakeArgs,
            profiles=frozenset(
                {"person_dm", "team_channel", "tainted", "routine", "agent_request"}
            ),
        )
    )
