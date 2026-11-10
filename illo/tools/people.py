"""People tools. The workspace remains the source of identities."""

from pydantic import Field

from illo.ledger import people
from illo.tools.registry import Arguments, Tool


class GetArgs(Arguments):
    key: str = Field(min_length=1)


async def people_list(ctx, args):
    """Read the active team list."""
    return await people.list_people(ctx.conn)


async def people_get(ctx, args):
    """Read one active person."""
    return await people.get(ctx.conn, args["key"])


def register(registry) -> None:
    """Register the real people handlers."""
    for name, handler, arguments in (
        ("people_list", people_list, Arguments),
        ("people_get", people_get, GetArgs),
    ):
        registry.add(
            Tool(
                name,
                "people",
                "read",
                lambda a: "ledger",
                False,
                handler,
                "Read active people from the workspace",
                arguments,
            )
        )
