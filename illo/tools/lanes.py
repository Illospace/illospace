"""Lane tools use the authenticated principal for every visibility filter."""

from pydantic import Field

from illo.ledger import lanes
from illo.tools.registry import Arguments, Tool


class SearchArgs(Arguments):
    query: str = ""
    owner: str | None = None
    state: str | None = None
    limit: int = Field(default=20, ge=1, le=50)


class GetArgs(Arguments):
    id: int = Field(gt=0)


def limits(ctx):
    return ctx.app.workspace.config.stale_minutes.model_dump(by_alias=True)


async def lanes_search(ctx, args):
    """Search lanes visible to this run."""
    return await lanes.search(ctx.conn, principal=ctx.principal, stale_minutes=limits(ctx), **args)


async def lanes_get(ctx, args):
    """Read one visible lane."""
    return await lanes.get(ctx.conn, args["id"], principal=ctx.principal, stale_minutes=limits(ctx))


async def lanes_mine(ctx, args):
    """Read lanes owned by the run's person. A team has no personal lane list."""
    if not ctx.principal.startswith("person:"):
        return []
    return await lanes.search(
        ctx.conn, principal=ctx.principal, owner=ctx.principal[7:], stale_minutes=limits(ctx)
    )


def register(registry) -> None:
    """Register the real lane handlers."""
    for name, handler, arguments in (
        ("lanes_search", lanes_search, SearchArgs),
        ("lanes_get", lanes_get, GetArgs),
        ("lanes_mine", lanes_mine, Arguments),
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
