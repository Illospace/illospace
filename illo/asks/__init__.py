"""Asks, cards, drafts, rules, and the decision log."""

from illo.asks.service import open_asks_summary

__all__ = ["open_asks_summary"]


async def setup(ctx):
    from illo.asks import actions, drafts, relay, service

    ctx.slack_actions.register("ask:", actions.handle)
    ctx.slack_actions.register("draft:", drafts.handle)
    ctx.gateway.register_claim(actions.claim_thread)

    async def check_asks():
        await service.tick(ctx)
        await drafts.tick(ctx)
        await relay.tick(ctx)

    ctx.every(2, check_asks)
