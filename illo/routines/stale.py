"""Rebuild every existing ask card from the ledger once a minute."""

from illo.asks.service import tick


async def reconcile(ctx):
    await tick(ctx, force=True)


async def setup(ctx):
    async def check_stale():
        await reconcile(ctx)

    ctx.every(60, check_stale)
