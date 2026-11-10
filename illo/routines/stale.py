"""Rebuild every existing ask card from the ledger once a minute."""

from illo.asks.service import tick
from illo.tools.claims import expire_model_claims


async def reconcile(ctx):
    async with ctx.pool.connection() as conn:
        await expire_model_claims(conn)
    await tick(ctx, force=True)


async def setup(ctx):
    async def check_stale():
        await reconcile(ctx)

    ctx.every(60, check_stale)
