"""Bound model inputs on the measured archive shape and retain useful detail."""

import json

import pytest

from illo.brain.server import tool_result
from illo.db import one
from illo.ledger import items, lanes, people, runs
from illo.tools._format import ask, github, hit, lane, short
from illo.tools.registry import MAX_RESULT_CHARS, Tool, ToolContext
from scripts.measure_tool_results import LIST_TOOLS, TYPICAL_ARGS, configure_fakes, measure, seed


async def test_default_read_results_on_live_shape(app, monkeypatch):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    await configure_fakes(ctx)
    async with ctx.pool.connection() as conn:
        await seed(conn)
        assert (await one(conn, "SELECT count(*) n FROM lanes"))["n"] == 1200
        assert (await one(conn, "SELECT count(*) n FROM items WHERE source_ref LIKE 'archive:%'"))["n"] == 4650
        assert (await one(conn, "SELECT count(*) n FROM asks"))["n"] == 30
    async def page(*args, **kwargs):
        return "<public-page>Outside data.</public-page>"
    monkeypatch.setattr("illo.tools.web.fetch_page", page)
    measured = await measure(ctx)
    for name, result in measured.items():
        assert result["decision"] == "allow", name
    for name in LIST_TOOLS:
        assert measured[name]["characters"] <= MAX_RESULT_CHARS, (name, measured[name])
    # The largest normal question paths leave room for prompt and tool declarations.
    assert sum(measured[name]["tokens"] for name in
               ("lanes_search", "lanes_get", "github_get_issue")) < 6000


async def call(ctx, conn, run, name, args=None):
    result = await ctx.registry.call(ToolContext(conn, run, ctx), name, args or {})
    assert result["decision"] == "allow", result
    return result["result"]


async def test_lane_defaults_counts_and_explicit_filters(app):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    async with ctx.pool.connection() as conn:
        await seed(conn)
        ada = await people.get(conn, "ada")
        run = await runs.create(conn, principal="person:ada", person_id=ada["id"],
                                profile="person_dm", runtime="fake", model="fake")
        text = await call(ctx, conn, run, "lanes_search")
        assert "open 765" in text and "done 390" in text
        assert "Showing 15 of" in text
        assert not any(row.startswith(("done ·", "open ·")) for row in text.splitlines())
        for state in ("open", "done"):
            text = await call(ctx, conn, run, "lanes_search", {"state": state})
            assert f"{state} ·" in text
        text = await call(ctx, conn, run, "lanes_search", {"query": "Mobile", "limit": 40})
        assert "Showing 40 of 1200" in text
        text = await call(ctx, conn, run, "lanes_search", {"repo": "missing"})
        assert "Showing 0 of 0" in text
        result = await ctx.registry.call(ToolContext(conn,run,ctx), "lanes_search", {"limit": 41})
        assert result["decision"] == "deny"


async def test_lane_get_last_five_events_and_links(app):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        value = await lanes.upsert(conn, source="t3", external_id="events", title="Catalog",
                                   links=["https://example.com/first", "https://example.com/second"])
        for n in range(8):
            await lanes.add_event(conn, value["id"], "update", f"signal-{n} " + "a"*900)
        run = await runs.create(conn, principal="person:ada", person_id=ada["id"],
                                profile="person_dm", runtime="fake", model="fake")
        text = await call(ctx,conn,run,"lanes_get", {"id": value["id"]})
        assert "Events: showing 5 of 8" in text
        assert "signal-2" not in text and "signal-7" in text
        assert "a"*301 not in text
        assert "https://example.com/second" in text


async def test_search_defaults_and_detail_pages(app):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    async with ctx.pool.connection() as conn:
        await seed(conn)
        await items.upsert(conn, source_ref="saved:long", title="Long record", body="a"*6000+"b"*6000+"c")
        ada = await people.get(conn, "ada")
        run = await runs.create(conn, principal="person:ada", person_id=ada["id"],
                                profile="person_dm", runtime="fake", model="fake")
        text = await call(ctx,conn,run,"search_all", {"query": "mobile app"})
        assert "Showing 8 of" in text
        assert "tsv" not in text and "metadata" not in text
        first = await call(ctx,conn,run,"search_get", {"source_ref": "saved:long"})
        second = await call(ctx,conn,run,"search_get", {"source_ref": "saved:long", "offset": 6000})
        assert "6001 characters omitted" in first and "offset=6000" in first
        assert "b"*100 not in first and "a"*100 not in second
        assert "offset=12000" in second


async def test_github_details_keep_evidence_and_bound_comments(app):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    await configure_fakes(ctx)
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn,"ada")
        run = await runs.create(conn, principal="person:ada", person_id=ada["id"],
                                profile="person_dm", runtime="fake", model="fake")
        for name in ("github_get_issue", "github_get_pull"):
            text = await call(ctx,conn,run,name,TYPICAL_ARGS[name])
            assert "Comments: showing 5 of 20" in text and "characters omitted" in text
            value = json.loads(text)
            assert value["type"] == "outside_data" and value["trusted"] is False
            assert value["data"].count("API evidence and decision.") < 110
        assert "APPROVED" in text and "success" in text and '"merged":false' in json.loads(text)["data"]


@pytest.mark.parametrize("text", ["a"*50_000, '"\\\n😀'*15_000])
async def test_registry_caps_future_tools_including_json_escapes(app, text):
    ctx = app.state.ctx
    async def huge(ctx, args):
        return {"type": "outside_data", "trusted": False, "data": text,
                "visibility": "private"}
    ctx.registry.add(Tool("huge", "test", "read", lambda a: "ledger", False, huge,
                          profiles=frozenset({"person_dm"})))
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn,"ada")
        run = await runs.create(conn, principal="person:ada", person_id=ada["id"],
                                profile="person_dm", runtime="fake", model="fake")
        result = await ctx.registry.call(ToolContext(conn,run,ctx),"huge",{})
        envelope = tool_result(result, brain=True)
        assert len(envelope["content"][0]["text"]) <= MAX_RESULT_CHARS
        assert result["result"].endswith("Result cut at 12000 characters. Ask for less, or for one item.")
        assert '"type":"outside_data"' in result["result"]
        assert (await runs.get(conn,run["id"]))["personal_read"]
        assert "result" not in envelope["structuredContent"]


async def test_format_limits():
    from illo.sources.search import Hit
    assert len(short("x"*200,90)) == 90
    assert "x"*91 not in lane({"state":"working", "title":"x"*100, "id":1, "source":"t3"})
    assert "x"*161 not in ask({"kind":"review", "question":"x"*200, "id":1})
    assert "x"*221 not in hit(Hit("ref", "doc", "docs", "title", "x"*300))
    assert "x"*101 not in github({"number":1, "title":"x"*200})


async def test_mail_offset_fetches_rows_before_the_page(app):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    await configure_fakes(ctx)
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(conn, principal="person:ada", person_id=ada["id"],
                                profile="person_dm", runtime="fake", model="fake")
        result = await call(ctx, conn, run, "mail_search",
                            {"query": "catalog", "offset": 10, "limit": 10})
        assert "Showing 10 of 20" in result["data"]
        assert "example-10" in result["data"] and "example-19" in result["data"]
        assert "example-0" not in result["data"]
        assert ctx.google_client.calls[-1]["arguments"] == {"query": "catalog", "limit": 20}
        assert result["type"] == "outside_data" and result["trusted"] is False


@pytest.mark.parametrize("link,expected_count", [
    ("https://example.com/thread/1", 1),
    ("https://github.com/acme/api/issues/1", 1),
])
async def test_g2_lane_row_shows_the_repository_without_a_detail_call(app, link, expected_count):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        await lanes.upsert(conn, source="t3", external_id="repo-row", title="Release work",
                           state="working", repo="acme/api", links=[link])
        values, _, _ = await lanes.compact_search(conn, principal="team")
    assert values[0]["repo"] == "acme/api"
    text = lane(values[0])
    assert text.count("acme/api") == expected_count
    assert link in text and len(text) < 240
