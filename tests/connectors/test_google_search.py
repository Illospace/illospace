"""The real search hook keeps Google credentials bound to the principal."""

import pytest

from illo.connectors import google, store
from illo.ledger import people, runs
from illo.sources import search
from tests.fakes import FakeGoogleClient
from tests.tool_results import read_result


@pytest.fixture
async def searchable_google(google_app, monkeypatch):
    ctx = google_app.state.ctx
    monkeypatch.setattr(search, "_providers", {})
    await google.setup(ctx)
    ctx.google_client = FakeGoogleClient()
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        await store.save(conn, ctx, "google", ada["id"], {"refresh_token": "test-refresh"}, [])
    return ctx


async def test_search_personal_provider_marks_run_and_shared_write_drafts(
    searchable_google, rpc, rpc_token
):
    ctx = searchable_google
    row, token = rpc_token
    response = await rpc(
        token,
        "tools/call",
        {"name": "search_all", "arguments": {"query": "invoice", "kinds": ["mail"]}},
    )
    result = read_result(response.json()["result"])
    assert result["decision"] == "allow"
    assert "google:google:mail:mail-1" in result["result"]
    response = await rpc(
        token,
        "tools/call",
        {
            "name": "slack_post_channel",
            "arguments": {"channel": "C_ACME_TEAM", "text": "A private mail summary"},
        },
    )
    assert response.json()["result"]["structuredContent"]["decision"] == "draft"
    assert ctx.google_client.calls[0]["person_id"] == row["person_id"]
    async with ctx.pool.connection() as conn:
        assert (await runs.get(conn, row["id"]))["personal_read"]


async def test_search_does_not_use_personal_grant_for_another_person_or_team(searchable_google):
    ctx = searchable_google
    for principal, tainted in (("person:ben", False), ("team", False), ("person:ada", True)):
        hits = await search.search(ctx, principal, "invoice", kinds=["mail"], tainted=tainted)
        assert not hits
        assert not hits.personal_read
    assert not ctx.google_client.calls
