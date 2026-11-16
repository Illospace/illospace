"""A wait check keeps one reminder and binds every result to the current wait."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from illo.channels.slack import tasks as surface
from illo.db import all_rows, one
from illo.deep import worker
from illo.gateway import InboundEvent
from illo.gateway.principal import Authority
from illo.ledger import kv, people, tasks
from illo.tasks import release, waits
from illo.tools._format import data_text
from illo.tools.registry import Refusal
from illo.tools.waits import Result
from illo.workspace import TaskList
from tests.tasks.test_wake_release import ADA, BUILD, age, make, record
from tests.tool_results import read_result

PROOF = "The repair is live. The release passed: https://example.com/releases/1"
ACTION = "Choose the report date. An agent cannot decide the date. The export already passes its checks."


@pytest.fixture
async def ctx(app, monkeypatch):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.lists.append(TaskList(key="tasks", name="Tasks"))
    ctx.workspace.config.tasks.reminder_window = (0, 24)
    monkeypatch.setattr(surface, "in_window", lambda *_: True)
    return ctx


async def waiting(ctx, *, holder="person:ada", **kwargs):
    row = await make(ctx, holder=holder, **kwargs)
    if holder.startswith("agent:"):
        async with ctx.pool.connection() as conn:
            await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=BUILD,
                             text="Check the result.", question=True)
    await age(ctx, row["id"], 40)
    return row


async def deep_rows(ctx):
    async with ctx.pool.connection() as conn:
        return await all_rows(conn, "SELECT * FROM deep_runs ORDER BY id")


async def result(ctx, fake_brain, outcome, message=PROOF, *, twice=False):
    fake_brain.results.clear()
    call = {"name": "wait_result", "arguments": {"outcome": outcome, "message": message}}
    fake_brain.script = [call, call] if twice else [call]
    deep = await worker.tick(ctx)
    assert deep["state"] == "done", deep
    assert read_result(fake_brain.results[0])["decision"] == "allow", fake_brain.results
    return deep


async def test_trigger_dedupe_and_lock_order(ctx, monkeypatch):
    row = await waiting(ctx)
    original = waits.start
    called = []

    async def unlocked(*args, **kwargs):
        async with ctx.pool.connection() as conn:
            # This would fail while tick holds its task lock on another connection.
            assert (await one(conn, "SELECT pg_try_advisory_xact_lock(12000001) AS ok"))["ok"]
        called.append(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(waits, "start", unlocked)
    await asyncio.wait_for(release.tick(ctx), 3)
    await release.tick(ctx)
    runs = await deep_rows(ctx)
    assert len(runs) == len(called) == 1
    assert not any(e["kind"] == "reminded" for e in (await record(ctx, row["id"]))[1])
    async with ctx.pool.connection() as conn:
        current = await one(conn, "SELECT * FROM tasks WHERE id=%s", (row["id"],))
        period = await waits.current(conn, current)
    wait = {"task_id": row["id"], "target": "person:ada", "since_event_id": period["since_event_id"], "round": 0}
    assert runs[0]["context"]["wait"] == wait
    assert runs[0]["dedupe_key"] == waits.key(wait)
    assert runs[0]["kind"] == "check" and runs[0]["deliver"] == {"none": True}
    assert "The record says: you hold it" in runs[0]["job"]


@pytest.mark.parametrize("mode", ["deep_off", "private", "wait_off", "full", "refused"])
async def test_plain_fallbacks(ctx, monkeypatch, mode):
    if mode == "private":
        ctx.workspace.lists[0:0] = [TaskList(key="private", name="Private", owner="ada")]
        async with ctx.pool.connection() as conn:
            row = await tasks.create(conn, workspace=ctx.workspace, viewer=ADA, list_key="private",
                                     title="Read the notes", holder="person:ada")
        await age(ctx, row["id"], 40)
    else:
        row = await waiting(ctx)
    if mode == "deep_off":
        ctx.workspace.config.deep.enabled = False
    if mode == "wait_off":
        ctx.workspace.config.tasks.wait_check = False
    if mode == "full":
        ctx.workspace.config.deep.max_queue = 1
        async with ctx.pool.connection() as conn:
            await waits.start(ctx, conn, kind="scout", job="Read the code.",
                              authority=Authority("team", "routine"), deliver={"none": True})
    if mode == "refused":
        async def refuse(*args, **kwargs):
            raise Refusal("The queue is full.")
        monkeypatch.setattr(waits, "start", refuse)
    await release.tick(ctx)
    await release.tick(ctx)
    events = (await record(ctx, row["id"]))[1]
    assert len([e for e in events if e["kind"] == "reminded"]) == 1
    assert not [r for r in await deep_rows(ctx) if r["kind"] == "check"]


@pytest.mark.parametrize("state", ["done", "failed", "cancelled"])
async def test_ended_without_result_keeps_reminder(ctx, state):
    row = await waiting(ctx)
    await release.tick(ctx)
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE deep_runs SET state=%s,ended_at=now()", (state,))
    await release.tick(ctx)
    assert len([e for e in (await record(ctx, row["id"]))[1] if e["kind"] == "reminded"]) == 1


async def test_two_checks_at_most_and_others_wait(ctx):
    for _ in range(4):
        await waiting(ctx)
    await asyncio.gather(release.tick(ctx), release.tick(ctx))
    runs = await deep_rows(ctx)
    assert len(runs) == 2
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE deep_runs SET state='running' WHERE id=%s", (runs[0]["id"],))
    await release.tick(ctx)
    assert len(await deep_rows(ctx)) == 2
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE deep_runs SET state='done'")
    await release.tick(ctx)
    assert len(await deep_rows(ctx)) == 4


async def test_person_result_sends_one_reminder_and_no_deep_delivery(ctx, fake_brain):
    row = await waiting(ctx)
    await release.tick(ctx)
    deep = await result(ctx, fake_brain, "person", ACTION, twice=True)
    assert deep["context"]["wait"]["applied"] == "person"
    denied = read_result(fake_brain.results[1])
    assert denied["decision"] == "deny" and "already has a result" in denied["reason"]
    assert not ctx.slack_client.calls
    await release.tick(ctx)
    await surface.tick(ctx)
    await surface.tick(ctx)
    events = (await record(ctx, row["id"]))[1]
    reminder = [e for e in events if e["kind"] == "reminded"]
    assert len(reminder) == 1 and reminder[0]["text"] == ACTION
    assert reminder[0]["data"]["source"] == "wait_check"
    assert reminder[0]["data"]["to"] == "person:ada"
    assert len([c for c in ctx.slack_client.calls if c["method"] == "chat_postMessage" and ACTION in c["text"]]) == 1


@pytest.mark.parametrize("choice", ["done", "back"])
async def test_complete_review_card_and_buttons(ctx, fake_brain, choice):
    row = await waiting(ctx, holder="agent:build")
    await release.tick(ctx)
    await result(ctx, fake_brain, "complete", PROOF + " <proof>")
    current, events = await record(ctx, row["id"])
    assert current["state"] == "review" and current["waits_for"] == "person:ada"
    returned = next(e for e in events if e["kind"] == "returned")
    assert returned["actor"] == "code" and returned["data"]["source"] == "wait_check"
    await surface.tick(ctx)
    async with ctx.pool.connection() as conn:
        current = await tasks.get(conn, row["id"], viewer=ADA)
        card = await kv.get(conn, f"task:card:{row['id']}")
        ada = await people.get(conn, "ada")
    posts = [c for c in ctx.slack_client.calls if c["method"] == "chat_postMessage" and c.get("blocks")]
    assert PROOF in str(posts[-1]["blocks"])
    assert "&lt;proof&gt;" in str(posts[-1]["blocks"])
    assert [b["text"]["text"] for b in posts[-1]["blocks"][-1]["elements"]] == ["Done", "Send back"]
    await surface.handle(ctx, ada, {"action_id": f"task:{row['id']}:{choice}",
        "channel": current["slack_channel"], "message_ts": card["ts"]})
    if choice == "back":
        event = InboundEvent(channel="slack", external_id="wait-send-back", kind="dm", sender=ada["slack_id"],
            text="Check the other format.", reply_to=current["slack_ts"],
            payload={"channel_id": current["slack_channel"], "thread_ts": current["slack_ts"]})
        assert await surface.claim_back(ctx, event, Authority("person:ada", "person_dm", ada["id"]))
    current = (await record(ctx, row["id"]))[0]
    assert current["state"] == ("done" if choice == "done" else "todo")
    assert current["waits_for"] == (None if choice == "done" else "agent:build")


@pytest.mark.parametrize("mode", ["code_rule", "already_review"])
async def test_complete_falls_back_to_person(ctx, fake_brain, mode):
    row = await waiting(ctx, holder="agent:build")
    if mode == "code_rule":
        next(e for e in ctx.workspace.lists if e.key == "tasks").done = ["link:proof"]
    else:
        async with ctx.pool.connection() as conn:
            await tasks.return_work(conn, row["id"], workspace=ctx.workspace, viewer=BUILD, message="Read the result.")
        await age(ctx, row["id"], 40)
    await release.tick(ctx)
    deep = await result(ctx, fake_brain, "complete")
    assert deep["context"]["wait"]["applied"] == "person"
    assert [e["text"] for e in (await record(ctx, row["id"]))[1] if e["kind"] == "reminded"] == [PROOF]


async def test_restart_is_bounded_and_counts_as_automatic(ctx, fake_brain):
    row = await waiting(ctx, holder="agent:build")
    await release.tick(ctx)
    await result(ctx, fake_brain, "restart")
    current, events = await record(ctx, row["id"])
    releases = [e for e in events if e["kind"] == "released"]
    assert current["waits_for"] == "agent:build" and current["stopped_at"] is None
    assert len(releases) == 1 and not releases[0]["data"].get("requested")
    assert releases[0]["data"]["source"] == "wait_check"
    async with ctx.pool.connection() as conn:
        assert await one(conn, "SELECT * FROM task_wakes WHERE event_id=%s", (releases[0]["id"],))
        await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=BUILD, text="Check again.", question=True)
    await age(ctx, row["id"], 40)
    await release.tick(ctx)
    deep = await result(ctx, fake_brain, "restart")
    assert deep["context"]["wait"]["applied"] == "person"
    events = (await record(ctx, row["id"]))[1]
    assert len([e for e in events if e["kind"] == "released"]) == 1
    assert any(e["kind"] == "reminded" for e in events)


async def test_restart_of_person_holder_becomes_reminder(ctx, fake_brain):
    row = await waiting(ctx)
    await release.tick(ctx)
    assert (await result(ctx, fake_brain, "restart"))["context"]["wait"]["applied"] == "person"
    assert any(e["kind"] == "reminded" for e in (await record(ctx, row["id"]))[1])


@pytest.mark.parametrize("change", ["closed", "new_wait", "other_person"])
async def test_stale_result_changes_nothing(ctx, fake_brain, change):
    row = await waiting(ctx, holder="agent:build")
    await release.tick(ctx)
    async with ctx.pool.connection() as conn:
        if change == "closed":
            await tasks.done(conn, row["id"], workspace=ctx.workspace, viewer=ADA)
        elif change == "other_person":
            await tasks.hand(conn, row["id"], workspace=ctx.workspace, viewer=ADA, holder="person:ben")
        else:
            await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=ADA, text="Use CSV.")
            await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=BUILD, text="Which date?", question=True)
    before = await record(ctx, row["id"])
    deep = await result(ctx, fake_brain, "complete")
    assert deep["context"]["wait"]["applied"] == "stale"
    assert read_result(fake_brain.results[0])["result"] == "The task changed. Illo made no change."
    assert await record(ctx, row["id"]) == before


@pytest.mark.parametrize("outcome", ["person", "complete"])
async def test_later_look_outside_window(ctx, fake_brain, monkeypatch, outcome):
    row = await waiting(ctx)
    await release.tick(ctx)
    await result(ctx, fake_brain, "person", ACTION)
    before = (await record(ctx, row["id"]))[1]
    monkeypatch.setattr(release, "in_window", lambda *_: False)
    await release.tick(ctx, datetime.now(UTC) + timedelta(days=3))
    await release.tick(ctx, datetime.now(UTC) + timedelta(days=3))
    runs = await deep_rows(ctx)
    assert len(runs) == 2 and runs[-1]["context"]["wait"]["round"] == 1
    await result(ctx, fake_brain, outcome)
    current, events = await record(ctx, row["id"])
    if outcome == "person":
        # The look sends nothing. The record keeps what Illo found.
        assert events[:-1] == before and current["state"] == "todo"
        assert events[-1]["kind"] == "message" and events[-1]["data"]["source"] == "wait_check"
    else:
        assert current["state"] == "review"
        assert any(e["kind"] == "returned" for e in events)


@pytest.mark.parametrize("outcome", ["person", "complete"])
async def test_plain_reminder_without_check_gets_one_silent_look(ctx, fake_brain, outcome):
    row = await waiting(ctx)
    ctx.workspace.config.tasks.wait_check = False
    await release.tick(ctx)
    before = (await record(ctx, row["id"]))[1]
    assert [e["kind"] for e in before].count("reminded") == 1 and not await deep_rows(ctx)
    ctx.workspace.config.tasks.wait_check = True
    await release.tick(ctx)
    await release.tick(ctx)
    runs = await deep_rows(ctx)
    assert len(runs) == 1 and runs[0]["context"]["wait"]["round"] == 0
    await result(ctx, fake_brain, outcome)
    current, events = await record(ctx, row["id"])
    if outcome == "person":
        # The look sends nothing. The record keeps what Illo found.
        assert events[:-1] == before and current["state"] == "todo"
        assert events[-1]["kind"] == "message" and events[-1]["data"]["source"] == "wait_check"
    else:
        assert current["state"] == "review"
    await release.tick(ctx)
    assert len(await deep_rows(ctx)) == 1


async def test_zero_days_stops_later_checks(ctx, fake_brain):
    ctx.workspace.config.tasks.recheck_days = 0
    await waiting(ctx)
    await release.tick(ctx)
    await result(ctx, fake_brain, "person")
    await release.tick(ctx, datetime.now(UTC) + timedelta(days=30))
    assert len(await deep_rows(ctx)) == 1


@pytest.mark.parametrize("failure", ["wake_failed", "state"])
async def test_failure_and_escalation_guidance_in_job_and_plain_reminder(ctx, failure):
    row = await make(ctx, holder="agent:build")
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE tasks SET waits_for='person:ada' WHERE id=%s", (row["id"],))
        await tasks._event(conn, row, "code", failure, "The agent cannot start here.",
            {"final": True, "escalated": failure == "state", "guidance": "Select an agent that works in this repository."})
    await age(ctx, row["id"], 40)
    await release.tick(ctx)
    job = (await deep_rows(ctx))[0]["job"]
    assert "The record says: The agent cannot start here. Select an agent" in job
    ctx.workspace.config.tasks.wait_check = False
    await release.tick(ctx)
    assert "Select an agent" in next(e["text"] for e in (await record(ctx, row["id"]))[1] if e["kind"] == "reminded")


async def test_wait_result_after_web_read(ctx, fake_brain, monkeypatch):
    row = await waiting(ctx)
    await release.tick(ctx)
    async def fetch(*args, **kwargs):
        return data_text("web", "The release passed.")
    monkeypatch.setattr("illo.tools.deep.fetch", fetch)
    fake_brain.script = [
        {"name": "web_fetch", "arguments": {"url": "https://example.com/release"}},
        {"name": "wait_result", "arguments": {"outcome": "complete", "message": PROOF}},
    ]
    await worker.tick(ctx)
    assert [read_result(r)["decision"] for r in fake_brain.results] == ["allow", "allow"]
    assert (await record(ctx, row["id"]))[0]["state"] == "review"


@pytest.mark.parametrize("message", [" ", "x" * 901, "word " * 91])
def test_result_message_limits(message):
    with pytest.raises(ValidationError):
        Result(outcome="person", message=message)
