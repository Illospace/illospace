"""Rule 8: a model answer needs the ask owner's explicit approval."""

import httpx
import pytest

from collectors.common import MCPClient
from collectors.t3.collector import Collector, Config, T3Source
from illo.asks import drafts
from illo.db import one
from illo.ledger import asks, people
from tests.collectors.test_t3 import APIs
from tests.mcp.client import AgentClient
from tests.tool_results import read_result


@pytest.mark.parametrize("personal_read", [False, True])
async def test_rule_08_model_answer_waits_for_owner_approval(
    app, http, rpc, rpc_token, slack_client, personal_read
):
    ctx = app.state.ctx
    agent = await AgentClient.create(app, http)
    created = await agent.call(
        "ask_create",
        {
            "question": "Which test target should I use?",
            "source_ref": "q-1",
            "options": [{"id": "preview", "label": "Preview"}, {"id": "local", "label": "Local"}],
        },
    )
    run, token = rpc_token
    text = "Run the local checks. " * 200 + "Final condition: inspect the report."
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET personal_read=%s WHERE id=%s", (personal_read, run["id"])
        )
    response = await rpc(
        token,
        "tools/call",
        {
            "name": "asks_answer",
            "arguments": {
                "id": created["ask_id"],
                "option": "preview",
                "text": text,
            },
        },
    )
    result = read_result(response.json()["result"])
    assert result["decision"] == "draft"
    assert (await agent.call("ask_pull"))["answers"] == []
    async with ctx.pool.connection() as conn:
        row = await asks.get(conn, created["ask_id"], principal="person:ada")
        assert row["state"] == "open" and row["answered_via"] is None
    await drafts.tick(ctx)
    async with ctx.pool.connection() as conn:
        draft = await one(conn, "SELECT * FROM drafts WHERE id=%s", (result["result"]["draft_id"],))
        ada, ben = await people.get(conn, "ada"), await people.get(conn, "ben")
    card = next(
        c
        for c in slack_client.calls
        if c.get("ts") == draft["slack_ts"]
        or (c.get("method") == "chat_postMessage" and "Draft for" in c.get("text", ""))
    )
    visible = "".join(
        b["text"]["text"][2:]
        for b in card["blocks"]
        if b["type"] == "section" and b["text"]["text"].startswith("> ")
    )
    assert "Which test target should I use?" in visible and "Preview" in visible
    assert text in visible
    click = {
        "action_id": f"draft:{draft['id']}:approve",
        "channel": draft["slack_channel"],
        "message_ts": draft["slack_ts"],
    }
    await drafts.handle(ctx, ben, click)
    assert (await agent.call("ask_pull"))["answers"] == []
    await drafts.handle(ctx, ada, {**click, "channel": "D_OTHER"})
    assert (await agent.call("ask_pull"))["answers"] == []
    await drafts.handle(ctx, ada, click)
    answer = (await agent.call("ask_pull"))["answers"][0]
    assert answer["answered_via"] == "approved_draft" and answer["answer_text"] == text


async def test_rule_08_model_cannot_draft_another_owners_answer(app, rpc, rpc_token):
    async with app.state.ctx.pool.connection() as conn:
        ben = await people.get(conn, "ben")
        ask = await asks.create(conn, owner_person_id=ben["id"], question="Proceed?")
    response = await rpc(
        rpc_token[1],
        "tools/call",
        {"name": "asks_answer", "arguments": {"id": ask["id"], "text": "Proceed"}},
    )
    assert response.json()["result"]["structuredContent"]["decision"] == "deny"
    async with app.state.ctx.pool.connection() as conn:
        assert await one(conn, "SELECT * FROM drafts") is None


def test_rule_08_collector_names_the_actual_answer_source(tmp_path):
    api = APIs()
    api.wait()
    local_token = tmp_path / "token"
    local_token.write_text("acme-token")
    local_token.chmod(0o600)
    config = Config(
        "https://illo.example/mcp",
        tmp_path / "token",
        t3_url="https://t3.example",
        t3_token_file=local_token,
        state_file=tmp_path / "state.json",
    )
    collector = Collector(
        config,
        T3Source(config, httpx.Client(transport=httpx.MockTransport(api.t3))),
        MCPClient(config.mcp_url, "fake", httpx.Client(transport=httpx.MockTransport(api.illo))),
    )
    collector.poll()
    api.answer()
    api.answers[0].update(answered_via="rule", answer_text=None, rule_text="Run tests first.")
    collector.poll()
    message = api.sent[0]["arguments"]["answers"]["question-a"]
    assert "Answer by rule" in message and "Answer from Sam" not in message
