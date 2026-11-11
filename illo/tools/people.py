"""People tools. The workspace remains the source of identities."""

from pydantic import Field

from illo.ledger import people
from illo.tools._format import line, rows
from illo.tools.registry import Arguments, Tool


class GetArgs(Arguments):
    key: str = Field(min_length=1)


async def people_list(ctx, args):
    """Read the active team list."""
    return rows(
        await people.list_people(ctx.conn),
        lambda p: line(
            p["key"], p["name"], p["role"], ", ".join(p["areas"]), p.get("github_login")
        ),
    )


async def people_get(ctx, args):
    """Read one active person."""
    return await people.get(ctx.conn, args["key"])


DESCRIPTIONS = {
    "people_list": "List the active team members: key, name, role, areas, and GitHub login. Use the key in `owner`, `person`, and `assignee` arguments.",
    "people_get": "Read one active team member by their `key`.",
}


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
                DESCRIPTIONS[name],
                arguments,
            )
        )
