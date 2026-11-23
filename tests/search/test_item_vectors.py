"""Keep stored text vectors current through every database write shape."""

from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb

from illo.db import one
from illo.ledger import items, mcp
from illo.sources import search, slack_index

NOW = datetime(2026, 5, 20, tzinfo=UTC)


@pytest.mark.parametrize("field", ["title", "summary", "body"])
async def test_item_text_changes_and_delete(conn, field):
    fields = {"title": "widget", field: "oldword"}
    row = await items.upsert(conn, source_ref="note:widget", **fields)
    assert await items.search(conn, "oldword")
    await conn.execute(f"UPDATE items SET {field}='newword' WHERE id=%s", (row["id"],))
    assert not await items.search(conn, "oldword")
    assert await items.search(conn, "newword")
    assert not await search._items(conn, "team", "oldword", None, 50, NOW, [])
    assert await search._items(conn, "team", "newword", None, 50, NOW, [])
    await conn.execute("DELETE FROM items WHERE id=%s", (row["id"],))
    assert not await one(conn, "SELECT * FROM item_search WHERE item_id=%s", (row["id"],))


async def test_item_save_and_upsert(conn, ada):
    row = await mcp.save_item(conn, title="oldword", body="oldword", person_id=ada["id"],
                              visibility="team", meta={})
    assert await items.search(conn, "oldword")
    await items.upsert(conn, source_ref=row["source_ref"], title="newword", body="newword",
                       summary="newword")
    assert not await items.search(conn, "oldword")
    assert await items.search(conn, "newword")


async def test_slack_text_eligibility_and_delete(conn, monkeypatch):
    async def channels(*args):
        return {"C_ACME": "acme"}

    monkeypatch.setattr(slack_index, "channels", channels)
    row = await one(conn, """INSERT INTO events(channel,external_id,kind,principal,payload)
        VALUES ('slack','C_ACME:1','message','team',%s) RETURNING id""",
        (Jsonb({"text": "oldword", "payload": {"channel_id": "C_ACME"}}),))

    async def found(query):
        return await slack_index.search(None, conn, query, None, 10, NOW)

    assert await found("oldword")
    await conn.execute("UPDATE events SET payload=jsonb_set(payload,'{text}',%s) WHERE id=%s",
                       (Jsonb("newword"), row["id"]))
    assert not await found("oldword")
    assert await found("newword")
    for field, hidden, visible in [("channel", "email", "slack"),
                                    ("principal", "person:ada", "team"),
                                    ("kind", "dm", "channel")]:
        await conn.execute(f"UPDATE events SET {field}=%s WHERE id=%s", (hidden, row["id"]))
        assert not await found("newword")
        assert not await one(conn, "SELECT * FROM slack_event_search WHERE event_id=%s", (row["id"],))
        await conn.execute(f"UPDATE events SET {field}=%s WHERE id=%s", (visible, row["id"]))
        assert await found("newword")
    await conn.execute("DELETE FROM events WHERE id=%s", (row["id"],))
    assert not await one(conn, "SELECT * FROM slack_event_search WHERE event_id=%s", (row["id"],))


async def test_migration_lock_timeout_rolls_back_and_retries(conn, pool):
    from pathlib import Path

    from psycopg.errors import LockNotAvailable

    from illo.db import migrate

    await items.upsert(conn, source_ref="note:widget", title="widget")
    await conn.execute("DROP FUNCTION illo_item_search_vector() CASCADE")
    await conn.execute("DROP FUNCTION illo_slack_event_search_vector() CASCADE")
    await conn.execute("DROP TABLE item_search,slack_event_search")
    await conn.execute("DELETE FROM schema_migrations WHERE name='0406_search_items.sql'")
    await conn.commit()
    migration = (Path(__file__).parents[2] / "illo/migrations/0406_search_items.sql").read_text()
    async with pool.connection() as writer:
        await writer.execute("UPDATE items SET title='newword'")
        with pytest.raises(LockNotAvailable):
            async with conn.transaction():
                await conn.execute(migration)
        assert (await one(conn, "SELECT to_regclass('item_search') AS name"))["name"] is None
        await writer.commit()
    assert await migrate(conn) == ["0406_search_items.sql"]
    assert await migrate(conn) == []
    assert await items.search(conn, "newword")
