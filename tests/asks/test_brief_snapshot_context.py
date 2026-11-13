"""Brief snapshots keep past openings and count only finished work."""

from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb

from illo.db import one
from illo.ledger import digest_snapshot
from tests.asks.test_digest import NOW


async def brief_event(conn, channel, person_id, text, *, status="handled", suffix=""):
    return await one(
        conn,
        """INSERT INTO events(channel,external_id,kind,person_id,status,payload)
        VALUES (%s,%s,'scheduled',%s,%s,%s) RETURNING *""",
        (channel, channel + suffix, person_id, status, Jsonb({"final_answer": text})),
    )


async def test_recent_openings_are_bounded_and_private(conn, ada, ben, workspace):
    assert await digest_snapshot.recent_openings(conn, "digest", ada["id"]) == []
    assert await digest_snapshot.recent_openings(conn, "team_digest") == []
    for index in range(7):
        await brief_event(conn, "digest", ada["id"], f"Opening {index}\nOther lines", suffix=str(index))
        await brief_event(conn, "digest", ben["id"], f"Other person {index}", suffix="other" + str(index))
        await brief_event(conn, "team_digest", None, f"Team {index}\nDetails", suffix=str(index))
    for channel, person in (("digest", ada["id"]), ("team_digest", None)):
        await brief_event(conn, channel, person, "Failed brief", status="failed", suffix="failed")
        await brief_event(conn, channel, person, None, suffix="empty")
    await brief_event(conn, "slack", ada["id"], "A direct message")
    await brief_event(conn, "digest", ada["id"], "x" * 200 + "\nDetails", suffix="long")
    personal = await digest_snapshot.build(conn, ada, NOW, workspace)
    team = await digest_snapshot.team_snapshot(conn, workspace, NOW)
    assert personal["recent_openings"] == ["x" * 160, "Opening 6", "Opening 5", "Opening 4", "Opening 3"]
    assert team["recent_openings"] == [f"Team {i}" for i in range(6, 1, -1)]
    assert "Other person" not in str(personal["recent_openings"]) + str(team["recent_openings"])


async def test_openings_query_uses_an_index_and_a_limit(conn, ada):
    class Recorded:
        async def execute(self, query, args):
            self.query, self.args = query, args
            return await conn.execute(query, args)

    recorded = Recorded()
    for person in (ada["id"], None):
        await digest_snapshot.recent_openings(recorded, "digest" if person else "team_digest", person)
        assert "LIMIT 5" in recorded.query
        async with conn.transaction():
            await conn.execute("SET LOCAL enable_seqscan=off")
            plan = await (await conn.execute("EXPLAIN " + recorded.query, recorded.args)).fetchall()
        assert "events_brief_openings_idx" in str(plan)


@pytest.mark.parametrize("channel", ["digest", "team_digest"])
async def test_openings_are_frozen_before_the_model_reads_them(app, fake_brain, slack_client, monkeypatch, channel):
    from illo.ledger import people
    from illo.routines.digest import send_due
    from illo.routines.team_digest import send_team_due
    from tests.asks.test_brief_preferences import only_ada
    from tests.asks.test_team_digest import configure, seed_team
    from tests.tool_results import read_result, snapshot_data

    ctx = app.state.ctx
    if channel == "digest":
        await only_ada(ctx)
    else:
        configure(ctx)
        await seed_team(ctx)
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        row = await brief_event(conn, channel, ada["id"] if channel == "digest" else None, "Previous opening")
    fake_brain.script = [{"name": "digest_snapshot" if channel == "digest" else "team_snapshot", "arguments": {}}]
    original = fake_brain.run

    async def change(request):
        async with ctx.pool.connection() as conn:
            await conn.execute("UPDATE events SET payload=%s WHERE id=%s", (Jsonb({"final_answer": "Changed later"}), row["id"]))
        async for event in original(request):
            yield event

    monkeypatch.setattr(fake_brain, "run", change)
    await (send_due(ctx, NOW) if channel == "digest" else send_team_due(ctx, NOW + timedelta(hours=5)))
    result = snapshot_data(read_result(fake_brain.results[0])["result"])
    assert result["recent_openings"] == ["Previous opening"]
    async with ctx.pool.connection() as conn:
        stored = await one(conn, "SELECT payload FROM events WHERE channel=%s ORDER BY id DESC LIMIT 1", (channel,))
    key = "digest_snapshot" if channel == "digest" else "team_snapshot"
    assert stored["payload"]["payload"][key]["recent_openings"] == ["Previous opening"]


def test_brief_instructions_match_the_example_files():
    from pathlib import Path

    from illo.routines.digest import DEFAULT_INSTRUCTIONS as personal
    from illo.routines.team_digest import DEFAULT_INSTRUCTIONS as team

    root = Path(__file__).resolve().parents[2]
    assert (root / "workspace.example/routines/digest.md").read_text().strip() == personal
    assert (root / "workspace.example/routines/team-digest.md").read_text().strip() == team
    from illo.brain.prompts import PROFILE_TEXTS

    assert "recent_openings" in personal
    assert "Every number" in personal
    assert "recent_openings" in PROFILE_TEXTS["team_digest"]
    assert "Every claim comes from a tool result of this run." in team
