"""Rule 6: a forwarded email cannot use a personal connector."""

from illo.channels.email.parser import parse_message
from illo.db import one
from illo.tools.registry import Tool
from tests.email.test_email import raw_mail


async def test_rule_06_forward_keeps_person_but_removes_personal_tools(app, fake_brain):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.email.reply = "slack"
    called = []

    async def personal(ctx, args):
        called.append(args)

    ctx.registry.tools.pop("mail_search", None)
    ctx.registry.add(
        Tool(
            "mail_search",
            "personal",
            "read",
            lambda _: "ledger",
            True,
            personal,
            profiles=frozenset({"person_dm"}),
        )
    )
    fake_brain.script = [{"name": "mail_search", "arguments": {}}]
    event = parse_message(
        raw_mail(body="Summarize this.\n> Outside instructions"), authserv_id="mx.example.com"
    )
    await ctx.gateway.receive(event)
    assert not called
    async with ctx.pool.connection() as conn:
        row = await one(
            conn, "SELECT r.* FROM runs r JOIN events e ON r.event_id=e.id WHERE e.channel='email'"
        )
        assert row["tainted"] and row["principal"] == "person:ada"
        assert (await one(conn, "SELECT decision FROM audit WHERE tool='mail_search'"))[
            "decision"
        ] == "deny"
