"""Rules 4 and 6: bound automation runs and owner notices across runs."""

from datetime import UTC, datetime, timedelta

from illo.channels.slack.intake import SlackIntake
from illo.db import all_rows, one


def post(n, channel="C_ACME_ALERTS"):
    return {"event": {
        "type": "message", "subtype": "bot_message", "bot_id": "B_HOOK",
        "channel": channel, "ts": f"{200+n}.1", "text": f"<@U_ACME_ILLO> Alert {n}",
    }}


async def test_rule_06_at_most_thirty_tainted_runs_per_channel_per_hour(app, slack_client, fake_brain):
    ctx = app.state.ctx
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    for n in range(30):
        assert (await intake.receive(post(n)))["decision"] == "run"
    assert (await intake.receive(post(30)))["decision"] == "ignore"
    async with ctx.pool.connection() as conn:
        sensor = await one(conn, "SELECT * FROM sensors WHERE key='slack:automation:C_ACME_ALERTS'")
        assert sensor and sensor["last_error_at"]
    assert (await intake.receive(post(31)))["decision"] == "ignore"
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT last_error_at FROM sensors WHERE key=%s", (sensor["key"],)))["last_error_at"] == sensor["last_error_at"]
        assert (await one(conn, "SELECT count(*) AS n FROM runs"))["n"] == 30
    assert len(fake_brain.requests) == 30
    assert (await intake.receive(post(32, "C_ACME_TEAM")))["decision"] == "run"
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE events SET created_at=now()-interval '61 minutes'")
    assert (await intake.receive(post(33)))["decision"] == "run"


async def test_rule_06_owner_notices_are_capped_and_remaining_links_reach_one_digest(
    app, slack_client, github_client, fake_brain, monkeypatch
):
    ctx = app.state.ctx
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    for n in range(7):
        fake_brain.script = [{"name": "github_create_issue", "arguments": {
            "repo": "acme-example/engine", "title": f"Outage {n}-{k}",
            "body": "Alert data", "signature": f"outage number {n} part {k}",
        }} for k in range(3)]
        assert (await intake.receive(post(n)))["decision"] == "run"
    notices = [c for c in slack_client.calls if c["method"] == "chat_postMessage"
               and "New ticket from outside text" in str(c.get("blocks", []))]
    assert len(github_client.issues) == 21
    assert len(notices) == 6
    async with ctx.pool.connection() as conn:
        audit = await all_rows(conn, "SELECT run_id,count(*) AS n FROM audit GROUP BY run_id")
    assert len(audit) == 7 and all(r["n"] == 3 for r in audit)
    from illo.channels.slack import limits
    later = datetime.now(UTC) + timedelta(minutes=61)
    monkeypatch.setattr(limits, "now", lambda: later)
    before = len(slack_client.calls)
    await limits.flush_owner_notices(ctx)
    await limits.flush_owner_notices(ctx)
    digests = [c for c in slack_client.calls[before:] if c["method"] == "chat_postMessage"]
    assert len(digests) == 1
    combined = "\n".join(str(c.get("blocks", [])) + c["text"] for c in notices + digests)
    assert all(issue["html_url"] in combined for issue in github_client.issues)


async def test_rule_06_concurrent_automation_admissions_cannot_exceed_cap(app, fake_brain):
    import asyncio

    intake = SlackIntake(app.state.ctx, "U_ACME_ILLO")
    results = await asyncio.gather(*(intake.receive(post(n)) for n in range(35)))
    assert sum(result["decision"] == "run" for result in results) == 30
    assert len(fake_brain.requests) == 30
