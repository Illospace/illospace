"""Rule 3: compact rows and counts retain private-read destination limits."""

import pytest

from illo.db import one
from illo.ledger import asks, lanes, people, runs
from illo.tools.registry import ToolContext


@pytest.mark.parametrize("name", ["lanes_search", "lanes_mine", "lanes_attention",
                                  "asks_list", "asks_digest"])
async def test_rule_03_compact_private_read_marks_run(app, name):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn,"ada")
        ben = await people.get(conn,"ben")
        for owner in (ada, ben):
            lane = await lanes.upsert(conn, source="t3", external_id=owner["key"],
                title=owner["key"]+" private task", state="waiting_person",
                owner_person_id=owner["id"], visibility="private")
            await asks.create(conn, owner_person_id=owner["id"], lane_id=lane["id"],
                              question=owner["key"]+" private ask")
        run = await runs.create(conn, principal="person:ada", person_id=ada["id"],
                                profile="person_dm", runtime="fake", model="fake")
        result = await ctx.registry.call(ToolContext(conn,run,ctx),name,{})
        assert result["decision"] == "allow"
        assert "ada private" in result["result"] and "ben private" not in result["result"]
        assert (await runs.get(conn,run["id"]))["personal_read"]
        assert "private" not in result or result["private"] is not True
        write = await ctx.registry.call(ToolContext(conn,run,ctx),"slack_post_channel",
                                       {"channel":"C_ACME_TEAM", "text":"Private text"})
        assert write["decision"] == "draft"


async def test_rule_03_private_counts_beyond_page_mark_run(app):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn,"ada")
        lane = await lanes.upsert(conn, source="t3",external_id="hidden",title="Hidden backlog",
                                 state="open",visibility="private",owner_person_id=ada["id"])
        for n in range(16):
            public = await lanes.upsert(conn, source="t3",external_id=str(n),title="Public work",
                                        state="working",owner_person_id=ada["id"])
            await asks.create(conn,owner_person_id=ada["id"],lane_id=public["id"],question="Public ask")
        await asks.create(conn,owner_person_id=ada["id"],lane_id=lane["id"],question="Hidden private ask")
        for name in ("lanes_search", "asks_list"):
            run = await runs.create(conn,principal="person:ada",person_id=ada["id"],
                                    profile="person_dm",runtime="fake",model="fake")
            result = await ctx.registry.call(ToolContext(conn,run,ctx),name,{})
            assert "Hidden" not in result["result"]
            assert (await one(conn,"SELECT personal_read FROM runs WHERE id=%s",(run["id"],)))["personal_read"]


async def test_rule_03_attention_person_routine_and_team_visibility(app):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn,"ada")
        await lanes.upsert(conn,source="t3",external_id="private",title="Private work",
                           state="working",visibility="private",owner_person_id=ada["id"])
        for principal, profile, person_id, expected in (
            ("team","team_channel",None,"deny"), ("person:ada","agent_request",ada["id"],"deny"),
            ("team","routine",None,"allow"), ("person:ada","routine",ada["id"],"allow")):
            run = await runs.create(conn,principal=principal,person_id=person_id,profile=profile,
                                    runtime="fake",model="fake")
            result = await ctx.registry.call(ToolContext(conn,run,ctx),"lanes_attention",{})
            assert result["decision"] == expected
            assert "Private work" not in str(result)


@pytest.mark.parametrize("principal,profile,name", [
    ("person:ada", "person_dm", name) for name in
    ("lanes_search", "lanes_mine", "lanes_attention", "asks_list", "asks_digest")
] + [("team", "team_channel", name) for name in ("lanes_search", "asks_list")])
async def test_rule_03_hidden_rows_do_not_change_compact_text_or_counts(app, principal, profile, name):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        ben = await people.get(conn, "ben")
        run = await runs.create(conn, principal=principal, profile=profile,
                                person_id=ada["id"] if principal == "person:ada" else None,
                                runtime="fake", model="fake")
        before = await ctx.registry.call(ToolContext(conn, run, ctx), name, {})
        hidden = await lanes.upsert(conn, source="t3", external_id="hidden-owner",
            title="Hidden task", state="blocked", summary="Hidden summary",
            links=["https://example.com/hidden"], owner_person_id=ben["id"], visibility="private")
        await lanes.add_event(conn, hidden["id"], "update", "Hidden event")
        await asks.create(conn, owner_person_id=ben["id"], lane_id=hidden["id"],
                          question="Hidden question", proof={"text": "Hidden proof"})
        after = await ctx.registry.call(ToolContext(conn, run, ctx), name, {})
        assert after["decision"] == before["decision"] == "allow"
        assert after["result"] == before["result"]
        # The digest always marks a personal read, including an empty result.
        assert (await runs.get(conn, run["id"]))["personal_read"] == (name == "asks_digest")
