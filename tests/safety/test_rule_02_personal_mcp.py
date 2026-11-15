"""Rule 2: a personal MCP tool uses only the run owner's grant."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from illo.connectors import mcp_oauth, store
from illo.connectors.config import Connector
from illo.connectors.mcp_client import MCPClient
from illo.db import all_rows, one
from illo.ledger import people, runs
from illo.tools.connectors import call_connector, list_connectors
from illo.tools.registry import ToolContext
from tests.fakes import FakeMCPSession

ARGS = {"connector": "acme-crm", "tool": "read", "arguments": {"query": "plan"}}


@pytest.fixture
async def personal_mcp(app):
    ctx = app.state.ctx
    ctx.config.vault_key = SecretStr(Fernet.generate_key().decode())
    cfg = Connector(
        key="acme-crm",
        kind="mcp",
        scope="personal",
        url="https://mcp.example.com/mcp",
        auth={"type": "oauth", "client_id": "acme-client"},
        tools={
            "read": {"risk": "read"},
            "write": {"risk": "write_internal"},
            "send": {"risk": "send_outside"},
        },
    )
    team = Connector(
        key="acme-team",
        kind="mcp",
        scope="team",
        url="https://mcp.example.com/team",
        tools={"read": {"risk": "read"}},
    )
    ctx.workspace = ctx.workspace.model_copy(update={"connectors": [cfg, team]})
    sessions, seen = {}, []
    for key in ("ada", "ben"):
        session = FakeMCPSession(tools=["read", "write", "send"], text="Private data for " + key)
        session.list_tools = AsyncMock(
            return_value=SimpleNamespace(
                tools=[
                    SimpleNamespace(name=name, inputSchema={"type": "object", "description": key})
                    for name in ("read", "write", "send")
                ],
                nextCursor=None,
            )
        )
        sessions["fake-" + key + "-access"] = session
    sessions[None] = FakeMCPSession(tools=["read"])

    @asynccontextmanager
    async def factory(config, bearer):
        seen.append(bearer)
        yield sessions[bearer]

    ctx.mcp_client = MCPClient(session_factory=factory)
    async with ctx.pool.connection() as conn:
        for key in ("ada", "ben"):
            member = await people.get(conn, key)
            await store.save(
                conn, ctx, cfg.key, member["id"], {"access_token": "fake-" + key + "-access"}, []
            )
    return ctx, cfg, sessions, seen


async def make_run(
    ctx, conn, *, key="ada", profile="person_dm", principal=None, deep=None, tainted=False, **flags
):
    member = await people.get(conn, key) if key else None
    row = await runs.create(
        conn,
        principal=principal or ("person:" + key if key else "team"),
        person_id=member["id"] if member else None,
        profile=profile,
        runtime="fake",
        model="fake",
        tainted=tainted,
    )
    if deep:
        await conn.execute("UPDATE runs SET deep_kind=%s WHERE id=%s", (deep, row["id"]))
        row["deep_kind"] = deep
    if flags.get("customer_read"):
        await conn.execute("UPDATE runs SET customer_read=true WHERE id=%s", (row["id"],))
        row["customer_read"] = True
    return ToolContext(conn, row, ctx)


@pytest.mark.parametrize(
    "profile,principal,key,deep,tainted,allowed",
    [
        ("person_dm", "person:ada", "ada", None, False, True),
        ("team_channel", "team", None, None, False, False),
        ("routine", "team", None, None, False, False),
        ("routine", "person:ada", "ada", None, False, False),
        ("team_channel", "team", None, "request", False, False),
        ("person_dm", "person:ada", "ada", "request", False, True),
        ("person_dm", "person:ada", "ada", "scout", False, True),
        ("person_dm", "person:ada", "ada", "brief", False, True),
        ("routine", "team", None, "scout", False, False),
        ("agent_request", "person:ada", "ada", None, False, False),
        ("person_dm", "person:ada", "ada", None, True, False),
        ("tainted", "outsider", None, None, False, False),
    ],
)
async def test_personal_mcp_access_matches_google(
    personal_mcp, profile, principal, key, deep, tainted, allowed
):
    ctx, _, _, seen = personal_mcp
    async with ctx.pool.connection() as conn:
        tc = await make_run(
            ctx, conn, key=key, profile=profile, principal=principal, deep=deep, tainted=tainted
        )
        listing = await list_connectors(tc, {})
        assert ("acme-crm" in listing) is allowed
        seen.clear()
        result = await ctx.registry.call(tc, "connectors_call", ARGS)
        assert result["decision"] == ("allow" if allowed else "deny")
        assert bool(seen) is allowed


async def test_two_people_use_separate_tokens_and_schemas(personal_mcp):
    ctx, cfg, sessions, seen = personal_mcp
    async with ctx.pool.connection() as conn:
        for key in ("ada", "ben", "ada"):
            tc = await make_run(ctx, conn, key=key)
            catalog = await list_connectors(tc, {})
            other = "ben" if key == "ada" else "ada"
            assert key in catalog and other not in catalog
            seen.clear()
            result = await ctx.registry.call(tc, "connectors_call", ARGS)
            assert result["decision"] == "allow"
            assert "Private data for " + key in result["result"]
            assert seen == ["fake-" + key + "-access"]
    assert cfg.model_dump_json() not in ctx.mcp_client.schemas
    assert len(sessions["fake-ada-access"].calls) == 2
    assert len(sessions["fake-ben-access"].calls) == 1


async def test_no_grant_lists_names_risks_and_hint_without_network(personal_mcp):
    ctx, cfg, _, seen = personal_mcp
    ctx.workspace = ctx.workspace.model_copy(update={"connectors": [cfg]})
    async with ctx.pool.connection() as conn:
        await conn.execute("DELETE FROM connector_grants")
        tc = await make_run(ctx, conn)
        result = await list_connectors(tc, {})
    assert "read" in result and "write_internal" in result
    assert 'Not signed in. The person writes "connect my acme-crm" in a DM to Illo.' in result
    assert not seen


async def test_personal_read_closes_web_and_team_but_keeps_own_reads(personal_mcp):
    ctx, _, _, seen = personal_mcp
    async with ctx.pool.connection() as conn:
        tc = await make_run(ctx, conn)
        for _ in range(2):
            assert (await ctx.registry.call(tc, "connectors_call", ARGS))["decision"] == "allow"
        assert tc.run["personal_read"]
        stored = await runs.get(conn, tc.run["id"])
        assert stored["personal_read"] and not stored["customer_read"]
        seen.clear()
        team = await ctx.registry.call(tc, "connectors_call", {**ARGS, "connector": "acme-team"})
        assert team["decision"] == "deny"
        web = await ctx.registry.call(tc, "web_read_public_page", {"url": "https://example.com"})
        assert web["decision"] == "deny"
        assert not seen


@pytest.mark.parametrize("tool", ["write", "send"])
async def test_each_personal_write_needs_its_own_card(personal_mcp, tool):
    ctx, _, sessions, _ = personal_mcp
    async with ctx.pool.connection() as conn:
        tc = await make_run(ctx, conn)
        await ctx.registry.call(tc, "connectors_call", ARGS)
        ids = []
        for _ in range(2):
            result = await ctx.registry.call(tc, "connectors_call", {**ARGS, "tool": tool})
            assert result["decision"] == "draft"
            ids.append(result["result"]["draft_id"])
        assert ids[0] != ids[1]
        drafts = await all_rows(conn, "SELECT * FROM drafts ORDER BY id")
        assert all(d["approver_person_id"] == tc.run["person_id"] for d in drafts)
    assert [c["name"] for c in sessions["fake-ada-access"].calls] == ["read"]
    await ctx.draft_performers["connectors_call"](ctx, {**drafts[0], "state": "approved"})
    assert sessions["fake-ada-access"].calls[-1]["name"] == tool


@pytest.mark.parametrize("deep", ["request", "scout", "brief"])
async def test_personal_writes_never_run_in_deep_work(personal_mcp, deep):
    ctx, _, _, seen = personal_mcp
    async with ctx.pool.connection() as conn:
        tc = await make_run(ctx, conn, deep=deep)
        for tool in ("write", "send"):
            assert (await ctx.registry.call(tc, "connectors_call", {**ARGS, "tool": tool}))[
                "decision"
            ] == "deny"
        assert not await all_rows(conn, "SELECT * FROM drafts")
    assert not seen


async def test_customer_read_needs_approval_for_a_personal_read(personal_mcp):
    ctx, _, _, seen = personal_mcp
    async with ctx.pool.connection() as conn:
        tc = await make_run(ctx, conn, customer_read=True)
        result = await ctx.registry.call(tc, "connectors_call", ARGS)
        assert result["decision"] == "draft" and not seen
        draft = await one(conn, "SELECT * FROM drafts")
    await ctx.draft_performers["connectors_call"](ctx, {**draft, "state": "approved"})
    assert seen == ["fake-ada-access"]


async def test_handler_refuses_a_forged_person_even_without_registry(personal_mcp):
    ctx, _, _, seen = personal_mcp
    async with ctx.pool.connection() as conn:
        tc = await make_run(ctx, conn)
        tc.run["principal"] = "person:ben"
        with pytest.raises(ValueError, match="owner"):
            await call_connector(tc, ARGS)
    assert not seen


async def test_ended_grant_notifies_only_its_person_and_leaves_other_grants_working(
    personal_mcp, monkeypatch
):
    ctx, cfg, _, seen = personal_mcp
    ctx.config.public_url = "https://illo.example.com"
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        ben = await people.get(conn, "ben")
        await conn.execute(
            "UPDATE connector_grants SET expires_at=clock_timestamp()-interval '1 second' WHERE person_id=%s",
            (ben["id"],),
        )
        tc = await make_run(ctx, conn, key="ben")
        for _ in range(2):
            result = await ctx.registry.call(tc, "connectors_call", ARGS)
            assert result["decision"] == "deny" and "Your sign-in" in result["reason"]
    notices = [c for c in ctx.slack_client.calls if c["method"] == "chat_postMessage"]
    assert len(notices) == 1
    opened = [c for c in ctx.slack_client.calls if c["method"] == "conversations_open"]
    assert opened[0]["users"] == ben["slack_id"]
    button = notices[0]["blocks"][1]["elements"][0]
    monkeypatch.setattr(mcp_oauth, "reconnect", AsyncMock())
    await mcp_oauth.signin_button(
        ctx, ada, {"channel": "D_EXAMPLE", "action_id": button["action_id"]}
    )
    mcp_oauth.reconnect.assert_not_called()
    await mcp_oauth.signin_button(
        ctx, ben, {"channel": "D_EXAMPLE", "action_id": button["action_id"]}
    )
    assert mcp_oauth.reconnect.call_args.args[2]["id"] == ben["id"]
    async with ctx.pool.connection() as conn:
        tc = await make_run(ctx, conn)
        assert (await ctx.registry.call(tc, "connectors_call", ARGS))["decision"] == "allow"
        row = await one(conn, "SELECT * FROM connector_grants WHERE person_id=%s", (ada["id"],))
        assert not row["broken_at"]
        sensor = await one(conn, "SELECT * FROM sensors WHERE key=%s", ("connector:" + cfg.key,))
        assert sensor["last_ok_at"] > sensor["last_error_at"]
        assert "Ben" not in str(sensor) and "Ada" not in str(sensor)
    assert seen == ["fake-ada-access"]


async def test_refresh_saves_each_persons_token_in_its_own_grant(personal_mcp):
    ctx, cfg, sessions, seen = personal_mcp
    cfg.auth.token_url = "https://auth.example.com/token"
    sent = []

    async def refresh(method, url, **options):
        key = options["data"]["refresh_token"].removeprefix("fake-refresh-")
        sent.append(key)
        sessions["fake-new-" + key] = sessions["fake-" + key + "-access"]
        return {"access_token": "fake-new-" + key, "expires_in": 3600}

    ctx.mcp_client.http.request = refresh
    async with ctx.pool.connection() as conn:
        for key in ("ada", "ben"):
            member = await people.get(conn, key)
            await store.save(
                conn,
                ctx,
                cfg.key,
                member["id"],
                {
                    "access_token": "fake-" + key + "-access",
                    "refresh_token": "fake-refresh-" + key,
                },
                [],
            )
        await conn.execute(
            "UPDATE connector_grants SET expires_at=clock_timestamp()-interval '1 second'"
        )
        for key in ("ben", "ada"):
            tc = await make_run(ctx, conn, key=key)
            assert (await ctx.registry.call(tc, "connectors_call", ARGS))["decision"] == "allow"
            _, token = await store.load(conn, ctx, cfg.key, tc.run["person_id"])
            assert token["access_token"] == "fake-new-" + key
    assert sent == ["ben", "ada"] and seen == ["fake-new-ben", "fake-new-ada"]


async def test_personal_catalog_is_hidden_when_it_is_the_only_connector_and_access_is_denied(
    personal_mcp,
):
    ctx, cfg, _, seen = personal_mcp
    ctx.workspace = ctx.workspace.model_copy(update={"connectors": [cfg]})
    async with ctx.pool.connection() as conn:
        tc = await make_run(ctx, conn, profile="agent_request")
        names = {t["name"] for t in await ctx.registry.list_tools_for(tc)}
        assert "connectors_list" not in names and "connectors_call" not in names
    assert not seen


async def test_customer_read_without_a_person_cannot_call_a_personal_connector(personal_mcp):
    ctx, _, _, seen = personal_mcp
    async with ctx.pool.connection() as conn:
        tc = await make_run(ctx, conn, key=None, profile="routine", customer_read=True)
        assert (await ctx.registry.call(tc, "connectors_call", ARGS))["decision"] == "deny"
        assert not await all_rows(conn, "SELECT * FROM drafts")
    assert not seen


async def test_personal_deep_run_inherits_private_read_without_another_persons_grant(
    personal_mcp, fake_brain
):
    from illo.deep import start
    from illo.deep.worker import tick
    from tests.tool_results import read_result

    ctx, _, _, seen = personal_mcp
    async with ctx.pool.connection() as conn:
        tc = await make_run(ctx, conn, key="ben")
        tc.run["personal_read"] = True
        await start(
            ctx,
            conn,
            kind="request",
            job="Read my records.",
            authority=tc.run,
            deliver={"slack_dm": {"person_id": tc.run["person_id"]}},
        )
    fake_brain.script = [
        {"name": "connectors_call", "arguments": ARGS},
        {"name": "web_fetch", "arguments": {"url": "https://example.com"}},
    ]
    row = await tick(ctx)
    assert row["state"] == "done"
    results = [read_result(r) for r in fake_brain.results]
    assert [r["decision"] for r in results] == ["allow", "deny"]
    assert "Private data for ben" in results[0]["result"]
    assert seen == ["fake-ben-access"]
    async with ctx.pool.connection() as conn:
        run = await runs.get(conn, row["run_id"])
        assert run["person_id"] == tc.run["person_id"] and run["personal_read"]


async def test_personal_catalog_uses_the_personal_data_rule(personal_mcp):
    ctx, _, _, seen = personal_mcp
    async with ctx.pool.connection() as conn:
        tc = await make_run(ctx, conn)
        await list_connectors(tc, {})
        assert tc.run["personal_read"]
        stored = await runs.get(conn, tc.run["id"])
        assert stored["personal_read"]
        assert (await ctx.registry.call(tc, "connectors_call", {**ARGS, "connector": "acme-team"}))[
            "decision"
        ] == "deny"
    assert seen == ["fake-ada-access"]
