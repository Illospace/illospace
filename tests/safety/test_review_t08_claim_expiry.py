"""Rule 6: model claims expire without deleting server or recent claims."""

from illo.db import all_rows
from illo.ledger import claims
from illo.routines.stale import reconcile


async def test_rule_06_old_model_claim_expires(app, rpc, rpc_token):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        for key in ("model:old-alert", "model:recent-alert", "routine:server-key"):
            await claims.take(conn, key, None)
        await conn.execute(
            "UPDATE claims SET created_at=now()-interval '25 hours' WHERE key <> 'model:recent-alert'"
        )
    await reconcile(ctx)
    async with ctx.pool.connection() as conn:
        keys = {row["key"] for row in await all_rows(conn, "SELECT key FROM claims")}
    assert keys == {"model:recent-alert", "routine:server-key"}
    response = await rpc(
        rpc_token[1],
        "tools/call",
        {
            "name": "claims_take",
            "arguments": {"key": "old-alert"},
        },
    )
    assert response.json()["result"]["structuredContent"]["result"] is True
