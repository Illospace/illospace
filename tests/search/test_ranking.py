"""Use invented archive text to measure recent-message recall with a fixed clock."""

from datetime import UTC, datetime, timedelta

import pytest

from illo.ledger import items, lanes, people
from illo.sources import search

NOW = datetime(2026, 8, 20, 18, tzinfo=UTC)
MESSAGE_REF = "slack:C_ACME_ALERTS:1787248740.000001"
MESSAGE = (
    "Hello, I am mira.orbit@example.com. I use Nebula Pro for our product photos. "
    "Why is the price so high? Do credits expire after a month? "
    "We need to compare the cost before we place the next order."
)
QUERIES = ["Nebula Pro pricing", "Nebula Pro pricing today", "mira", "orbit",
           "Cobalt batching protocol"]


@pytest.fixture
async def ranking_corpus(app, monkeypatch):
    ctx = app.state.ctx
    monkeypatch.setattr(search, "utc_now", lambda: NOW, raising=False)
    monkeypatch.setattr(search, "_providers", {})
    old = NOW - timedelta(days=140)
    async with ctx.pool.connection() as conn:
        for index in range(300):
            title, body = [
                ("Nebula Pro pricing review", "The price uses credits for each photo."),
                ("Product photo costs", "Nebula Pro pricing and credit estimates for the next release."),
                ("Nebula model comparison", "Compare Nebula Pro with the basic product and its price."),
            ][index % 3]
            await items.upsert(conn, source_ref=f"archive:sample-{index:03}", kind="archive",
                               title=title, body=body, source_at=old-timedelta(days=index % 30))
        for index in range(20):
            lane = await lanes.upsert(conn, source="manual", external_id=f"sample-{index}",
                                      title="Nebula Pro pricing investigation", summary="Check credits.")
            await conn.execute("UPDATE lanes SET updated_at=%s WHERE id=%s", (old, lane["id"]))
        await items.upsert(conn, source_ref=MESSAGE_REF, kind="slack", title="#alerts — U_ACME_GUEST",
                           body=MESSAGE, source_at=NOW-timedelta(minutes=1))
        await items.upsert(conn, source_ref="decision:cobalt", kind="decision",
                           title="Cobalt batching protocol", body="Use four photos in each batch.",
                           source_at=NOW-timedelta(days=400))
        ada = await people.get(conn, "ada")
        await items.upsert(conn, source_ref="saved:private-pricing", visibility="private",
                           owner_person_id=ada["id"], title="Nebula Pro pricing mira orbit",
                           body=MESSAGE, source_at=NOW)
    repo = ctx.workspace.config.github.repositories[0]
    ctx.github_client.issues = [
        {"number": index + 1, "title": "Nebula Pro pricing", "body": "Check price and credits.",
         "html_url": f"https://github.com/{repo}/issues/{index+1}", "updated_at": old.isoformat()}
        for index in range(20)
    ]
    return ctx


async def test_recent_message_beats_archive_and_old_decision_stays_findable(ranking_corpus):
    ctx = ranking_corpus
    for query in QUERIES:
        hits = await search.search(ctx, "person:ben", query, limit=50)
        refs = [h.source_ref for h in hits]
        expected = "decision:cobalt" if query == QUERIES[-1] else MESSAGE_REF
        position = refs.index(expected) + 1 if expected in refs else None
        print(f"{query}: {position or 'absent'} / {len(hits)}")
        assert position is not None and position <= 3
        assert "saved:private-pricing" not in refs
    for limit in (1, 3, 10):
        hits = await search.search(ctx, "team", "Nebula Pro pricing today", limit=limit)
        assert hits[0].source_ref == MESSAGE_REF
    async with ctx.pool.connection() as conn:
        assert await items.get(conn, "saved:private-pricing", principal="person:ben") is None


@pytest.mark.parametrize("query", [
    "mira.orbit@example.com", "mira.orbit", "example", "com", "orbit@example.com",
])
async def test_email_whole_and_parts(ranking_corpus, query):
    hits = await search.search(ranking_corpus, "team", query, kinds=["slack"])
    assert [h.source_ref for h in hits] == [MESSAGE_REF]


@pytest.mark.parametrize(("identifier", "parts"), [
    ("delta.company", ["delta", "company"]),
    ("output_image_unreadable", ["output", "image", "unreadable"]),
    ("acme-backend", ["acme", "backend"]),
    ("ABC-123", ["ABC", "123"]),
])
async def test_identifiers_match_whole_and_parts_in_items_lanes_and_events(app, identifier, parts):
    from tests.search.test_slack_events import event

    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        await items.upsert(conn, source_ref="saved:identifier", title=identifier)
        lane = await lanes.upsert(conn, source="manual", external_id="identifier", title=identifier)
        row = await event(conn, text=identifier)
    expected = {"saved:identifier", f"lane:{lane['id']}", f"slack-event:{row['id']}"}
    for query in [identifier, *parts]:
        hits = await search.search(ctx, "team", query, kinds=["saved", "lane", "slack"])
        assert {h.source_ref for h in hits} == expected
        async with ctx.pool.connection() as conn:
            assert [r["source_ref"] for r in await items.search(conn, query, kinds=["saved"])] == ["saved:identifier"]


async def test_recent_inbound_event_and_live_sources_share_rank_scale(ranking_corpus):
    from tests.search.test_slack_events import event

    ctx = ranking_corpus
    async with ctx.pool.connection() as conn:
        await conn.execute("DELETE FROM items WHERE source_ref=%s", (MESSAGE_REF,))
        row = await event(conn, text=MESSAGE)
        await conn.execute("UPDATE events SET created_at=%s WHERE id=%s", (NOW, row["id"]))
    hits = await search.search(ctx, "team", "Nebula Pro pricing today", limit=3)
    assert hits[0].source_ref == f"slack-event:{row['id']}"
    repo = ctx.workspace.config.github.repositories[0]
    ctx.github_client.issues.insert(0, {
        "number": 100, "title": "Nebula Pro pricing", "body": MESSAGE,
        "html_url": f"https://github.com/{repo}/issues/100", "updated_at": NOW.isoformat(),
    })
    hits = await search.search(ctx, "team", "Nebula Pro pricing", limit=3)
    assert hits[0].source_ref == f"github:{repo}#100"
    assert hits[0].lexical > 0 and hits[0].recency == 1
    assert f"slack-event:{row['id']}" in {h.source_ref for h in hits}


async def test_knowledge_search_exposes_scores_and_keeps_legacy_fields(ranking_corpus, app, http):
    from tests.mcp.client import AgentClient

    agent = await AgentClient.create(app, http, person="ben")
    result = await agent.call("illo_read", {"capability": "knowledge.search",
                                          "arguments": {"query": "Nebula Pro pricing today"}})
    hit = result["results"][0]
    assert hit["source_ref"] == MESSAGE_REF
    assert {"title", "snippet", "link", "at", "score", "match_tier"} <= hit.keys()
    assert 0 < hit["scores"]["lexical"] < 1
    assert 0.99 < hit["scores"]["recency"] <= 1
    assert hit["score"] > hit["scores"]["rrf"] > 0
    assert result["semantic_available"] is False


async def test_recency_floor_half_life_and_future_clock(app, monkeypatch):
    ctx = app.state.ctx
    monkeypatch.setattr(search, "utc_now", lambda: NOW)
    async with ctx.pool.connection() as conn:
        for ref, age in [("future", -2), ("now", 0), ("half", 21), ("old", 2100)]:
            await items.upsert(conn, source_ref=f"saved:{ref}", title="Quartz",
                               source_at=NOW-timedelta(days=age))
    hits = await search.search(ctx, "team", "Quartz", kinds=["saved"])
    scores = {h.source_ref: h.recency for h in hits}
    assert scores == {"saved:future": 1, "saved:now": 1, "saved:half": 0.6, "saved:old": 0.2}
    assert len(hits) == 4
    assert not await search.search(ctx, "team", "today")
    assert not await search.search(ctx, "team", "yesterday this week")


async def test_recent_personal_provider_competes_with_archive_at_limit_one(ranking_corpus):
    ctx = ranking_corpus

    async def mail(ctx, principal, query, kinds, limit):
        return [search.Hit("mail:recent", "mail", "mail", "Nebula Pro pricing", MESSAGE,
                           at=NOW.isoformat())]

    search.register_provider("mail", mail, personal=True)
    hits = await search.search(ctx, "person:ben", "Nebula Pro pricing today", limit=1)
    assert [h.source_ref for h in hits] == ["mail:recent"]
    assert hits.personal_read and hits[0].personal
    for principal, options in [("team", {}), ("person:ben", {"personal_allowed": False})]:
        hits = await search.search(ctx, principal, "Nebula Pro pricing today", **options)
        assert "mail:recent" not in {h.source_ref for h in hits}


async def test_live_source_candidates_do_not_shrink_with_result_limit(ranking_corpus):
    ctx = ranking_corpus
    repo = ctx.workspace.config.github.repositories[0]
    ctx.github_client.issues.append({
        "number": 100, "title": "Nebula Pro pricing", "body": MESSAGE,
        "html_url": f"https://github.com/{repo}/issues/100", "updated_at": NOW.isoformat(),
    })
    for limit in (1, 3, 50):
        hits = await search.search(ctx, "team", "Nebula Pro pricing today", limit=limit)
        assert hits[0].source_ref == f"github:{repo}#100"


@pytest.mark.parametrize("query", ["today@example.com", "yesterday.report", "this-week", '"today"'])
async def test_time_words_inside_identifiers_and_quotes_stay_searchable(app, query):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        await items.upsert(conn, source_ref="saved:time-token", title=query.strip('"'))
    hits = await search.search(ctx, "team", query, kinds=["saved"])
    assert [h.source_ref for h in hits] == ["saved:time-token"]
