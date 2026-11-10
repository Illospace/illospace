"""Search tools share the run's SQL visibility and personal-read state."""

import json

from fastapi.encoders import jsonable_encoder
from pydantic import Field

from illo.sources import search
from illo.tools.registry import Arguments, Tool

# Hostile text may search team data. SQL hides private rows and providers skip personal reads.
READ_PROFILES = frozenset({"person_dm", "team_channel", "routine", "agent_request"})
HINT = "Search again with other words if this is not enough."


class SearchArgs(Arguments):
    query: str = Field(min_length=1, max_length=2000)
    kinds: list[str] | None = None
    limit: int = Field(default=10, ge=1, le=50)


class GetArgs(Arguments):
    source_ref: str = Field(min_length=1, max_length=2000)


def format_hits(hits):
    blocks = [
        f"{hit.title}\nRef: {hit.source_ref}\nSource: {hit.source} | Date: {hit.at or 'unknown'}"
        f"\n{hit.snippet}\n{hit.link or ''}".strip()
        for hit in hits
    ]
    if hits.partial:
        blocks.append("Some sources are unavailable: " + ", ".join(hits.failures))
    return "\n\n".join([*blocks, HINT])


async def search_all(ctx, args):
    hits = await search.search(
        ctx.app,
        ctx.principal,
        **args,
        conn=ctx.conn,
        tainted=ctx.run["tainted"],
        personal_allowed=ctx.run["profile"] in {"person_dm", "agent_request"},
    )
    if hits.personal_read:
        await search.mark_personal(ctx)
    return format_hits(hits)


async def search_get(ctx, args):
    row = await search.get(ctx.app, ctx.principal, args["source_ref"], conn=ctx.conn)
    if row and row.get("visibility") == "private":
        await search.mark_personal(ctx)
    return (
        (
            json.dumps(jsonable_encoder(row), ensure_ascii=False)
            if row
            else "No visible item has this ref."
        )
        + "\n"
        + HINT
    )


def register(registry) -> None:
    for name, handler, arguments in [
        ("search_all", search_all, SearchArgs),
        ("search_get", search_get, GetArgs),
    ]:
        registry.add(
            Tool(
                name,
                "search",
                "read",
                lambda a: "ledger",
                False,
                handler,
                "Read visible team context and your own private context",
                arguments,
                profiles=READ_PROFILES,
            )
        )
