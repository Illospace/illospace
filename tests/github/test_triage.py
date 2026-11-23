"""Only code can assign a new outside ticket from a trusted source."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from illo.asks import drafts
from illo.channels.slack.intake import SlackIntake
from illo.channels.slack.limits import NOTICE_PREFIX, flush_owner_notices
from illo.connectors.github_client import GitHubError
from illo.db import all_rows, one
from illo.ledger import kv, people, runs
from illo.tools.github import CreateArgs
from illo.workspace import TriageConfig
from tests.tool_results import read_result

REPO = "acme-example/engine"
CREATE = {"repo": REPO, "title": "Export fails", "body": "Alert data", "signature": "export failure"}


async def alert(app, fake_brain, n=0, **fields):
    fake_brain.script = [{"name": "github_create_issue", "arguments": {
        **CREATE, "signature": f"export failure {n}"}}]
    intake = SlackIntake(app.state.ctx, "U_ACME_ILLO")
    result = await intake.receive({"event": {
        "type": "message", "channel": "C_ACME_ALERTS", "ts": f"{1700000000+n}.1",
        "subtype": "bot_message", "bot_id": "B_MONITOR", "text": "Export fails", **fields}})
    assert result["decision"] == "run" and "error" not in result
    return read_result(fake_brain.results[-1])["result"]


def trust(app):
    app.state.ctx.workspace.config.triage.auto_assign.slack_bots = ["B_MONITOR"]


async def test_trusted_monitor_assigns_and_audits_without_owner_dm(app, fake_brain, slack_client, github_client):
    trust(app)
    result = await alert(app, fake_brain)
    assert result["assignment"] == "automatic" and result["assigned_to"] == "acme-ada-example"
    assert result["issue"]["assignees"] == [{"login": "acme-ada-example"}]
    assert result["issue"]["labels"] == ["from-outside-text"]
    assert not any(c["method"] == "conversations_open" for c in slack_client.calls)
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM audit WHERE tool='github_assign_issue'")
        assert row["decision"] == "allow" and row["reason"] == "Automatic assignment for a trusted monitor"
        assert not await all_rows(conn, "SELECT * FROM drafts")


@pytest.mark.parametrize("fields", [
    {"bot_id": "B_OTHER"}, {"workflow_id": "W_FORM"},
    {"user": "U_FOREIGN", "user_team": "T_FOREIGN"},
    {"user": "U_ACME_ADA", "user_team": "T_FOREIGN"},
    {"thread_ts": "1699999999.1"},
])
async def test_untrusted_sources_get_one_card(app, fake_brain, slack_client, fields):
    trust(app)
    slack_client.messages = {"old": {"ts": "1699999999.1", "user": "U_FOREIGN", "text": "Bad request"}}
    result = await alert(app, fake_brain, **fields)
    assert result["assignment"] == "waits_for_owner" and result["assigned_to"] is None
    async with app.state.ctx.pool.connection() as conn:
        rows = await all_rows(conn, "SELECT * FROM drafts")
        assert len(rows) == 1 and rows[0]["kind"] == "github_assign_issue"
        assert rows[0]["content"]["arguments"]["assignee"] == "acme-ada-example"
    cards = [c for c in slack_client.calls if c["method"] == "chat_postMessage" and c.get("blocks")]
    assert len(cards) == 1
    assert "Export fails" in str(cards[0]["blocks"])
    assert "#alerts" in str(cards[0]["blocks"])
    assert "Automation" in str(cards[0]["blocks"])
    assert "acme.slack.com/archives/" in str(cards[0]["blocks"])
    assert cards[0]["blocks"][-1]["elements"][0]["text"]["text"] == "Assign to Ada"


async def test_hourly_limit_uses_card_path(app, fake_brain, slack_client):
    trust(app)
    app.state.ctx.workspace.config.triage.max_per_hour = 1
    assert (await alert(app, fake_brain))["assignment"] == "automatic"
    assert (await alert(app, fake_brain, 1))["assignment"] == "waits_for_owner"
    async with app.state.ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT * FROM drafts")) == 1


async def test_github_refusal_uses_card_path(app, fake_brain, github_client):
    trust(app)
    github_client.assignment_error = GitHubError(403)
    result = await alert(app, fake_brain)
    assert result["assignment"] == "waits_for_owner"
    async with app.state.ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT * FROM drafts")) == 1
        assert not await one(conn, "SELECT * FROM audit WHERE tool='github_assign_issue'")


@pytest.mark.parametrize("trusted", [False, True])
async def test_machine_signal_source(app, fake_brain, trusted, slack_client):
    from illo.gateway import InboundEvent
    if trusted:
        app.state.ctx.workspace.config.triage.auto_assign.signal_sources = ["log-scan"]
    fake_brain.script = [{"name": "github_create_issue", "arguments": CREATE}]
    await app.state.ctx.gateway.receive(InboundEvent(channel="signals", kind="signal", external_id="signal-1",
        text="Export fails", payload={"source": "log-scan"}))
    result = read_result(fake_brain.results[-1])["result"]
    assert result["assignment"] == ("automatic" if trusted else "waits_for_owner")
    if not trusted:
        assert "the signal source log-scan" in str(slack_client.calls)


@pytest.mark.parametrize("verb", ["approve", "reject", "expired", "wrong_person"])
async def test_owner_card_uses_existing_approval_checks(app, fake_brain, github_client, slack_client, verb):
    await alert(app, fake_brain)
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM drafts")
        person = await people.get(conn, "ben" if verb == "wrong_person" else "ada")
        if verb == "expired":
            await conn.execute("UPDATE drafts SET created_at=now()-interval '25 hours'")
        owner_run = await runs.get(conn, row["run_id"])
        assert owner_run["person_id"] == row["approver_person_id"]
        assert owner_run["status"] == "completed" and not owner_run.get("token_hash")
    action = {"action_id": f"draft:{row['id']}:{'reject' if verb == 'reject' else 'approve'}",
              "channel": row["slack_channel"], "message_ts": row["slack_ts"]}
    await drafts.handle(app.state.ctx, person, action)
    await drafts.handle(app.state.ctx, person, action)
    assigned = [c for c in github_client.calls if c["method"] == "assign_issue"]
    assert len(assigned) == int(verb == "approve")
    if verb in {"approve", "reject"}:
        assert ("Assigned to Ada." if verb == "approve" else "Left unassigned.") in str(slack_client.calls)


async def test_digest_titles_and_old_url_state(app, slack_client, monkeypatch):
    from illo.channels.slack import limits
    later = datetime.now(UTC) + timedelta(hours=2)
    monkeypatch.setattr(limits, "now", lambda: later)
    async with app.state.ctx.pool.connection() as conn:
        await kv.set(conn, NOTICE_PREFIX + "ada", {"sent": [], "due_at": later.timestamp()-1, "pending": [
            "https://github.com/acme-example/engine/issues/1",
            {"url": "https://github.com/acme-example/engine/issues/2", "repo": REPO, "number": 2,
             "title": "Export <fails>"}]})
    await flush_owner_notices(app.state.ctx)
    text = slack_client.calls[-1]["text"]
    assert text == ("Tickets that wait for an owner:\n"
                    "• <https://github.com/acme-example/engine/issues/1|engine#1>\n"
                    "• <https://github.com/acme-example/engine/issues/2|engine#2> Export &lt;fails&gt;\n"
                    "Tell me here which ones to assign.")


@pytest.mark.parametrize("value", ["", "B BAD", "B\tBAD"])
def test_config_rejects_invalid_bot_id(value):
    with pytest.raises(ValidationError):
        TriageConfig(auto_assign={"slack_bots": [value]})


@pytest.mark.parametrize("value", [0, 101])
def test_config_rejects_invalid_hourly_limit(value):
    with pytest.raises(ValidationError):
        TriageConfig(max_per_hour=value)


def test_model_cannot_choose_assignee():
    with pytest.raises(ValidationError):
        CreateArgs(**CREATE, assignee="ben")
    assert TriageConfig().model_dump() == {"auto_assign": {"slack_bots": [], "signal_sources": []}, "max_per_hour": 6}


async def test_concurrent_assignments_share_the_durable_hourly_limit(app, github_client):
    import asyncio

    from illo.tools._triage import auto_assign
    from illo.tools.registry import ToolContext

    trust(app)
    ctx = app.state.ctx
    ctx.workspace.config.triage.max_per_hour = 1
    event = {"channel": "slack", "kind": "message", "payload": {"payload": {
        "channel_id": "C_ACME_ALERTS", "bot_id": "B_MONITOR", "from_automation": True,
        "outside_workspace": False}}}
    for n in range(3):
        await github_client.create_issue(REPO, f"Failure {n}", "Alert data")

    async def assign(number):
        async with ctx.pool.connection() as conn:
            run = await runs.create(conn, principal="team", person_id=None, profile="tainted",
                                    tainted=True, runtime="fake", model="fake")
            return await auto_assign(ToolContext(conn, run, ctx), event, REPO, number)

    results = await asyncio.gather(*(assign(n) for n in range(1, 4)))
    assert results.count("acme-ada-example") == 1
    assert len([c for c in github_client.calls if c["method"] == "assign_issue"]) == 1
    async with ctx.pool.connection() as conn:
        assert len(await kv.get(conn, "triage:auto-assign")) == 1
        assert len(await all_rows(conn, "SELECT * FROM audit WHERE tool='github_assign_issue'")) == 1


async def test_missing_owner_slack_id_keeps_the_ticket_in_the_digest(app, fake_brain):
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET slack_id=NULL WHERE key='ada'")
    result = await alert(app, fake_brain)
    assert result["assignment"] == "waits_for_owner"
    async with app.state.ctx.pool.connection() as conn:
        state = await kv.get(conn, NOTICE_PREFIX + "ada")
        assert state["pending"][0]["title"] == "Export fails"
        assert (await one(conn, "SELECT * FROM sensors WHERE key='github:owner-notice'"))["last_error_at"]
