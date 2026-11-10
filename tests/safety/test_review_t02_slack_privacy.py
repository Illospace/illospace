"""Rule 3: failed privacy probes preserve the index and report the missed event."""

from datetime import UTC, datetime, timedelta

from illo.channels.slack.access import channel_private
from illo.channels.slack.intake import SlackIntake
from illo.db import one
from illo.ledger import items, kv


async def test_rule_03_privacy_probe_error_keeps_index_and_records_failed_event(
    app, slack_client, fake_brain
):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        await items.upsert(conn, source_ref="slack:C_ACME_ALERTS:300.1", kind="slack",
                           title="Alert", body="Earlier alert", visibility="team")
    async def rate_limited(**kwargs):
        raise RuntimeError("ratelimited")
    slack_client.conversations_info = rate_limited
    result = await SlackIntake(ctx, "U_ACME_ILLO").receive({"event": {
        "type": "message", "subtype": "bot_message", "bot_id": "B_HOOK",
        "channel": "C_ACME_ALERTS", "ts": "301.1", "text": "Machine down",
    }})
    assert result["decision"] == "ignore"
    assert not fake_brain.requests
    async with ctx.pool.connection() as conn:
        assert await one(conn, "SELECT * FROM items WHERE kind='slack'")
        event = await one(conn, "SELECT * FROM events")
        assert event and event["status"] == "failed"
        assert await one(conn, "SELECT * FROM sensors WHERE key='slack:privacy:C_ACME_ALERTS' AND last_error_at IS NOT NULL")


async def test_rule_03_privacy_cache_lasts_ten_minutes_and_refreshes(app, slack_client):
    ctx = app.state.ctx
    assert await channel_private(ctx, "C_ACME_ALERTS") is False
    async def now_private(**kwargs):
        await slack_client._record("conversations_info", **kwargs)
        return {"channel": {"is_private": True}}
    slack_client.conversations_info = now_private
    assert await channel_private(ctx, "C_ACME_ALERTS") is False
    assert len([c for c in slack_client.calls if c["method"] == "conversations_info"]) == 1
    async with ctx.pool.connection() as conn:
        cached = await kv.get(conn, "slack:privacy:C_ACME_ALERTS")
        cached["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        await kv.set(conn, "slack:privacy:C_ACME_ALERTS", cached)
    assert await channel_private(ctx, "C_ACME_ALERTS") is True


async def test_rule_03_slack_client_retries_rate_limits(app, monkeypatch):
    from pydantic import SecretStr
    from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler

    from illo.channels.slack.connection import SlackConnection, setup

    async def idle(self):
        return
    monkeypatch.setattr(SlackConnection, "run", idle)
    ctx = app.state.ctx
    ctx.config.slack_app_token = SecretStr("test-app")
    ctx.config.slack_bot_token = SecretStr("test-bot")
    await setup(ctx)
    assert any(isinstance(handler, AsyncRateLimitErrorRetryHandler)
               for handler in ctx.slack_client.retry_handlers)
