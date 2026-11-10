"""Rule 4: outbound email always creates a draft without SMTP."""

import pytest

from illo.db import one


@pytest.mark.parametrize("recipient", ["customer@example.net", "ben@example.com"])
async def test_rule_04_outbound_email_is_a_draft(app, rpc, rpc_token, recipient):
    run, token = rpc_token
    result = (
        await rpc(
            token,
            "tools/call",
            {
                "name": "email_draft",
                "arguments": {"to": recipient, "subject": "A reply", "body": "Hello."},
            },
        )
    ).json()["result"]["structuredContent"]
    assert result["decision"] == "draft"
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM drafts WHERE run_id=%s", (run["id"],))
        assert row["state"] == "pending" and row["kind"] == "email_draft"
