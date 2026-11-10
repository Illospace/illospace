"""Lane tools use the authenticated principal for every visibility filter."""

from pydantic import Field

from illo.db import all_rows
from illo.ledger import asks, lanes
from illo.tools._format import Compact, ask, body, lane, line, relative, rows
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
        ctx.conn, principal=ctx.principal, stale_minutes=limits(ctx),
        owner=owner, attention=attention, **args)
    formatted = rows(values, (lambda row: lane(row).replace("\n  ", " · ")) if attention else lane, total=values[0]["total"] if values else 0,
                     hint="Narrow the query, or pass state= / repo= / limit=.")
    count_line = "Counts: " + ", ".join(f"{state} {n}" for state, n in sorted(counts.items()))
    return Compact(count_line + "\n" + formatted.text, formatted.private or private)


async def lanes_search(ctx, args):
    args = dict(args)
    return await listing(ctx, args, owner=args.pop("owner"))


async def lanes_get(ctx, args):
    value = await lanes.get(ctx.conn, args["id"], principal=ctx.principal, stale_minutes=limits(ctx))
    if not value:
        return None
    events = await all_rows(ctx.conn, """
        SELECT at,kind,left(text,300) AS text,count(*) OVER () AS total
        FROM lane_events WHERE lane_id=%s ORDER BY at DESC,id DESC LIMIT 5
    """, (value["id"],))
    owners = await all_rows(ctx.conn, "SELECT key FROM people WHERE id=%s", (value["owner_person_id"],))
    value["owner"] = owners[0]["key"] if owners else None
    text = [lane(value), body(value["summary"], 3000)]
    text.extend(str(link) for link in value["links"])
    text.append(f"Events: showing {len(events)} of {events[0]['total'] if events else 0}.")
    text.extend(line(relative(e["at"]), e["kind"], e["text"]) for e in events)
    return Compact("\n".join(text), value["visibility"] == "private")


async def lanes_mine(ctx, args):
    if not ctx.principal.startswith("person:"):
        return rows([], lane)
    return await listing(ctx, args, owner=ctx.principal[7:])


async def lanes_attention(ctx, args):
    if not ctx.principal.startswith("person:"):
        return Compact("A personal attention list needs a person principal.")
    key = ctx.principal[7:]
    waiting = await asks.compact_list(ctx.conn, principal=ctx.principal, owner=key, limit=15)
    active = await listing(ctx, {"limit": 15}, owner=key, attention=True)
    ask_rows = rows(waiting, ask, total=waiting[0]["total"] if waiting else 0,
                    hint="Use asks_list with owner= and limit= for more.")
    return Compact("Open asks: " + ask_rows.text + "\nLanes: " + active.text,
                   ask_rows.private or active.private)


def register(registry) -> None:
    """Register the real lane handlers."""
    registry.add(Tool("lanes_attention", "lanes", "read", lambda a: "ledger", False,
                      lanes_attention, "Read your open asks and lanes that need attention",
                      profiles=frozenset({"person_dm", "routine"})))
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
                "Read visible agent lanes",
                arguments,
            )
        )
