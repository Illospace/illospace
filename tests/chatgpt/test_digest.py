"""A failed personal sign-in still gives exactly one morning brief."""

import pytest

from illo.brain.client import BrainEvent
from illo.db import one
from illo.ledger import chatgpt as grants
from illo.routines.digest import FIRST_BRIEF, fallback_text, send_due
from tests.asks.test_digest import NOW, empty, only_ada, posts
from tests.chatgpt.conftest import save_grant


@pytest.mark.parametrize("mode", ["missing", "broken", "auth_retry", "usage_limit"])
async def test_failed_personal_credential_gives_only_fallback(chatgpt_app, slack_client, mode):
    app, backend = chatgpt_app
    ctx = app.state.ctx
    await only_ada(ctx)
    person = None
    if mode != "missing":
        person = await save_grant(ctx, expires=0 if mode == "broken" else 3600)
    backend.broken = mode in {"broken", "auth_retry"}

    class FailedBrain:
        async def run(self, request):
            if mode == "auth_retry":
                yield BrainEvent(
                    type="error", category="auth", code="invalid_token", http_status=401
                )
            else:
                yield BrainEvent(type="error", category="limit", code="usage_limit_reached")

    ctx.brain_client = FailedBrain()
    await send_due(ctx, NOW)
    await send_due(ctx, NOW)
    assert len(posts(slack_client)) == 1
    assert posts(slack_client)[0]["text"] == fallback_text(empty()) + "\n\n" + FIRST_BRIEF
    if mode in {"broken", "auth_retry"}:
        async with ctx.pool.connection() as conn:
            grant = await grants.load(conn, person["id"])
            assert grant["broken_at"] and grant["notified_at"] is None
    if mode in {"auth_retry", "usage_limit"}:
        async with ctx.pool.connection() as conn:
            run = await one(conn, "SELECT * FROM runs")
            assert run["status"] == "failed" and run["token_hash"] is None
