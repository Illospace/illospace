"""Rule 3: public requests and routine replies cannot export personal data."""

from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from illo.db import one
from illo.routines.scheduler import tick_routines
from illo.tools import web
from illo.tools.registry import Tool, ToolContext
from tests.routines.test_scheduler import configure


async def test_rule_03_web_denied_after_personal_read(conn, run, monkeypatch):
    from types import SimpleNamespace

    from illo.config import Config
    from illo.tools.registry import Registry

    registry = Registry()
    registry.discover()
    app_ctx = SimpleNamespace(config=Config())
    called = []

    async def fetch(*args, **kwargs):
        called.append(args)
        return "page"

    monkeypatch.setattr(web, "fetch_page", fetch)
    event = await one(
        conn,
        "INSERT INTO events(channel,external_id,kind,payload) VALUES ('slack','web','dm',%s) RETURNING id",
        (Jsonb({"text": "Read https://example.com/"}),),
    )
    run.update(event_id=event["id"], personal_read=True)
    result = await registry.call(
        ToolContext(conn, run, app_ctx),
        "web_read_public_page",
        {"url": "https://example.com/"},
    )
    assert result["decision"] == "deny" and not called


async def test_rule_03_personal_routine_shared_reply_becomes_draft(app, fake_brain, slack_client):
    ctx = app.state.ctx
    configure(ctx, principal="person:ada", destination="channel:alerts")

    async def personal(ctx, args):
        return "Personal data"

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
    fake_brain.final_text = "Personal result."
    await tick_routines(ctx, datetime(2026, 1, 1, 9, tzinfo=UTC))
    assert not slack_client.calls
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM drafts WHERE kind='routine_delivery'")
        assert row["state"] == "pending" and row["destination"] == "channel:C_ACME_ALERTS"
