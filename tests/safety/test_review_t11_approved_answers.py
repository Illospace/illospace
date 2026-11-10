"""Rules 5 and 8: approved answers keep their source and cannot send twice."""

import httpx
import pytest

from collectors.common import MCPClient
from collectors.t3.collector import Collector, Config, T3Source
from illo.asks import drafts
from illo.db import all_rows, one
from illo.ledger import people
from tests.collectors.test_t3 import APIs
from tests.mcp.client import AgentClient


async def test_rule_08_second_approved_answer_fails(app, http, rpc, rpc_token):
    ctx = app.state.ctx
    agent = await AgentClient.create(app, http)
    ask = await agent.call(
        "ask_create",
        {
            "question": "Which check first?",
            "source_ref": "duplicate-drafts",
            "options": [{"id": "tests", "label": "Tests"}, {"id": "lint", "label": "Lint"}],
        },
    )
    for option in ("tests", "lint"):
        response = await rpc(
            rpc_token[1],
            "tools/call",
            {
                "name": "asks_answer",
                "arguments": {"id": ask["ask_id"], "option": option},
            },
        )
        assert response.json()["result"]["structuredContent"]["decision"] == "draft"
    await drafts.tick(ctx)
    async with ctx.pool.connection() as conn:
        rows = await all_rows(conn, "SELECT * FROM drafts ORDER BY id")
        ada = await people.get(conn, "ada")
    for row in rows:
        await drafts.handle(
            ctx,
            ada,
            {
                "action_id": f"draft:{row['id']}:approve",
                "channel": row["slack_channel"],
                "message_ts": row["slack_ts"],
            },
        )
    async with ctx.pool.connection() as conn:
        states = await all_rows(conn, "SELECT state,send_error FROM drafts ORDER BY id")
        sensor = await one(conn, "SELECT * FROM sensors WHERE key=%s", (f"draft:{rows[1]['id']}",))
    assert [r["state"] for r in states] == ["sent", "approved"]
    assert states[1]["send_error"]
    assert sensor["last_error_at"]
    answer = (await agent.call("ask_pull"))["answers"][0]
    assert answer["answered_via"] == "approved_draft"
    assert answer["answer_option"]["id"] == "tests"


@pytest.mark.parametrize("free_text", [False, True])
def test_rule_08_collector_names_approved_model_answer(tmp_path, free_text):
    api = APIs()
    api.wait()
    token = tmp_path / "token"
    token.write_text("fake-token")
    token.chmod(0o600)
    config = Config(
        "https://illo.example/mcp",
        token,
        t3_url="https://t3.example",
        t3_token_file=token,
        state_file=tmp_path / "state.json",
    )
    collector = Collector(
        config,
        T3Source(config, httpx.Client(transport=httpx.MockTransport(api.t3))),
        MCPClient(config.mcp_url, "fake", httpx.Client(transport=httpx.MockTransport(api.illo))),
    )
    collector.poll()
    api.answer()
    api.answers[0]["answered_via"] = "approved_draft"
    if free_text:
        api.answers[0]["answer_option"] = None
    collector.poll()
    assert api.sent[0]["arguments"]["answers"]["question-a"].startswith(
        "Answer approved by Sam (via Illo):"
    )
