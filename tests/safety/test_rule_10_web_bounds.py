"""Rule 10: the only web tool accepts inbound URLs and host roots."""

from psycopg.types.json import Jsonb

from illo.db import one
from illo.tools import web
from illo.tools.registry import ToolContext


async def test_rule_10_web_cannot_send_data_in_an_invented_url(conn, run, monkeypatch):
    from types import SimpleNamespace

    from illo.config import Config
    from illo.tools.registry import Registry

    registry = Registry()
    registry.discover()
    app_ctx = SimpleNamespace(config=Config())
    called = []

    async def fetch(url, **kwargs):
        called.append(str(url))
        return "Outside page"

    monkeypatch.setattr(web, "fetch_page", fetch)
    event = await one(
        conn,
        "INSERT INTO events(channel,external_id,kind,payload) VALUES ('slack','web','dm',%s) RETURNING id",
        (Jsonb({"text": "Read https://example.com/public?known=one"}),),
    )
    run["event_id"] = event["id"]
    ctx = ToolContext(conn, run, app_ctx)
    for url in [
        "https://example.com/?secret=private",
        "https://another.example/",
        "https://example.com/public?known=secret",
    ]:
        assert (await registry.call(ctx, "web_read_public_page", {"url": url}))[
            "decision"
        ] == "deny"
    assert not called
    for url in ["https://example.com/public?known=one", "https://example.com/"]:
        assert (await registry.call(ctx, "web_read_public_page", {"url": url}))[
            "decision"
        ] == "allow"
    assert len(called) == 2
