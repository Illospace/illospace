"""Search ranks real Postgres text and merges only visible source results."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from illo.db import one
from illo.ledger import items, lanes
from illo.sources import search
from illo.sources.search import Hit


@pytest.fixture(autouse=True)
def isolated_providers(monkeypatch):
    monkeypatch.setattr(search, "_providers", {})


async def test_title_and_recency_rank(app):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        now = datetime.now(UTC)
        for ref, title, body, at in [
            ("saved:body", "Other topic", "orchard", now),
            ("saved:old", "orchard", "", now - timedelta(days=100)),
            ("saved:new", "orchard", "", now),
        ]:
            await items.upsert(conn, source_ref=ref, title=title, body=body, source_at=at)
    hits = await search.search(ctx, "person:ada", "orchard")
    assert [h.source_ref for h in hits] == ["saved:new", "saved:old", "saved:body"]
    assert "orchard" in hits[0].title
    assert hits[0].at


async def test_lane_events_state_link_and_get(app):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        lane = await lanes.upsert(
            conn,
            source="manual",
            external_id="one",
            title="A task",
            state="blocked",
            links=["https://example.com/task"],
        )
        await lanes.add_event(conn, lane["id"], "update", "The orchard needs a fix")
    hits = await search.search(ctx, "team", "orchard")
    assert len(hits) == 1
    assert hits[0].source_ref == f"lane:{lane['id']}"
    assert "blocked" in hits[0].snippet
    assert hits[0].link == "https://example.com/task"
    full = await search.get(ctx, "team", hits[0].source_ref)
    assert full["events"][0]["text"] == "The orchard needs a fix"
    assert not await search.search(ctx, "team", "orchard", kinds=["note"])


async def test_merge_duplicates_and_partial_failure(app):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        await items.upsert(conn, source_ref="saved:one", title="orchard")

    async def duplicate(ctx, principal, query, kinds, limit):
        return [
            Hit("saved:one", "saved", "custom", "orchard"),
            Hit("saved:one", "saved", "custom", "duplicate"),
            Hit("doc:two", "doc", "custom", "Another orchard"),
        ]

    async def broken(*args):
        raise RuntimeError("Do not print private service details")

    search.register_provider("duplicate", duplicate)
    search.register_provider("unavailable", broken)
    hits = await search.search(ctx, "team", "orchard", limit=2)
    assert [h.source_ref for h in hits] == ["saved:one", "doc:two"]
    assert hits.partial and hits.failures == ["unavailable"]
    with pytest.raises(ValueError, match="Duplicate"):
        search.register_provider("duplicate", duplicate)


async def test_provider_timeout_keeps_ledger_results(app, monkeypatch):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        await items.upsert(conn, source_ref="saved:one", title="orchard")

    async def slow(*args):
        await asyncio.sleep(10)

    timeout = asyncio.timeout
    monkeypatch.setattr(search.asyncio, "timeout", lambda seconds: timeout(0.01))
    search.register_provider("slow", slow)
    hits = await search.search(ctx, "team", "orchard")
    assert hits[0].source_ref == "saved:one"
    assert hits.failures == ["slow"]


async def test_personal_provider_skips_team_taint_routine_and_marks_read(app, rpc, rpc_token):
    calls = []

    async def personal(ctx, principal, query, kinds, limit):
        calls.append(principal)
        return [Hit("mail:one", "mail", "mail", "A private orchard")]

    search.register_provider("mail", personal, personal=True)
    ctx = app.state.ctx
    for principal, kwargs in [
        ("team", {}),
        ("person:ada", {"tainted": True}),
        ("person:ada", {"personal_allowed": False}),
    ]:
        assert not await search.search(ctx, principal, "orchard", **kwargs)
    assert not calls
    run, token = rpc_token
    response = await rpc(
        token, "tools/call", {"name": "search_all", "arguments": {"query": "orchard"}}
    )
    result = response.json()["result"]["structuredContent"]
    assert result["decision"] == "allow"
    assert "A private orchard" in result["result"]
    assert "Search again with other words" in result["result"]
    assert calls == ["person:ada"]
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT personal_read FROM runs WHERE id=%s", (run["id"],)))[
            "personal_read"
        ]


async def test_github_scoped_search_and_get(app, github_client):
    ctx = app.state.ctx
    repo = ctx.workspace.config.github.repositories[0]
    github_client.issues = [
        {
            "number": 3,
            "title": "orchard",
            "body": "Fix it",
            "html_url": f"https://github.com/{repo}/issues/3",
        },
        {
            "number": 4,
            "title": "Another team",
            "html_url": "https://github.com/other/repo/issues/4",
        },
    ]
    hits = await search.search(ctx, "team", "orchard")
    assert [h.source_ref for h in hits] == [f"github:{repo}#3"]
    assert f"repo:{repo}" in github_client.calls[0]["query"]
    row = await search.get(ctx, "team", hits[0].source_ref)
    assert row["body"] == "Fix it"
    assert await search.get(ctx, "team", "github:other/repo#4") is None
    github_client.calls.clear()
    await search.search(ctx, "team", "a")
    assert not github_client.calls
    ctx.github_client = None
    assert not await search.search(ctx, "team", "orchard")


async def test_blank_unknown_and_kind_filters(app):
    ctx = app.state.ctx
    assert not await search.search(ctx, "team", "")
    assert not await search.search(ctx, "outsider", "orchard")
    assert await search.get(ctx, "outsider", "saved:one") is None
    assert await search.get(ctx, "team", "bad:ref") is None
    async with ctx.pool.connection() as conn:
        await items.upsert(conn, source_ref="note:one.md", title="orchard", kind="note")
        await items.upsert(conn, source_ref="saved:one", title="orchard")
    hits = await search.search(ctx, "team", "orchard", kinds=["note"])
    assert [h.source_ref for h in hits] == ["note:one.md"]


async def test_personal_read_survives_limit_and_kind_filter(app, rpc, rpc_token):
    async def personal(ctx, principal, query, kinds, limit):
        return [Hit("mail:hidden", "mail", "mail", "A private result")]

    search.register_provider("mail", personal, personal=True)
    run, token = rpc_token
    response = await rpc(
        token,
        "tools/call",
        {"name": "search_all", "arguments": {"query": "orchard", "kinds": ["note"], "limit": 1}},
    )
    assert "A private result" not in response.json()["result"]["structuredContent"]["result"]
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT personal_read FROM runs WHERE id=%s", (run["id"],)))[
            "personal_read"
        ]
