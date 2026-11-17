"""Briefs use read tools in deep mode and retain the old path when it is off."""

import pytest

from illo.db import one
from illo.routines.digest import send_due
from illo.routines.team_digest import send_team_due
from illo.workspace import TeamDigestConfig
from tests.asks.test_digest import NOW, only_ada, posts
from tests.asks.test_team_digest import seed_team
from tests.tool_results import read_result


@pytest.mark.parametrize("deep", [True, False])
@pytest.mark.parametrize("team", [True, False])
async def test_brief_modes(app, fake_brain, deep, team):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.digest.deep = deep
    if team:
        ctx.workspace.config.timezone = "UTC"
        ctx.workspace.config.digest.team = TeamDigestConfig(channel="C_ACME_TEAM")
        await seed_team(ctx)
    else:
        await only_ada(ctx)
    fake_brain.script = [
        {"name": "team_snapshot" if team else "digest_snapshot", "arguments": {}},
        {"name": "task_list", "arguments": {}},
        {"name": "web_fetch", "arguments": {"url": "https://example.com"}},
    ]
    await (send_team_due(ctx, NOW) if team else send_due(ctx, NOW))
    assert len(fake_brain.requests) == 1
    request = fake_brain.requests[0]
    assert request.deep is deep and not request.web_search
    results = [read_result(r) for r in fake_brain.results]
    assert [r["decision"] for r in results] == ["allow", "allow" if deep else "deny", "deny"]
    async with ctx.pool.connection() as conn:
        count = await one(conn, "SELECT count(*) AS n FROM deep_runs")
    assert count["n"] == (1 if deep else 0)



async def test_team_brief_holds_no_lock_during_the_run(app, fake_brain, slack_client, monkeypatch):
    """A tool of the brief run takes the channel lock and the brief lock on its own connection."""
    import asyncio

    from illo.channels.slack.access import channel_private
    from illo.ledger import ask_workflow
    from illo.routines import team_digest

    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.timezone = "UTC"
    ctx.workspace.config.digest.team = TeamDigestConfig(channel="C_ACME_TEAM")
    await seed_team(ctx)
    runs = []

    async def run(ctx, *args, channel_id=None, **kwargs):
        runs.append(channel_id)
        async with asyncio.timeout(5):
            await channel_private(ctx, channel_id)
            async with ctx.pool.connection() as conn:
                await ask_workflow.lock_digest(conn, team_digest.TEAM_STATE_KEY)
        return "The team brief."

    monkeypatch.setattr(team_digest, "write_brief", run)
    await send_team_due(ctx, NOW)
    assert runs == ["C_ACME_TEAM"]
    assert [call["text"] for call in posts(slack_client)] == ["The team brief."]
    async with ctx.pool.connection() as conn:
        state = await one(conn, "SELECT last_status FROM routine_state WHERE key=%s", (team_digest.TEAM_STATE_KEY,))
    assert state["last_status"] == "ok"


@pytest.mark.parametrize("stopped", [False, True])
async def test_personal_brief_holds_no_lock_during_the_run(app, fake_brain, slack_client, monkeypatch, stopped):
    """A tool of the brief run can change the brief settings of the same person."""
    import asyncio

    from illo.ledger import ask_workflow
    from illo.routines import digest

    ctx = app.state.ctx
    await only_ada(ctx)
    runs = []

    async def run(ctx, person, data, due):
        runs.append(person["key"])
        async with asyncio.timeout(5):
            async with ctx.pool.connection() as conn:
                await ask_workflow.lock_digest(conn, person["key"])
                if stopped:
                    await conn.execute("UPDATE brief_preferences SET enabled=false WHERE person_id=%s", (person["id"],))
        return "The brief."

    monkeypatch.setattr(digest, "brief_text", run)
    await send_due(ctx, NOW)
    assert runs == ["ada"]
    sent = [call["text"] for call in posts(slack_client)]
    async with ctx.pool.connection() as conn:
        last = await ask_workflow.digest_last_sent(conn, "ada")
    if stopped:
        # The person stopped the briefs during the run. Illo sends nothing.
        assert not sent and not last
    else:
        assert len(sent) == 1 and sent[0].startswith("The brief.") and last


async def test_pool_ends_a_transaction_that_stays_idle(app):
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SHOW idle_in_transaction_session_timeout")
    assert row["idle_in_transaction_session_timeout"] == "1h"


def test_pool_keeps_the_options_of_the_address():
    from illo.db import create_pool

    pool = create_pool("postgresql://u:p@127.0.0.1:1/d?options=-csearch_path%3Dacme")
    assert pool.kwargs["options"] == "-csearch_path=acme -c idle_in_transaction_session_timeout=3600000"
