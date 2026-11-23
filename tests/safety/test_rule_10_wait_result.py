"""Rule 10: only a code-started wait check gets its bounded result tool."""

import pytest

from illo.db import one
from illo.deep import start
from illo.gateway.principal import Authority
from illo.ledger import runs
from illo.tools.registry import Refusal, ToolContext


@pytest.mark.parametrize("profile,principal,kind,tainted", [
    ("person_dm", "person:ada", None, False),
    ("team_channel", "team", None, False),
    ("routine", "team", None, False),
    ("agent_request", "person:ada", None, False),
    ("routine", "team", "scout", False),
    ("person_dm", "person:ada", "request", False),
    ("routine", "team", "brief", False),
    ("tainted", "team", None, True),
    ("tainted", "outsider", None, False),
    ("routine", "team", "check", True),
    ("tainted", "outsider", "check", False),
])
async def test_wait_result_refused_by_list_and_direct_call(app, profile, principal, kind, tainted):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        person_id = (await one(conn, "SELECT id FROM people WHERE key='ada'"))["id"] if principal.startswith("person:") else None
        run = await runs.create(conn, principal=principal, person_id=person_id, profile=profile, tainted=tainted,
                                provider="fake", tool_limit=12, model="fake")
        await conn.execute("UPDATE runs SET deep_kind=%s WHERE id=%s", (kind, run["id"]))
        run["deep_kind"] = kind
        assert "wait_result" not in {t["name"] for t in ctx.registry.list_tools(run)}
        result = await ctx.registry.call(ToolContext(conn, run, ctx), "wait_result",
                                        {"outcome": "complete", "message": "The release passed."})
        assert result["decision"] == "deny"
        assert (await one(conn, "SELECT count(*) AS n FROM task_events"))["n"] == 0


@pytest.mark.parametrize("kind", ["request", "scout", "brief", "check"])
async def test_commit_read_in_every_deep_kind(app, kind):
    ctx = app.state.ctx
    run = {"principal": "team", "profile": "routine", "tainted": False,
           "personal_read": False, "deep_kind": kind}
    names = {t["name"] for t in ctx.registry.list_tools(run)}
    assert "github_get_commit" in names
    if kind == "check":
        assert "wait_result" in names
        assert not {"findings_list", "finding_add", "task_return", "task_done", "deep_start"} & names
        writes = {t.name for t in ctx.registry.tools.values() if t.name in names and t.risk != "read"}
        assert writes == {"wait_result"}


@pytest.mark.parametrize("authority,deliver", [
    (Authority("person:ada", "person_dm", 1), {"none": True}),
    (Authority("team", "team_channel"), {"none": True}),
    (Authority("team", "routine"), {"slack_channel": {"channel_id": "C_ACME_TEAM"}}),
])
async def test_only_code_starts_check_with_no_delivery(app, authority, deliver):
    async with app.state.ctx.pool.connection() as conn:
        with pytest.raises(Refusal, match="Only code"):
            await start(app.state.ctx, conn, kind="check", job="Read the task.",
                        authority=authority, deliver=deliver)
