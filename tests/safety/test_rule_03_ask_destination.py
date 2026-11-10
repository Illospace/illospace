"""Rule 3: personal data reaches another ask owner only after requester approval."""

from illo.asks import drafts, service
from illo.db import one
from illo.ledger import people
from tests.tool_results import read_result


async def call(rpc, token, name, arguments):
    result = await rpc(token, "tools/call", {"name": name, "arguments": arguments})
    return read_result(result.json()["result"])


async def test_rule_03_personal_data_to_other_person_is_draft_until_approval(app, rpc, rpc_token):
    ctx = app.state.ctx
    run, token = rpc_token
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE runs SET personal_read=true WHERE id=%s", (run["id"],))
    result = await call(
        rpc, token, "asks_create", {"owner": "ben", "question": "Can you review this private plan?"}
    )
    assert result["decision"] == "draft"
    async with ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT * FROM asks")
        ada = await people.get(conn, "ada")
    await drafts.tick(ctx)
    async with ctx.pool.connection() as conn:
        draft = await one(conn, "SELECT * FROM drafts")
    await drafts.handle(
        ctx,
        ada,
        {
            "action_id": f"draft:{draft['id']}:approve",
            "channel": draft["slack_channel"],
            "message_ts": draft["slack_ts"],
        },
    )
    async with ctx.pool.connection() as conn:
        assert (
            await one(
                conn, "SELECT a.question,p.key FROM asks a JOIN people p ON p.id=a.owner_person_id"
            )
        )["key"] == "ben"
    await service.tick(ctx)
