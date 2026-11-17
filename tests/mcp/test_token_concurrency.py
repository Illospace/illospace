"""Token use must not serialize requests or delay revocation checks."""

import asyncio

import pytest

from illo.db import one
from illo.ledger import mcp, tokens


@pytest.fixture
async def client_token(conn, ada):
    raw = await tokens.create(conn, ada["id"], "test client")
    await conn.commit()
    return raw, ada["id"]


async def test_verify_releases_usage_lock_before_request_finishes(pool, client_token):
    raw, _ = client_token
    async with pool.connection() as request_a:
        assert await tokens.verify(request_a, raw)
        # Request A keeps its transaction open while request B verifies and commits.
        async with asyncio.timeout(2):
            async with pool.connection() as request_b:
                assert await tokens.verify(request_b, raw)
        assert (await one(request_a, "SELECT last_used_at FROM tokens"))["last_used_at"]


async def test_usage_changes_at_most_once_per_minute(pool, client_token):
    raw, _ = client_token
    async with pool.connection() as conn:
        assert await tokens.verify(conn, raw)
        first = await one(conn, "SELECT last_used_at,xmin::text FROM tokens")
        for _ in range(3):
            assert await tokens.verify(conn, raw)
        assert await one(conn, "SELECT last_used_at,xmin::text FROM tokens") == first
        await conn.execute("UPDATE tokens SET last_used_at=clock_timestamp()-interval '61 seconds'")
    async with pool.connection() as conn:
        assert await tokens.verify(conn, raw)
        second = await one(conn, "SELECT last_used_at,xmin::text FROM tokens")
        assert second["last_used_at"] > first["last_used_at"]
        assert await tokens.verify(conn, raw)
        assert await one(conn, "SELECT last_used_at,xmin::text FROM tokens") == second


async def test_busy_usage_row_does_not_block_verification(pool, client_token):
    raw, _ = client_token
    async with pool.connection() as writer:
        await writer.execute("UPDATE tokens SET label='busy'")
        async with asyncio.timeout(2):
            async with pool.connection() as reader:
                assert await tokens.verify(reader, raw)
    async with pool.connection() as reader:
        assert await tokens.verify(reader, raw)
        assert (await one(reader, "SELECT last_used_at FROM tokens"))["last_used_at"]


async def test_revoked_token_and_inactive_person_fail_at_once(pool, client_token):
    raw, person_id = client_token
    async with pool.connection() as reader:
        assert await tokens.verify(reader, raw)
        async with pool.connection() as writer:
            await tokens.revoke(writer, (await one(writer, "SELECT id FROM tokens"))["id"])
        assert await tokens.verify(reader, raw) is None
        async with pool.connection() as writer:
            await writer.execute("UPDATE tokens SET revoked_at=NULL")
            await writer.execute("UPDATE people SET active=false WHERE id=%s", (person_id,))
        assert await tokens.verify(reader, raw) is None


async def test_write_guards_do_not_block_token_reads(pool, client_token):
    raw, _ = client_token
    async with pool.connection() as writer:
        token = await tokens.verify(writer, raw)
        for scope in ("asks", "tasks", "signals", "submissions"):
            await mcp.lock_token(writer, token["id"], scope)
        async with asyncio.timeout(2):
            async with pool.connection() as reader:
                assert await tokens.verify(reader, raw)


async def test_busy_token_table_does_not_block_verification(pool, client_token):
    raw, _ = client_token
    async with pool.connection() as reader:
        await reader.execute("LOCK TABLE tokens IN ACCESS EXCLUSIVE MODE")
        async with asyncio.timeout(2):
            assert await tokens.verify(reader, raw)
    async with pool.connection() as reader:
        assert await tokens.verify(reader, raw)
        assert (await one(reader, "SELECT last_used_at FROM tokens"))["last_used_at"]


async def test_foreign_key_read_lock_still_allows_usage_update(pool, client_token):
    raw, _ = client_token
    async with pool.connection() as writer:
        # Audit and receipt foreign keys hold this compatible lock until commit.
        await writer.execute("SELECT id FROM tokens FOR KEY SHARE")
        async with asyncio.timeout(2):
            async with pool.connection() as reader:
                assert await tokens.verify(reader, raw)
                assert (await one(reader, "SELECT last_used_at FROM tokens"))["last_used_at"]
