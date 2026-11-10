"""Rule 6: model claims cannot reserve a server routine slot."""

import pytest

from illo.db import one
from illo.ledger import claims


@pytest.mark.parametrize("tainted", [False, True])
async def test_rule_06_model_claim_cannot_suppress_routine(app, rpc, rpc_token, tainted):
    run, token = rpc_token
    if tainted:
        async with app.state.ctx.pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET tainted=true,profile='tainted',principal='team',person_id=NULL "
                "WHERE id=%s", (run["id"],),
            )
    key = "routine:daily-digest:2026-11-11T08:00:00+00:00"
    response = await rpc(token, "tools/call", {
        "name": "claims_take", "arguments": {"key": key},
    })
    assert response.json()["result"]["structuredContent"]["result"] is True
    async with app.state.ctx.pool.connection() as conn:
        assert await one(conn, "SELECT * FROM claims WHERE key=%s", (key,)) is None
        assert await claims.take(conn, key, None)
        assert await one(conn, "SELECT * FROM claims WHERE key=%s", ("model:" + key,))
    again = await rpc(token, "tools/call", {
        "name": "claims_take", "arguments": {"key": key},
    })
    assert again.json()["result"]["structuredContent"]["result"] is False
