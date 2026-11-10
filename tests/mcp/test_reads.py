from illo.channels.mcp import reads
from tests.mcp.client import AgentClient


async def test_ledger_fallback_and_search_filters(app, http, monkeypatch):
    agent = await AgentClient.create(app, http)
    monkeypatch.setattr(reads, "search", None)
    await agent.call(
        "illo_submit", {"message": "The cobalt decision.", "desired_outcome": "preserve_knowledge"}
    )
    await agent.call(
        "lane_report", {"source": "t3", "external_id": "acme-1", "title": "Cobalt checks"}
    )
    args = {"query": "cobalt", "kinds": ["saved"], "sources": ["saved"]}
    result = await agent.call("illo_read", {"capability": "knowledge.search", "arguments": args})
    assert len(result["results"]) == 1 and result["results"][0]["kind"] == "saved"
    args["sources"] = ["github"]
    assert (await agent.call("illo_read", {"capability": "knowledge.search", "arguments": args}))[
        "results"
    ] == []


async def test_shared_search_receives_the_authenticated_principal(app, http, monkeypatch):
    agent = await AgentClient.create(app, http)
    seen = []

    async def search(principal, query, kinds, limit):
        seen.append((principal, query, kinds, limit))
        return []

    monkeypatch.setattr(reads, "search", search)
    monkeypatch.setattr(reads, "SEARCH_USES_CONTEXT", False)
    await agent.call(
        "illo_read", {"capability": "knowledge.search", "arguments": {"query": "cobalt"}}
    )
    assert seen == [("person:ada", "cobalt", None, 50)]


async def test_shared_context_search_does_not_open_personal_connectors(app, http, monkeypatch):
    agent = await AgentClient.create(app, http)
    seen = []

    async def search(ctx, principal, query, kinds, limit, *, conn, personal_allowed):
        seen.append((ctx, principal, conn, personal_allowed))
        return []

    monkeypatch.setattr(reads, "search", search)
    monkeypatch.setattr(reads, "SEARCH_USES_CONTEXT", True)
    await agent.call(
        "illo_read", {"capability": "knowledge.search", "arguments": {"query": "cobalt"}}
    )
    assert seen[0][0] is app.state.ctx and seen[0][1] == "person:ada" and seen[0][3] is False
