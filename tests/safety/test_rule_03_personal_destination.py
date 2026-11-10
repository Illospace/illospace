"""Rule 3: a personal read makes a later shared write a draft."""

from illo.db import one
from illo.tools.registry import Tool


async def test_personal_read_is_stored_and_shared_write_is_draft(app, rpc, rpc_token):
    run, token = rpc_token
    sent = []

    async def read_personal(ctx, args):
        assert ctx.run["principal"] == "person:ada"
        return {"subject": "Example private message"}

    async def shared_write(ctx, args):
        sent.append(args)

    registry = app.state.ctx.registry
    registry.add(Tool("mail_search", "personal", "read", lambda a: "ledger", True, read_personal))
    registry.add(
        Tool(
            "slack_post_channel",
            "slack",
            "write_internal",
            lambda a: "channel:C_ACME_TEAM",
            False,
            shared_write,
        )
    )
    read = await rpc(token, "tools/call", {"name": "mail_search"})
    assert not read.json()["result"]["isError"]
    write = await rpc(token, "tools/call", {"name": "slack_post_channel"})
    assert write.json()["result"]["structuredContent"]["decision"] == "draft"
    assert not sent
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT personal_read FROM runs WHERE id=%s", (run["id"],)))[
            "personal_read"
        ]
        draft = await one(conn, "SELECT * FROM drafts WHERE run_id=%s", (run["id"],))
        assert draft["approver_person_id"] == run["person_id"]
        assert draft["destination"] == "channel:C_ACME_TEAM" and draft["state"] == "pending"
