"""The briefs use tasks, their record, and the ledger's visibility rules."""

from datetime import timedelta

import pytest

from illo.ledger import asks, digest_snapshot, lanes, sensors, tasks
from illo.routines.digest import fallback_text as personal_text
from illo.routines.team_digest import fallback_text as team_text
from illo.workspace import TaskList
from tests.asks.test_digest import NOW

ADA = tasks.Viewer("ada", "person:ada", human=True)
BEN = tasks.Viewer("ben", "person:ben", human=True)
BUILD = tasks.Viewer("ada", "agent:build")


async def make(conn, workspace, title="Repair the export", *, viewer=ADA, list_key="launches", **kwargs):
    return await tasks.create(conn, workspace=workspace, viewer=viewer,
                              list_key=list_key, title=title, **kwargs)


async def stamp(conn, row, age=3):
    at = NOW - timedelta(hours=age)
    await conn.execute("UPDATE tasks SET created_at=%s,last_activity_at=%s,holder_since=%s WHERE id=%s",
                       (at, at, at, row["id"]))
    await conn.execute("UPDATE task_events SET at=%s WHERE task_id=%s", (at, row["id"]))


async def complete(conn, workspace, row, viewer=ADA):
    await tasks.done(conn, row["id"], workspace=workspace, viewer=viewer)
    await conn.execute("UPDATE tasks SET done_at=%s WHERE id=%s", (NOW - timedelta(hours=1), row["id"]))


async def test_needs_reasons_wait_order_limits_and_threads(conn, ada, workspace):
    overdue = await make(conn, workspace, "Read the overdue plan", holder="person:ada", due_at=NOW - timedelta(hours=1))
    await stamp(conn, overdue, 1)
    question = await make(conn, workspace, "Choose the format", holder="agent:build")
    await tasks.post(conn, question["id"], workspace=workspace, viewer=BUILD, question=True, text="Which format? " * 30)
    await stamp(conn, question, 9)
    await tasks.post(conn, question["id"], workspace=workspace, viewer=BUILD, text="I still need the format.")
    await conn.execute("UPDATE tasks SET slack_channel='C_ACME_TEAM',slack_ts='123.456' WHERE id=%s", (question["id"],))
    returned = await make(conn, workspace, "Confirm the result", holder="agent:build")
    await tasks.return_work(conn, returned["id"], workspace=workspace, viewer=BUILD, message="The result is ready.")
    await stamp(conn, returned, 8)
    ticket = await make(conn, workspace, "Choose a holder")
    await conn.execute("UPDATE tasks SET waits_for='person:ada' WHERE id=%s", (ticket["id"],))
    await tasks._event(conn, ticket, "code", "state", "The ticket waits for an owner.")
    await stamp(conn, ticket, 7)
    for i in range(6):
        row = await make(conn, workspace, f"Read plan {i}", holder="person:ada")
        await stamp(conn, row, 6 - i)
    data = await digest_snapshot.build(conn, ada, NOW, workspace)
    assert data["needs_you_total"] == 10 and len(data["needs_you"]) == 7
    assert [i["id"] for i in data["needs_you"][:4]] == [overdue["id"], question["id"], returned["id"], ticket["id"]]
    assert data["needs_you"][0]["reason"].startswith("you hold it; due ")
    assert data["needs_you"][1]["reason"] == ("Which format? " * 30)[:200]
    assert data["needs_you"][1]["age_hours"] == 9
    assert data["needs_you"][1]["link"] == "https://slack.com/archives/C_ACME_TEAM/p123456"
    assert data["needs_you"][2]["reason"] == "returned by Build agents, confirm or send back"
    assert "waits for an owner" in data["needs_you"][3]["reason"]
    assert "and 3 more" in personal_text(data)
    assert data["due"] == []


async def test_agents_silence_threshold_and_restart_window(conn, ada, workspace):
    row = await make(conn, workspace, holder="agent:build")
    await stamp(conn, row, 3)
    await tasks._event(conn, row, "code", "released", "The holder is silent.")
    await tasks._event(conn, row, "person:ada", "released", "A person asks for a restart.", {"requested": True})
    fresh = await make(conn, workspace, "Fresh work", holder="agent:build")
    await stamp(conn, fresh, 1)
    await make(conn, workspace, "Other agents", holder="agent:studio")
    data = await digest_snapshot.build(conn, ada, NOW, workspace)
    assert data["your_agents"] == [{"name": "Build agents", "count": 2, "silent": [row["id"]],
                                    "restarts": 1, "restart_tasks": [row["id"]]}]
    await conn.execute("UPDATE task_events SET at=%s WHERE kind='released'", (NOW - timedelta(hours=1),))
    await conn.execute("INSERT INTO routine_state(key,last_run_at) VALUES ('digest:ada',%s)", (NOW,))
    assert (await digest_snapshot.build(conn, ada, NOW, workspace))["your_agents"][0]["restarts"] == 0


async def test_done_roles_proof_limits_and_separate_windows(conn, ada, workspace):
    for i in range(8):
        row = await make(conn, workspace, f"Result {i}", viewer=BEN, holder="agent:build",
                         links=[{"kind": "video", "url": f"https://example.com/video/{i}"}])
        await complete(conn, workspace, row, BEN)
    unrelated = await make(conn, workspace, "Other work", viewer=BEN, holder="person:ben")
    changed = workspace.model_copy(deep=True)
    changed.lists[1].reviewer = "person:ben"
    await complete(conn, changed, unrelated, BEN)
    data = await digest_snapshot.build(conn, ada, NOW, changed)
    team = await digest_snapshot.team_snapshot(conn, changed, NOW)
    assert data["finished_total"] == 8 and len(data["finished"]) == 5
    assert all(i["proof"] == ["video"] and i["done_by"] == "Ben" for i in data["finished"])
    assert team["finished_total"] == 9 and len(team["finished"]) == 7
    assert "and 3 more" in personal_text(data) and "and 2 more" in team_text(team)
    await conn.execute("INSERT INTO routine_state(key,last_run_at) VALUES ('digest:ada',%s)", (NOW,))
    assert (await digest_snapshot.build(conn, ada, NOW, changed))["finished"] == []
    assert (await digest_snapshot.team_snapshot(conn, changed, NOW))["finished_total"] == 9


async def test_done_uses_recorded_check_and_done_rule(conn, ada, workspace):
    row = await make(conn, workspace, links=[{"kind": "ticket", "url": "https://example.com/ticket/1"}])
    await complete(conn, workspace, row)
    await conn.execute("UPDATE tasks SET done_by='code' WHERE id=%s", (row["id"],))
    await conn.execute("UPDATE task_events SET data=data || '{\"checks\":[{\"ok\":true,\"text\":\"The ticket is closed.\"}]}'::jsonb WHERE task_id=%s AND kind='done'", (row["id"],))
    item = (await digest_snapshot.build(conn, ada, NOW, workspace))["finished"][0]
    assert item["proof"] == ["The ticket is closed."] and item["done_by"] == "the Done rule"


async def test_due_excludes_needs_and_closed_and_other_reviewers(conn, ada, workspace):
    held = await make(conn, workspace, "Held work", holder="person:ada", due_at=NOW - timedelta(hours=1))
    review = await make(conn, workspace, "Agent work", holder="agent:build", due_at=NOW + timedelta(hours=24))
    await make(conn, workspace, "Later work", holder="agent:build", due_at=NOW + timedelta(hours=25))
    closed = await make(conn, workspace, "Closed work", holder="person:ada", due_at=NOW)
    await complete(conn, workspace, closed)
    data = await digest_snapshot.build(conn, ada, NOW, workspace)
    assert [i["id"] for i in data["needs_you"]] == [held["id"]]
    assert [i["id"] for i in data["due"]] == [review["id"]]


async def test_team_real_stops_wait_counts_and_holder_counts(conn, workspace):
    for title, kind, values in [
        ("Failed wake", "wake_failed", {"final": True}),
        ("Silent holder", "state", {"escalated": True, "restarts": 2}),
    ]:
        row = await make(conn, workspace, title, holder="agent:build")
        await conn.execute("UPDATE tasks SET waits_for='person:ada' WHERE id=%s", (row["id"],))
        await tasks._event(conn, row, "code", kind, "The wake endpoint did not answer.", values)
        await stamp(conn, row, 10)
    unheld = await make(conn, workspace, "No holder")
    await stamp(conn, unheld, 25)
    await make(conn, workspace, "Overdue", holder="person:ben", due_at=NOW - timedelta(hours=1))
    silent = await make(conn, workspace, "Quiet work", holder="agent:build")
    await stamp(conn, silent, 50)
    await lanes.upsert(conn, source="github", external_id="old", title="Old lane", state="blocked")
    data = await digest_snapshot.team_snapshot(conn, workspace, NOW)
    assert {i["title"] for i in data["stuck"]} == {"Failed wake", "Silent holder", "No holder", "Overdue"}
    assert data["in_work"] == {"agents": 3, "people": 1}
    ada = next(p for p in data["waiting"] if p["name"] == "Ada")
    assert ada["count"] == 2 and ada["oldest_hours"] == 10
    assert ada["oldest"]["title"] in {"Failed wake", "Silent holder"}
    assert all(i["action_by"] in {"Ada", "Ben"} for i in data["stuck"])
    text = team_text(data)
    assert text.index("Waiting on people:") < text.index("Stuck:") < text.index("In work:")
    assert "no signal" not in text and "Old lane" not in text


async def test_private_tasks_and_personal_data_never_enter_team_snapshot(conn, ada, ben, workspace):
    changed = workspace.model_copy(deep=True)
    changed.lists.extend([TaskList(key="ada-private", name="Private", owner="ada"),
                          TaskList(key="ben-private", name="Private", owner="ben")])
    for person, viewer in [(ada, ADA), (ben, BEN)]:
        row = await make(conn, changed, person["name"] + " private", viewer=viewer,
                         list_key=person["key"] + "-private", holder="person:" + person["key"])
        await tasks.post(conn, row["id"], workspace=changed, viewer=viewer, question=True, text="Private question")
        await asks.create(conn, owner_person_id=person["id"], question="A personal ask")
    await sensors.error(conn, "github", "A service fault")
    personal = await digest_snapshot.build(conn, ada, NOW, changed)
    team = await digest_snapshot.team_snapshot(conn, changed, NOW)
    assert personal["needs_you"][0]["title"] == "Ada private"
    assert "Ben private" not in str(personal)
    assert personal["asks"] and personal["faults"]
    assert "private" not in str(team).lower() and "personal ask" not in str(team)
    assert "faults" not in team and not team["waiting"]
    assert not any(team["in_work"].values())
    ben_data = await digest_snapshot.build(conn, ben, NOW, changed)
    assert ben_data["faults"] == []


def test_empty_fallbacks_omit_empty_sections():
    assert personal_text({"name": "Ada"}) == "Morning, Ada. Nothing needs you."
    assert team_text({"finished_total": 0, "waiting": [], "stuck": [], "in_work": {"agents": 0, "people": 0}}) == "Morning, team."


@pytest.mark.parametrize("send_empty", [False, True])
async def test_empty_team_setting(app, fake_brain, send_empty):
    from illo.routines.team_digest import send_team_due
    from tests.asks.test_team_digest import configure

    ctx = app.state.ctx
    configure(ctx).digest.team.send_empty = send_empty
    await send_team_due(ctx, NOW + timedelta(hours=5))
    assert bool(fake_brain.requests) == send_empty


async def test_skipped_day_keeps_last_sent_window(conn, ada, workspace):
    from illo.ledger import kv

    row = await make(conn, workspace, "Result before the skipped day")
    await complete(conn, workspace, row)
    await conn.execute("UPDATE tasks SET done_at=%s WHERE id=%s", (NOW - timedelta(hours=30), row["id"]))
    await kv.set(conn, "brief:sent:digest:ada", (NOW - timedelta(hours=48)).isoformat())
    await conn.execute("INSERT INTO routine_state(key,last_run_at,last_status) VALUES ('digest:ada',%s,'skipped')",
                       (NOW - timedelta(hours=24),))
    assert (await digest_snapshot.build(conn, ada, NOW, workspace))["finished_total"] == 1


@pytest.mark.parametrize("work", ["agent", "due", "done", "non-owner fault"])
async def test_needs_mode_skips_other_sections_and_non_owner_faults(app, fake_brain, slack_client, work):
    from illo.routines.digest import send_due
    from tests.asks.test_digest import only_ada, posts

    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.digest.personal = "needs"
    await only_ada(ctx)
    async with ctx.pool.connection() as conn:
        if work == "non-owner fault":
            await conn.execute("UPDATE people SET active=(key='ben'),timezone='UTC'")
            await sensors.error(conn, "github", "Example service error")
        else:
            row = await make(conn, ctx.workspace, holder="agent:build",
                             due_at=NOW if work == "due" else None)
            if work == "done":
                await complete(conn, ctx.workspace, row)
    await send_due(ctx, NOW)
    assert not fake_brain.requests and not posts(slack_client)


@pytest.mark.parametrize("kind,render", [("personal", personal_text), ("team", team_text)])
def test_fallback_examples_match_task_sections(kind, render):
    import json
    from pathlib import Path

    root = Path(__file__).with_name("fixtures")
    data = json.loads((root / "briefs.json").read_text())[kind]
    assert render(data) + "\n" == (root / (kind + "-brief.txt")).read_text()
