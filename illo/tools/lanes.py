"""Lane tools use the authenticated principal for every visibility filter."""

from pydantic import Field

from illo.db import all_rows
from illo.ledger import lanes
from illo.tools._format import Compact, body, data_text, lane, line, relative, rows
from illo.tools.registry import Arguments, Tool


class MineArgs(Arguments):
    query: str = ""
    state: str | None = None
    limit: int = Field(default=15, ge=1, le=40)
    repo: str | None = None


class SearchArgs(MineArgs):
    owner: str | None = None


class GetArgs(Arguments):
    id: int = Field(gt=0)


def limits(ctx):
    return ctx.app.workspace.config.stale_minutes.model_dump(by_alias=True)


async def listing(ctx, args, *, owner=None, attention=False):
    values, counts, private = await lanes.compact_search(
        ctx.conn,
        principal=ctx.principal,
        stale_minutes=limits(ctx),
        owner=owner,
        attention=attention,
        **args,
    )
    formatted = rows(
        values,
        (lambda row: lane(row).replace("\n  ", " · ")) if attention else lane,
        total=values[0]["total"] if values else 0,
        hint="Narrow the query, or pass state= / repo= / limit=.",
    )
    count_line = "Counts: " + ", ".join(f"{state} {n}" for state, n in sorted(counts.items()))
    return Compact(data_text("lanes", count_line + "\n" + formatted.text), formatted.private or private)


async def lanes_search(ctx, args):
    args = dict(args)
    return await listing(ctx, args, owner=args.pop("owner"))


async def lanes_get(ctx, args):
    value = await lanes.get(
        ctx.conn, args["id"], principal=ctx.principal, stale_minutes=limits(ctx)
    )
    if not value:
        return None
    events = await all_rows(
        ctx.conn,
        """
        SELECT at,kind,left(text,300) AS text,count(*) OVER () AS total
        FROM lane_events WHERE lane_id=%s ORDER BY at DESC,id DESC LIMIT 5
    """,
        (value["id"],),
    )
    owners = await all_rows(
        ctx.conn, "SELECT key FROM people WHERE id=%s", (value["owner_person_id"],)
    )
    value["owner"] = owners[0]["key"] if owners else None
    text = [lane(value), body(value["summary"], 3000)]
    text.extend(str(link) for link in value["links"])
    text.append(f"Events: showing {len(events)} of {events[0]['total'] if events else 0}.")
    text.extend(line(relative(e["at"]), e["kind"], e["text"]) for e in events)
    return Compact(data_text("lanes", "\n".join(text)), value["visibility"] == "private")


async def lanes_mine(ctx, args):
    if not ctx.principal.startswith("person:"):
        return rows([], lane)
    return await listing(ctx, args, owner=ctx.principal[7:])


async def lanes_attention(ctx, args):
    from illo.tools.digest import digest_snapshot_read

    return await digest_snapshot_read(ctx, args)


DESCRIPTIONS = {
    "lanes_search": "Search the lanes that this run can see: agent or ticket work with a state (open = backlog, working, waiting_person, blocked, review, done, failed). Filter with `query`, `state`, `owner` (a person key), and `repo`. Returns counts by state and one line per lane with its id and link.",
    "lanes_get": "Read one lane by `id`: its summary, links, and last 5 events.",
    "lanes_mine": "Same as `lanes_search`, limited to the lanes of the person you act for. Returns nothing in a run with no person.",
    "lanes_attention": "Older name of `digest_snapshot`, with the same result. Use `digest_snapshot`.",
}


def register(registry) -> None:
    """Register the real lane handlers."""
    registry.add(
        Tool(
            "lanes_attention",
            "lanes",
            "read",
            lambda a: "ledger",
            True,
            lanes_attention,
            "Older name of `digest_snapshot`, with the same result. Use `digest_snapshot`.",
            profiles=frozenset({"person_dm"}),
        )
    )
    for name, handler, arguments in (
        ("lanes_search", lanes_search, SearchArgs),
        ("lanes_get", lanes_get, GetArgs),
        ("lanes_mine", lanes_mine, MineArgs),
    ):
        registry.add(
            Tool(
                name,
                "lanes",
                "read",
                lambda a: "ledger",
                False,
                handler,
                DESCRIPTIONS[name],
                arguments,
            )
        )
