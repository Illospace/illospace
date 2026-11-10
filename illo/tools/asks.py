"""Ask read tools. The asks builder adds routing and write tools."""

from pydantic import Field

from illo.ledger import asks, people
from illo.tools.registry import Arguments, Tool


class ListArgs(Arguments):
    owner: str | None = None


class GetArgs(Arguments):
    id: int = Field(gt=0)


async def asks_list(ctx, args):
    """List visible open asks, optionally for one owner."""
    person = await people.get(ctx.conn, args["owner"]) if args["owner"] else None
    if args["owner"] and not person:
        return []
    return await asks.list_open(
        ctx.conn, owner_person_id=person["id"] if person else None, principal=ctx.principal
    )


async def asks_get(ctx, args):
    """Read one ask with the lane visibility filter."""
    return await asks.get(ctx.conn, args["id"], principal=ctx.principal)


def register(registry) -> None:
    """Register the foundation read tools."""
    for name, handler, arguments in (
        ("asks_list", asks_list, ListArgs),
        ("asks_get", asks_get, GetArgs),
    ):
        registry.add(
            Tool(
                name,
                "asks",
                "read",
                lambda a: "ledger",
                False,
                handler,
                "Read visible asks",
                arguments,
            )
        )
