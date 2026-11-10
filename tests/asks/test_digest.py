"""Digest text, person time zones, and failed delivery reports."""

from datetime import UTC, datetime

import pytest

from illo.asks.service import open_asks_summary
from illo.db import one
from illo.ledger import people, sensors
from illo.routines.digest import build_digest, send_due, snapshot


@pytest.mark.parametrize("count", [0, 3])
def test_pure_digest(count):
    now = datetime(2026, 11, 10, 9, tzinfo=UTC)
    data = {
        "now": now,
        "asks": [
            {"question": f"Question {i}", "lane_title": "Acme work", "created_at": now}
            for i in range(count)
        ],
        "running": 14,
        "finished": [],
    }
    result = str(build_digest(data))
    assert ("3 need you · 14 run fine" if count else "Nothing needs you · 14 run fine") in result
    assert "Not healthy" not in result
    assert build_digest(data) == build_digest(data)


def test_digest_sick_sensor_and_failed_answer():
    result = str(
        build_digest(
            {
                "sensors": [{"key": "collector:acme", "detail": "No recent report."}],
                "failed": [{"id": 12, "question": "Continue?"}],
            }
        )
    )
    assert "Not healthy" in result and "No recent report" in result
    assert "Answer 12 did not reach its agent" in result


async def test_digest_uses_person_time_zone_and_sends_once(app, slack_client):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET timezone='America/New_York' WHERE key='ada'")
        await conn.execute("UPDATE people SET active=false WHERE key='ben'")
    now = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)
    await send_due(ctx, now)
    await send_due(ctx, now)
    posts = [c for c in slack_client.calls if c["method"] == "chat_postMessage"]
    assert len(posts) == 1 and "Nothing needs you" in posts[0]["text"]


async def test_digest_snapshot_and_first_contact_helper(app, seed):
    ctx = app.state.ctx
    row = await seed()
    async with ctx.pool.connection() as conn:
        await sensors.error(conn, "acme", "No recent report.")
        person = await people.get(conn, "ada")
        data = await snapshot(conn, person, ctx)
        assert len(data["asks"]) == 1
        assert "No recent report" in str(build_digest(data))
    assert "1 open asks" in await open_asks_summary(person, ctx)
    assert "no open asks" in await open_asks_summary({"asks": []})
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE asks SET delivery_state='failed',state='answered' WHERE id=%s", (row["id"],)
        )
        data = await snapshot(conn, person, ctx)
        assert "did not reach its agent" in str(build_digest(data))
        assert await one(conn, "SELECT 1")


async def test_parallel_digest_timers_send_once(app, slack_client):
    import asyncio

    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET timezone='UTC',active=(key='ada')")
    now = datetime(2026, 11, 10, 9, tzinfo=UTC)
    await asyncio.gather(send_due(ctx, now), send_due(ctx, now))
    assert len([c for c in slack_client.calls if c["method"] == "chat_postMessage"]) == 1


async def test_digest_counts_only_fresh_work_and_reviews(conn, ada):
    from illo.ledger import ask_workflow, lanes

    for state in (
        "open",
        "unknown",
        "working",
        "review",
        "waiting_person",
        "blocked",
        "done",
        "failed",
    ):
        await lanes.upsert(
            conn,
            source="github",
            external_id=state,
            title=state,
            state=state,
            owner_person_id=ada["id"],
        )
    stale = await lanes.upsert(
        conn,
        source="t3",
        external_id="stale",
        title="Stale work",
        state="working",
        owner_person_id=ada["id"],
    )
    await conn.execute(
        "UPDATE lanes SET last_signal_at=now()-interval '10 days' WHERE id=%s OR state IN ('open','review')",
        (stale["id"],),
    )
    data = await ask_workflow.digest_data(conn, ada, datetime.now(UTC))
    assert data["running"] == 2
    assert "2 run fine" in str(build_digest(data))
