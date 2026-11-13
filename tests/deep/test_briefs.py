"""Briefs use read tools in deep mode and retain the old path when it is off."""

import pytest

from illo.db import one
from illo.routines.digest import send_due
from illo.routines.team_digest import send_team_due
from illo.workspace import TeamDigestConfig
from tests.asks.test_digest import NOW, only_ada
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
