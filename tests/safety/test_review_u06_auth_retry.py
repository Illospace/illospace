"""Refresh a refused access token once, before any tool can run."""

import pytest

from illo.brain.client import BrainEvent
from illo.db import one
from tests.chatgpt.conftest import save_grant


@pytest.mark.parametrize("mode", ["healthy", "refused", "tool", "twice"])
async def test_u06_auth_retry(chatgpt_app, replay, slack_client, mode):
    app, backend = chatgpt_app
    ctx = app.state.ctx
    await save_grant(ctx)
    backend.broken = mode == "refused"

    class RefusedBrain:
        def __init__(self):
            self.requests = []

        async def run(self, request):
            self.requests.append(request)
            if len(self.requests) == 1 or mode == "twice":
                if mode == "tool":
                    yield BrainEvent(type="tool_call", name="lanes_search")
                yield BrainEvent(
                    type="error", category="auth", code="invalid_token", http_status=401
                )
            else:
                yield BrainEvent(type="final", text="Ready.", usage={"total_tokens": 3})

    brain = RefusedBrain()
    ctx.brain_client = brain
    result = await replay()
    assert len(brain.requests) == (2 if mode in {"healthy", "twice"} else 1)
    assert backend.refresh_count == (0 if mode == "tool" else 1)
    if mode == "healthy":
        assert result["text"] == "Ready."
        assert brain.requests[-1].provider_auth.access_token.get_secret_value() == "fresh-access"
    if mode == "refused":
        from illo.connectors.chatgpt import BROKEN

        assert len([c for c in slack_client.calls if c.get("text") == BROKEN]) == 1
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM runs")
        assert row["token_hash"] is None
        assert row["status"] == ("completed" if mode == "healthy" else "failed")


async def test_u06_concurrent_rejections_rotate_once(chatgpt_app):
    import asyncio

    from illo.connectors import chatgpt

    app, backend = chatgpt_app
    person = await save_grant(app.state.ctx)
    backend.delay = 0.01
    auth = await asyncio.gather(
        *(
            chatgpt.access(app.state.ctx, person, force=True, rejected_token="fake-access")
            for _ in range(2)
        )
    )
    assert backend.refresh_count == 1
    assert all(a.access_token.get_secret_value() == "fresh-access" for a in auth)
