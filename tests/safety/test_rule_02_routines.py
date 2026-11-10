"""Rule 2: a team routine has no personal connector."""

from datetime import UTC, datetime

from illo.db import one
from illo.routines.scheduler import tick_routines
from illo.tools.registry import Tool
from tests.routines.test_scheduler import configure


async def test_rule_02_team_routine_has_no_personal_tool(app, fake_brain):
    ctx = app.state.ctx
    configure(ctx)
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
    await tick_routines(ctx, datetime(2026, 1, 1, 9, tzinfo=UTC))
    assert not called
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT decision FROM audit WHERE tool='mail_search'"))[
            "decision"
        ] == "deny"
