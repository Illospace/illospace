"""A temporary provider failure must not destroy a working grant."""

import httpx
import pytest

from illo.connectors import chatgpt
from illo.db import one
from illo.ledger import chatgpt as grants
from tests.chatgpt.conftest import save_grant


@pytest.mark.parametrize("failure", [429, 503, "timeout", "network"])
async def test_refresh_backoff(chatgpt_app, slack_client, failure):
    app, backend = chatgpt_app
    ctx = app.state.ctx
    person = await save_grant(ctx, expires=60)
    calls = []

    async def flaky(request):
        calls.append(request)
        if len(calls) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("fake secret")
            if failure == "network":
                raise httpx.ConnectError("fake secret")
            return httpx.Response(failure)
        return await backend.respond(request)

    ctx.chatgpt_client = chatgpt.ChatGPTClient(httpx.MockTransport(flaky))
    # The public run path returns a short temporary-failure reply.
    from illo.gateway import InboundEvent
    from illo.gateway.principal import Authority

    result = await ctx.gateway.run_manager.execute(
        InboundEvent(channel="slack", external_id="temporary", kind="dm"),
        Authority("person:ada", "person_dm", person["id"]),
        None,
    )
    assert result == "I could not reach the model sign-in service. Try again in a minute."
    assert await chatgpt.access(ctx, person) is None
    assert len(calls) == 1
    async with ctx.pool.connection() as conn:
        row = await grants.load(conn, person["id"])
        assert row["broken_at"] is None and row["next_refresh_at"] is not None
        assert (await one(conn, "SELECT detail FROM sensors WHERE key='chatgpt:ada'"))["detail"]
        await conn.execute(
            "UPDATE connector_grants SET next_refresh_at=clock_timestamp()-interval '1 second'"
        )
    assert (await chatgpt.access(ctx, person)).kind == "chatgpt"
    assert len(calls) == 2
    assert not [c for c in slack_client.calls if c.get("text") == chatgpt.BROKEN]
