"""Scout findings stay in scope and their buttons use a normal run."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.conninfo import make_conninfo

from illo.cli import execute, parser
from illo.db import one
from illo.deep import findings
from illo.deep.worker import tick
from illo.gateway import InboundEvent
from illo.gateway.principal import Authority
from illo.routines import scout
from tests.tool_results import read_result

NOW = datetime(2026, 11, 13, 7, tzinfo=UTC)
FINDING = {
    "key": "sample-api-retirement",
    "title": "Check the API version",
    "message": "The API retires on December 1. The config selects it. I could not check the live override. Test the replacement.",
    "links": [
        {"title": "Retirement notice", "url": "https://example.com/notice"},
        {"title": "API config", "url": "https://github.com/acme/app/blob/main/config.py#L2"},
    ],
    "person": "ada",
}


def posts(slack):
    return [c for c in slack.calls if c["method"] == "chat_postMessage"]


async def finding(ctx, fake_brain, *, personal=False):
    async with ctx.pool.connection() as conn:
        await scout.queue(ctx, conn, person_key="ada" if personal else None)
    fake_brain.script = [
        {"name": "findings_list", "arguments": {}},
        {"name": "finding_add", "arguments": FINDING},
    ]
    await tick(ctx)
    async with ctx.pool.connection() as conn:
        return await one(conn, "SELECT * FROM scout_findings")


async def test_schedule_slot_dedupe(app):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.timezone = "UTC"
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET timezone='UTC'")
    ctx.workspace.config.scout.team = "on"
    ctx.workspace.config.scout.people = ["ada"]
    await scout.send_due(ctx, NOW)
    await scout.send_due(ctx, NOW + timedelta(seconds=20))
    async with ctx.pool.connection() as conn:
        rows = await (await conn.execute("SELECT * FROM deep_runs ORDER BY id")).fetchall()
    assert len(rows) == 2
    assert rows[0]["principal"] == "team" and rows[1]["principal"] == "person:ada"
    assert all(row["kind"] == "scout" and row["deliver"] == {"none": True} for row in rows)
    assert rows[0]["job"] == scout.TEAM_JOB.replace("{max}", "2")
    assert rows[1]["job"] == scout.PERSON_JOB.replace("{max}", "2")


@pytest.mark.parametrize("personal", [False, True])
async def test_save_delivery_links_buttons_and_scope(app, fake_brain, slack_client, personal):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.scout.destination = "C_ACME_TEAM"
    row = await finding(ctx, fake_brain, personal=personal)
    assert row["state"] == "sent" and row["scope"] == ("person" if personal else "team")
    sent = posts(slack_client)
    assert len(sent) == 1
    assert sent[0]["channel"] == ("D_EXAMPLE" if personal else "C_ACME_TEAM")
    assert "<https://example.com/notice|Retirement notice> · " in sent[0]["text"]
    assert ("<@U_ACME_ADA>" in sent[0]["text"]) == (not personal)
    labels = [e["text"]["text"] for e in sent[0]["blocks"][1]["elements"]]
    assert labels == ["Make a task", "Not useful"]
    # The next scout can list this finding, but cannot save the same key.
    async with ctx.pool.connection() as conn:
        await scout.queue(ctx, conn, person_key="ada" if personal else None)
    fake_brain.results.clear()
    await tick(ctx)
    result = read_result(fake_brain.results[-1])
    assert result["decision"] == "deny" and "State: sent" in result["reason"]
    assert len(posts(slack_client)) == 1


@pytest.mark.parametrize("choice", ["task", "dismiss"])
async def test_buttons(app, fake_brain, slack_client, choice):
    ctx = app.state.ctx
    row = await finding(ctx, fake_brain)
    async with ctx.pool.connection() as conn:
        person = await one(conn, "SELECT * FROM people WHERE key='ben'")
    fake_brain.script = [
        {"name": "task_create", "arguments": {"title": "Check the API version", "list": "launches"}}
    ]
    await findings.action(
        ctx,
        person,
        {
            "action_id": f"scout:{row['id']}:{choice}",
            "channel": row["slack_channel"],
            "message_ts": row["slack_ts"],
        },
    )
    async with ctx.pool.connection() as conn:
        saved = await one(conn, "SELECT * FROM scout_findings WHERE id=%s", (row["id"],))
        tasks = await one(conn, "SELECT count(*) AS n FROM tasks")
        event = await one(conn, "SELECT * FROM events WHERE channel='slack'")
    assert saved["state"] == ("task" if choice == "task" else "dismissed")
    assert saved["decided_by"] == person["id"] and saved["decided_at"]
    assert tasks["n"] == (1 if choice == "task" else 0)
    if choice == "task":
        assert event["payload"]["text"].startswith(
            "Make a task from this finding: Check the API version."
        )
        assert fake_brain.requests[-1].deep is False
    updates = [c for c in slack_client.calls if c["method"] == "chat_update"]
    assert updates[0]["blocks"] and len(updates[0]["blocks"]) == 1


async def test_expiry_and_private_button_owner(app, fake_brain, slack_client):
    ctx = app.state.ctx
    row = await finding(ctx, fake_brain, personal=True)
    async with ctx.pool.connection() as conn:
        ben = await one(conn, "SELECT * FROM people WHERE key='ben'")
        ada = await one(conn, "SELECT * FROM people WHERE key='ada'")
    action = {
        "action_id": f"scout:{row['id']}:dismiss",
        "channel": row["slack_channel"],
        "message_ts": row["slack_ts"],
    }
    await findings.action(ctx, ben, action)
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT state FROM scout_findings"))["state"] == "sent"
        await conn.execute("UPDATE scout_findings SET created_at=now()-interval '15 days'")
    await findings.action(ctx, ada, action)
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT state FROM scout_findings"))["state"] == "sent"
    assert any(
        c.get("text", "").startswith("These buttons expired after 14 days")
        for c in slack_client.calls
    )


async def test_manual_start_owner_only(app):
    ctx = app.state.ctx

    def event(sender, text="Run the Scout now!"):
        return InboundEvent(
            channel="slack", kind="dm", external_id=sender, sender=sender, text=text
        )

    async with ctx.pool.connection() as conn:
        ada = await one(conn, "SELECT * FROM people WHERE key='ada'")
        ben = await one(conn, "SELECT * FROM people WHERE key='ben'")
    answer = await scout.manual(
        ctx, event(ada["slack_id"]), Authority("person:ada", "person_dm", ada["id"])
    )
    assert answer.startswith("The scout starts now. Findings come to ")
    assert (
        await scout.manual(
            ctx, event(ben["slack_id"]), Authority("person:ben", "person_dm", ben["id"])
        )
        == "Only an owner can start the team scout."
    )
    assert (
        await scout.manual(
            ctx,
            event(ada["slack_id"], "Please run the scout"),
            Authority("person:ada", "person_dm", ada["id"]),
        )
        is None
    )
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM deep_runs"))["n"] == 1


async def test_cli_queues_same_run(app, capsys):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        url = make_conninfo(
            os.getenv("ILLO_TEST_DATABASE_URL", "postgresql://illo:illo@127.0.0.1:55432/postgres"),
            dbname=conn.info.dbname,
        )
    config = ctx.config.model_copy(update={"database_url": url})
    args = parser().parse_args(["scout", "run", "--person", "ada"])
    assert await execute(args, config) == 0
    deep_id = int(capsys.readouterr().out.strip())
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM deep_runs WHERE id=%s", (deep_id,))
    assert row["principal"] == "person:ada" and row["state"] == "queued"


async def test_saved_personal_findings_block_later_web(app, fake_brain, monkeypatch):
    ctx = app.state.ctx
    await finding(ctx, fake_brain, personal=True)
    async with ctx.pool.connection() as conn:
        await scout.queue(ctx, conn, person_key="ada")

    async def must_not_fetch(*args, **kwargs):
        pytest.fail("Private findings must not reach a web request")

    monkeypatch.setattr("illo.tools.deep.fetch", must_not_fetch)
    fake_brain.results.clear()
    fake_brain.script = [
        {"name": "findings_list", "arguments": {}},
        {"name": "web_fetch", "arguments": {"url": "https://example.com"}},
    ]
    await tick(ctx)
    result = read_result(fake_brain.results[-1])
    assert result["decision"] == "deny"
    assert "read private data" in result["reason"]
