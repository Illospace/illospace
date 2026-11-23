"""Verify schema creation, restart safety, and per-file rollback."""

import uuid
from pathlib import Path

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation, UndefinedTable

from illo.db import all_rows, migrate, one


async def test_migrations_apply_twice(pool):
    async with pool.connection() as conn:
        assert await migrate(conn) == []
        assert await migrate(conn) == []
        records = await all_rows(conn, "SELECT name FROM schema_migrations")
        expected = {
            path.name for path in (Path(__file__).parents[1] / "illo/migrations").glob("*.sql")
        }
        assert {row["name"] for row in records} == expected
        names = {
            r["tablename"]
            for r in await all_rows(
                conn, "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            )
        }
        assert {
            "people",
            "tokens",
            "events",
            "runs",
            "audit",
            "lanes",
            "lane_events",
            "asks",
            "rules",
            "items",
            "item_search",
            "slack_event_search",
            "submissions",
            "drafts",
            "signals",
            "claims",
            "connector_grants",
            "secrets",
            "routine_state",
            "sensors",
            "kv",
        } <= names
        indexes = await all_rows(conn, "SELECT indexdef FROM pg_indexes WHERE tablename='item_search'")
        assert any("USING gin (vector)" in r["indexdef"] for r in indexes)


async def test_each_migration_rolls_back_as_one_file(pool, tmp_path):
    (tmp_path / "9999_fail.sql").write_text(
        "CREATE TABLE should_rollback (id int); SELECT * FROM missing_table;"
    )
    async with pool.connection() as conn:
        with pytest.raises(UndefinedTable):
            await migrate(conn, tmp_path)
        assert (await one(conn, "SELECT to_regclass('should_rollback') AS name"))["name"] is None
        assert not await one(conn, "SELECT name FROM schema_migrations WHERE name='9999_fail.sql'")


async def test_schema_checks_states_and_foreign_keys(conn):
    with pytest.raises(CheckViolation):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO lanes (source,external_id,title,state) VALUES ('github','1','x','stale')"
            )

    with pytest.raises(ForeignKeyViolation):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO tokens (person_id,label,token_hash) VALUES (999,'test','hash')"
            )


async def test_agent_oauth_migration_retries_and_keeps_static_tokens(conn, ada):
    from illo.ledger import tokens

    path = Path(__file__).parents[1] / "illo/migrations/1701_agent_oauth.sql"
    text = path.read_text()
    assert text.startswith("SET LOCAL lock_timeout = '1s';")
    raw = await tokens.create(conn, ada["id"], "Static machine", ["agent"])
    await conn.commit()
    for _ in range(2):
        async with conn.transaction():
            await conn.execute(text)
    assert (await tokens.verify(conn, raw))["label"] == "Static machine"
    names = {r["tablename"] for r in await all_rows(conn, "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert {"agent_oauth_clients", "agent_oauth_requests", "agent_oauth_access", "agent_oauth_refresh", "agent_oauth_limits"} <= names
    with pytest.raises(ForeignKeyViolation):
        async with conn.transaction():
            await conn.execute("""INSERT INTO agent_oauth_access (token_hash,token_id,resource,expires_at)
                VALUES ('hash',999,'https://illo.example.com/mcp',clock_timestamp())""")


async def test_slack_signin_migration_retries_and_keeps_pairing(conn, ada):
    from illo.ledger import agent_oauth as oauth
    from illo.ledger import tokens

    path = Path(__file__).parents[1] / "illo/migrations/1702_agent_slack_signin.sql"
    text = path.read_text()
    assert text.startswith("SET LOCAL lock_timeout = '1s';")
    client = await oauth.register(conn, "Existing app", ["http://localhost/callback"])
    pending = await oauth.request(conn, client["id"], "http://localhost/callback",
                                 "https://illo.example.com/mcp", None, oauth.challenge("v" * 43))
    await conn.commit()
    for _ in range(2):
        async with conn.transaction():
            await conn.execute(text)
    row = await oauth.browser_request(conn, pending["handle"])
    for column in ("slack_state_hash", "slack_nonce_hash", "consent_hash", "notice_at"):
        assert row[column] is None
    # Build 23 writes no new column. Its pairing and card decision still work.
    paired = await oauth.pair(conn, pending["code"].replace("-", ""), ada["slack_id"], "D_PAIR", "1")
    await conn.execute("UPDATE agent_oauth_requests SET slack_ts='2' WHERE id=%s", (paired["id"],))
    assert await oauth.decide(conn, paired["id"], ada["id"], "D_PAIR", "2", True)
    row = await oauth.browser_request(conn, pending["handle"])
    assert row["decision"] == "approved" and row["token_id"]
    callback, params = await oauth.continue_request(conn, pending["handle"])
    grant = await oauth.exchange(conn, {"client_id": client["id"], "code": params["code"], "code_verifier": "v" * 43})
    assert callback == "http://localhost/callback"
    assert (await tokens.verify(conn, grant["access_token"]))["person_id"] == ada["id"]


async def test_brain_routes_expand_keeps_the_old_runs_create_and_fills_old_rows(conn, ada):
    """Step 1 of 2. The old code still writes runs after 0601. A later migration drops runtime."""
    path = Path(__file__).parents[1] / "illo/migrations/0601_brain_routes.sql"
    text = path.read_text()
    assert text.startswith("SET LOCAL lock_timeout = '1s';")

    async def event(channel):
        return (await one(conn, "INSERT INTO events (channel,external_id,kind) VALUES (%s,%s,'x') "
                                "RETURNING id", (channel, uuid.uuid4().hex)))["id"]

    async def old_create(runtime, profile="person_dm", channel="slack", tainted=False, usage="{}"):
        # The SQL of runs.create() before this change, unchanged.
        row = await one(conn, """
            INSERT INTO runs (principal,person_id,profile,runtime,model,event_id,tainted)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, ("person:ada", ada["id"], profile, runtime, "old-model", await event(channel), tainted))
        await conn.execute("UPDATE runs SET usage=%s::jsonb WHERE id=%s", (usage, row["id"]))
        return row["id"]

    rows = {
        "claude": await old_create("claude"),
        "subscription": await old_create("openai_responses", usage='{"auth_kind": "chatgpt"}'),
        "api_key": await old_create("openai_responses", usage='{"auth_kind": "api_key"}'),
        "compat": await old_create("openai_compat", profile="routine", channel="routine"),
        "echo": await old_create("echo", channel="mcp", profile="agent_request"),
        "tainted": await old_create("claude", tainted=True),
        "digest": await old_create("claude", channel="digest"),
        "deep": await old_create("claude", channel="deep"),
        "unknown": await old_create("claude", channel="chat"),
    }
    await conn.commit()
    for _ in range(2):
        async with conn.transaction():
            await conn.execute(text)
    got = {name: await one(conn, "SELECT runtime,route,provider,payer,payer_person_id,tool_limit "
                                 "FROM runs WHERE id=%s", (run_id,)) for name, run_id in rows.items()}
    assert {name: (row["provider"], row["route"]) for name, row in got.items()} == {
        "claude": ("anthropic", "person_dm"),
        "subscription": ("chatgpt", "person_dm"),
        # An old API-key row can have used a custom base URL. It does not prove openai.
        "api_key": ("unknown", "person_dm"),
        "compat": ("unknown", "routine"),
        "echo": ("fake", "agent_request"),
        "tainted": ("anthropic", "tainted"),
        "digest": ("anthropic", "digest"),
        "deep": ("anthropic", "deep"),
        "unknown": ("anthropic", None),
    }
    assert all(row["payer"] is None and row["tool_limit"] is None for row in got.values())
    # The new code writes no runtime. The column stays until the contract step drops it.
    from illo.ledger import runs

    new = await runs.create(conn, principal="team", person_id=None, profile="routine",
                            route="routine", provider="openai", model="m", payer="api_key",
                            tool_limit=12)
    assert new["runtime"] is None and new["provider"] == "openai" and new["payer"] == "api_key"
    with pytest.raises(CheckViolation):
        async with conn.transaction():
            await conn.execute("UPDATE runs SET payer='card' WHERE id=%s", (new["id"],))
