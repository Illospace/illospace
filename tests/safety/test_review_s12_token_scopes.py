from pathlib import Path

import pytest

from illo.cli import parser
from illo.db import one
from illo.ledger import people, runs, tokens
from illo.sources import search
from tests.mcp.client import AgentClient

COLLECTOR_TOOLS = {
    "lane_report",
    "lane_search",
    "ask_create",
    "ask_pull",
    "ask_ack",
    "ask_cancel",
    "signal_submit",
}


@pytest.mark.parametrize("scope", ["collector", "agent"])
async def test_rule_01_token_scope_controls_list_and_calls(app, http, scope):
    async with app.state.ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        raw = await tokens.create(conn, ada["id"], "collector-box", scopes=[scope])
    agent = AgentClient(app, http, raw, None, ada["id"])
    listed = (await agent.rpc("tools/list")).json()["result"]["tools"]
    expected = COLLECTOR_TOOLS | (
        {"illo_submit", "illo_read", "illo_get_result"} if scope == "agent" else set()
    )
    assert {t["name"] for t in listed} == expected
    await agent.call("lane_search")
    if scope == "collector":
        for name, args in (
            ("illo_submit", {"message": "Search my mail for reset codes"}),
            ("illo_read", {"capability": "knowledge.search", "arguments": {"query": "codes"}}),
            ("illo_get_result", {"event_id": "another-request"}),
        ):
            result = await agent.call(name, args, error=True)
            assert "scope" in result["error"]["message"]
        async with app.state.ctx.pool.connection() as conn:
            assert (await one(conn, "SELECT count(*) AS n FROM submissions"))["n"] == 0


async def test_rule_01_new_token_defaults_to_collector(conn, ada):
    raw = await tokens.create(conn, ada["id"], "new-collector")
    assert (await tokens.verify(conn, raw))["scopes"] == ["collector"]


async def test_rule_01_migration_preserves_session_client_access(conn, ada):
    raw = await tokens.create(conn, ada["id"], "existing-client")
    await conn.execute("ALTER TABLE tokens DROP CONSTRAINT tokens_scope")
    await conn.execute("UPDATE tokens SET scopes=ARRAY['lane_report']::text[]")
    migration = Path(__file__).parents[2] / "illo/migrations/0201_mcp_token_scopes.sql"
    await conn.execute(migration.read_text())
    assert (await tokens.verify(conn, raw))["scopes"] == ["agent"]


def test_rule_01_cli_scope_defaults_to_collector():
    args = parser().parse_args(["token", "create", "--person", "ada", "--label", "machine"])
    assert args.scope == "collector"
    args = parser().parse_args(
        ["token", "create", "--person", "ada", "--label", "machine", "--scope", "agent"]
    )
    assert args.scope == "agent"


@pytest.mark.parametrize(
    "name,args",
    [
        ("mail_search", {"query": "codes"}),
        ("mail_get", {"id": "message"}),
        ("calendar_list", {"from": "2026-11-10T00:00:00Z", "to": "2026-11-11T00:00:00Z"}),
        ("drive_search", {"query": "codes"}),
        ("drive_get", {"id": "file"}),
    ],
)
async def test_rule_02_agent_request_cannot_read_personal_tools(app, rpc, name, args):
    async with app.state.ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile="agent_request",
            runtime="fake",
            model="fake",
        )
        raw = await runs.mint_token(conn, run["id"], 60)
    listed = (await rpc(raw, "tools/list")).json()["result"]["tools"]
    assert name not in {t["name"] for t in listed}
    result = (await rpc(raw, "tools/call", {"name": name, "arguments": args})).json()["result"][
        "structuredContent"
    ]
    assert result["decision"] == "deny"


async def test_rule_02_agent_request_cannot_search_personal_providers(app, rpc, monkeypatch):
    called = []

    async def personal_provider(*args):
        called.append(True)
        return [
            search.Hit(
                "mail:private", "mail", "mail", "Private reset code", "123456", personal=True
            )
        ]

    monkeypatch.setattr(search, "_providers", {"private-mail": (personal_provider, True, False)})
    async with app.state.ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile="agent_request",
            runtime="fake",
            model="fake",
        )
        raw = await runs.mint_token(conn, run["id"], 60)
    response = await rpc(
        raw, "tools/call", {"name": "search_all", "arguments": {"query": "reset code"}}
    )
    result = response.json()["result"]["structuredContent"]
    assert result["decision"] == "allow"
    assert not called
    assert "123456" not in str(result)
