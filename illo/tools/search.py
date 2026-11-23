"""Search tools share the run's SQL visibility and personal-read state."""

from pydantic import Field

from illo.sources import search
from illo.tools._format import Compact, body, data_text, header, hit, iso_date, line
from illo.tools.registry import Arguments, Tool

# Only untainted profiles may search. SQL hides private rows from other principals.
READ_PROFILES = frozenset({"person_dm", "team_channel", "routine", "agent_request"})
HINT = "Search again with other words if this is not enough."


class SearchArgs(Arguments):
    query: str = Field(min_length=1, max_length=2000)
    kinds: list[str] | None = None
    limit: int = Field(default=8, ge=1, le=20)


class GetArgs(Arguments):
    source_ref: str = Field(min_length=1, max_length=2000)
    offset: int = Field(default=0, ge=0)


def format_hits(hits):
    blocks = [
        header(
            len(hits),
            f"at least {len(hits)}",
            "Narrow the query, or pass limit=. Use search_get with source_ref= for detail.",
        )
    ]
    blocks.extend(hit(value) for value in hits)
    if hits.partial:
        blocks.append("Some sources are unavailable: " + ", ".join(hits.failures))
    return Compact(data_text("search", "\n".join([*blocks, HINT])), hits.personal_read)


async def search_all(ctx, args):
    hits = await search.search(
        ctx.app,
        ctx.principal,
        **args,
        conn=ctx.conn,
        tainted=ctx.run["tainted"],
        personal_allowed=ctx.run["profile"] == "person_dm",
    )
    if hits.personal_read:
        await search.mark_personal(ctx)
    return format_hits(hits)


async def search_get(ctx, args):
    row = await search.get(ctx.app, ctx.principal, args["source_ref"], conn=ctx.conn)
    if row and row.get("visibility") == "private":
        await search.mark_personal(ctx)
    if not row:
        return "No visible item has this ref."
    text = row.get("body") or row.get("summary") or row.get("text") or ""
    if not isinstance(text, str):
        text = str(text)
    offset = args["offset"]
    page = text[offset : offset + 6000]
    remaining = max(0, len(text) - offset - len(page))
    lines = [
        line(
            row.get("kind"),
            row.get("title"),
            args["source_ref"],
            iso_date(row.get("source_at") or row.get("created_at")),
        ),
        page,
    ]
    if remaining:
        lines.append(f"{remaining} characters omitted. Read on with offset={offset + len(page)}.")
    lines.extend(str(link) for link in row.get("links", []))
    if row.get("events"):
        lines.extend(body(event.get("text"), 300) for event in row["events"][-5:])
    if row.get("error"):
        lines.append(row["error"])
    return Compact(data_text("search", "\n".join(lines)), row.get("visibility") == "private")


DESCRIPTIONS = {
    "search_all": "Search everything this run can see, ranked by match and recency. Optional `kinds`: saved, decision, note, lane_summary, archive, slack, github, doc, lane, and mail (this person's mail, DM only). Read one result in full with `search_get` and its `source_ref`.",
    "search_get": "Read one search result in full by its `source_ref` from `search_all`. A long text comes in pages of 6,000 characters: pass `offset` to read on.",
}


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
                (DESCRIPTIONS[name]),
                arguments,
                profiles=READ_PROFILES,
            )
        )
