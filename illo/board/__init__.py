"""Deliver durable notifications for the agent message board."""


async def setup(ctx):
    from illo.board import coordinator, escalate
    from illo.ledger.board import backfill_tickets
    from illo.tasks.pages.signin import open_action

    async with ctx.pool.connection() as conn:
        await backfill_tickets(conn, ctx.workspace)
    ctx.gateway.register("board", coordinator.BoardAdapter(ctx))
    ctx.gateway.register_claim(escalate.claim_reply)
    ctx.slack_actions.register("web-open-board:", open_action)
