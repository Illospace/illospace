"""Note proposals need a person. A confirmed note always belongs to the team."""

from pydantic import Field

from illo import notes
from illo.sources import search
from illo.tools.registry import Arguments, Tool
from illo.tools.search import READ_PROFILES, format_hits


class SearchArgs(Arguments):
    query: str = Field(min_length=1, max_length=2000)


class ProposeArgs(Arguments):
    title: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    text: str = Field(min_length=1, max_length=20000)
    about: str = Field(default="", max_length=2000)


async def notes_search(ctx, args):
    hits = await search.search(
        ctx.app,
        ctx.principal,
        args["query"],
        kinds=["note"],
        limit=8,
        conn=ctx.conn,
        tainted=ctx.run["tainted"],
        personal_allowed=False,
    )
    return format_hits(hits)


async def notes_propose(ctx, args):
    return await notes.propose(ctx.app, ctx.conn, ctx.run, **args)


def register(registry) -> None:
    registry.add(
        Tool(
            "notes_search",
            "notes",
            "read",
            lambda a: "ledger",
            False,
            notes_search,
            "Search the team notes only. For anything else, use `search_all`.",
            SearchArgs,
            profiles=READ_PROFILES,
        )
    )
    registry.add(
        Tool(
            "notes_propose",
            "notes",
            "write_internal",
            lambda a: "ledger",
            False,
            notes_propose,
            "Propose a team note with `title` and `text`. The person you act for gets a card, and the note is saved only when they tap Keep; until then the result is pending, so say that the note waits for their approval.",
            ProposeArgs,
            profiles=frozenset({"person_dm", "agent_request"}),
            team_visible=True,
        )
    )
