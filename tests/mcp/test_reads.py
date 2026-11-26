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


async def test_capabilities_self_map_returns_watched_playbooks_and_respects_visibility(app, http):
    from illo.workspace import SlackChannel, TaskList

    ws = app.state.ctx.workspace.model_copy(deep=True)
    ws.config.slack.channels.append(
        SlackChannel(id="C_SECRET_PRIVATE", name="exec-private", mode="listen", visibility="private")
    )
    ws.lists.append(TaskList(key="ada-private", name="Ada private", owner="ada"))
    ws.lists.append(TaskList(key="ben-private", name="Ben private", owner="ben"))
    app.state.ctx.workspace = ws

    ada = await AgentClient.create(app, http)
    caps = await ada.call("illo_read", {"capability": "capabilities"})

    channel_ids = {c["id"] for c in caps["watched"]["slack_channels"]}
    assert "C_ACME_ALERTS" in channel_ids
    assert "C_SECRET_PRIVATE" not in channel_ids
    assert caps["watched"]["github_repositories"] == ws.config.github.repositories

    list_keys = {entry["key"] for entry in caps["playbooks"]["lists"]}
    assert "ada-private" in list_keys
    assert "ben-private" not in list_keys
    assert "alerts_and_slack" in caps["how_illo_works"]
    assert "teaching_memory" in caps["how_illo_works"]
    assert "illo_submit" in caps["mcp_tools"] and "illo_read" in caps["mcp_tools"]
