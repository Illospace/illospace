"""Rule tools expose ownership and enforce it for removal."""

from pydantic import Field

from illo.ledger import rules
from illo.tools.registry import Arguments, Tool


class RemoveArgs(Arguments):
    id: int = Field(gt=0)


async def rules_list(ctx, args):
    """Read team rules and the principal's own rules."""
    return await rules.list_rules(ctx.conn, principal=ctx.principal)


async def rules_remove_mine(ctx, args):
    """Remove only the authenticated person's own rule."""
    return {"removed": await rules.remove(ctx.conn, args["id"], ctx.run["person_id"])}


def register(registry) -> None:
    """Register rule handlers. A team run cannot remove a person's rule."""
    registry.add(
        Tool(
            "rules_list",
            "rules",
            "read",
            lambda a: "ledger",
            False,
            rules_list,
            "Read standing rules",
        )
    )
    registry.add(
        Tool(
            "rules_remove_mine",
            "rules",
            "write_internal",
            lambda a: "ledger",
            False,
            rules_remove_mine,
            "Remove one of your standing rules",
            RemoveArgs,
        )
    )
