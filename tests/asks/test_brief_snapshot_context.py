"""Brief snapshots keep past openings and count only finished work."""

from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb

from illo.db import one
from illo.ledger import digest_snapshot, github, lanes
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
    personal = await digest_snapshot.build(conn, ada, NOW)
    team = await digest_snapshot.team_snapshot(conn, workspace.config, NOW)
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


async def sync(conn, number, *, body="", pull=False, done=True, not_planned=False):
    item = {
        "number": number, "title": f"{'Pull request' if pull else 'Issue'} {number}",
        "body": body, "state": "closed", "closed_at": (NOW - timedelta(hours=1)).isoformat(),
        "html_url": f"https://github.com/example/project/{'pull' if pull else 'issues'}/{number}",
        "assignees": [{"login": "ada-example"}],
    }
    if pull:
        item.update(pull_request={}, merged=done)
    if not_planned:
        item["state_reason"] = "not_planned"
    return await github.sync_item(conn, "example/project", item, {})


@pytest.mark.parametrize("issue_first", [True, False])
async def test_finished_issue_and_merged_pull_count_once(conn, ada, workspace, issue_first):
    # The poller stores a closes link in either arrival order.
    if issue_first:
        issue = await sync(conn, 10)
    pull = await sync(conn, 11, body="Fixes #10", pull=True)
    if not issue_first:
        issue = await sync(conn, 10)
    for row in (issue, pull):
        await conn.execute("UPDATE lanes SET owner_person_id=%s WHERE id=%s", (ada["id"], row["id"]))
    for data, field in (
        (await digest_snapshot.build(conn, ada, NOW), "finished"),
        (await digest_snapshot.team_snapshot(conn, workspace.config, NOW), "shipped"),
    ):
        assert data[field + "_total"] == 1
        assert len(data[field]) == 1
        assert data[field][0]["title"] == "Issue 10"
        assert data[field][0]["link"] == "https://github.com/example/project/pull/11"
        assert data[field][0]["state"] == "done"


async def test_failed_work_is_counted_but_never_shipped(conn, ada, ben, workspace):
    for number, args in ((20, {"not_planned": True}), (21, {"pull": True, "done": False})):
        row = await sync(conn, number, **args)
        await conn.execute("UPDATE lanes SET owner_person_id=%s WHERE id=%s", (ada["id"], row["id"]))
    private = await lanes.upsert(conn, source="manual", external_id="hidden-failure", title="Hidden failure", state="failed", owner_person_id=ben["id"], visibility="private")
    await conn.execute("UPDATE lanes SET closed_at=%s WHERE id=%s", (NOW, private["id"]))
    for data, field in (
        (await digest_snapshot.build(conn, ada, NOW), "finished"),
        (await digest_snapshot.team_snapshot(conn, workspace.config, NOW), "shipped"),
    ):
        assert data[field] == [] and data[field + "_total"] == 0
        assert data["closed_not_done"] == 2
        assert "Hidden failure" not in str(data)


async def test_pair_does_not_expose_a_private_issue(conn, ada, ben, workspace):
    issue = await sync(conn, 30)
    await conn.execute("UPDATE lanes SET visibility='private',owner_person_id=%s,title='Hidden issue' WHERE id=%s", (ben["id"], issue["id"]))
    pull = await sync(conn, 31, body="Closes #30", pull=True)
    await conn.execute("UPDATE lanes SET owner_person_id=%s WHERE id=%s", (ada["id"], pull["id"]))
    for data, field in (
        (await digest_snapshot.build(conn, ada, NOW), "finished"),
        (await digest_snapshot.team_snapshot(conn, workspace.config, NOW), "shipped"),
    ):
        assert data[field + "_total"] == 1
        assert data[field][0]["title"] == "Pull request 31"
        assert "Hidden issue" not in str(data)


def test_brief_instructions_match_the_example_files():
    from pathlib import Path

    from illo.routines.digest import DEFAULT_INSTRUCTIONS as personal
    from illo.routines.team_digest import DEFAULT_INSTRUCTIONS as team

    root = Path(__file__).resolve().parents[2]
    assert (root / "workspace.example/routines/digest.md").read_text().strip() == personal
    assert (root / "workspace.example/routines/team-digest.md").read_text().strip() == team
    line = '- `recent_openings` shows how your last briefs started. Start in a different way, and do not reuse a comparison or a joke from them.'
    assert personal.count(line) == team.count(line) == 1
    for text in (personal, team):
        assert text.index("- Maximum 8 lines") < text.index(line)


@pytest.mark.parametrize("owns_issue", [True, False])
async def test_personal_finished_pair_can_have_two_team_owners(conn, ada, ben, owns_issue):
    issue = await sync(conn, 40)
    pull = await sync(conn, 41, body="Fixes #40", pull=True)
    for row, owner in ((issue, ada if owns_issue else ben), (pull, ben if owns_issue else ada)):
        await conn.execute("UPDATE lanes SET owner_person_id=%s WHERE id=%s", (owner["id"], row["id"]))
    data = await digest_snapshot.build(conn, ada, NOW)
    assert data["finished_total"] == 1
    assert data["finished"][0]["title"] == "Issue 40"
    assert data["finished"][0]["link"] == "https://github.com/example/project/pull/41"


async def test_many_pull_requests_that_close_one_issue_count_once(conn, ada, workspace):
    issue = await sync(conn, 50)
    for number in (51, 52):
        await sync(conn, number, body="Resolves #50", pull=True)
    data = await digest_snapshot.team_snapshot(conn, workspace.config, NOW)
    assert data["shipped_total"] == 1
    assert data["shipped"][0]["title"] == issue["title"]
    assert data["shipped"][0]["link"] == "https://github.com/example/project/pull/52"
