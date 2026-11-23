"""Accept each machine signal once and send only tainted events to the gateway."""

from illo.gateway import InboundEvent
from illo.ledger import mcp, signals


def event(row: dict) -> InboundEvent:
    return InboundEvent(
        channel="signals",
        external_id=str(row["id"]),
        kind="signal",
        text=f"{row['title']}\n\n{row['body']}",
        payload={
            "signal_id": row["id"],
            "source": row["source"],
            "severity": row["severity"],
            "data": row["data"],
        },
    )


async def submit(ctx, args: dict) -> dict:
    row = await signals.insert(ctx.conn, **args)
    duplicate = row is None
    if duplicate:
        row = await mcp.signal_by_key(ctx.conn, args["source"], args["dedupe_key"])
    else:
        await ctx.conn.execute(
            "UPDATE signals SET token_id=%s WHERE id=%s", (ctx.token["id"], row["id"])
        )
        await mcp.check_hourly_limit(ctx.conn, ctx.token["id"])
        ctx.events.append(event(row))
    return {"signal_id": row["id"], "duplicate": duplicate}


class Adapter:
    async def show_working(self, event):
        pass

    async def deliver_final(self, event, text):
        # A signal has no person or reply destination. Tools can file a ticket.
        pass

    async def deliver_error(self, event, *, category="internal"):
        pass


async def setup(ctx) -> None:
    """Recover receipts that committed before their gateway event started."""
    ctx.gateway.register("signals", Adapter())

    async def recover():
        async with ctx.pool.connection() as conn:
            rows = await mcp.pending_signals(conn)
        for row in rows:
            await ctx.gateway.receive(event(row))

    ctx.every(5, recover)
