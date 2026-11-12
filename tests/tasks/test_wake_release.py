"""Wake delivery and silence checks keep the work in motion after a commit."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from illo.channels.slack import tasks as surface
from illo.db import all_rows, one
from illo.ledger import tasks
from illo.tasks import release, wake
from illo.tasks.connectors import WakeConfig, WebhookConfig
from illo.vault import Vault
from illo.workspace import TaskList, Wake

ADA = tasks.Viewer("ada", "person:ada", human=True)
BUILD = tasks.Viewer("ada", "agent:build")


@pytest.fixture
async def ctx(app):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.lists.append(TaskList(key="tasks", name="Tasks"))
    ctx.workspace.agents[0].wake = WakeConfig(type="poll")
    return ctx


async def make(ctx, **values):
    async with ctx.pool.connection() as conn:
        return await tasks.create(conn, workspace=ctx.workspace, viewer=ADA,
            list_key="tasks", title="Repair the export", **values)


async def record(ctx, task_id):
    async with ctx.pool.connection() as conn:
        return await tasks.get(conn, task_id, viewer=ADA), await tasks.events(conn, task_id, viewer=ADA, limit=100)


async def age(ctx, task_id, hours=5):
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE tasks SET last_activity_at=now()-%s*interval '1 hour',alive_at=NULL WHERE id=%s", (hours, task_id))
        await conn.execute("UPDATE task_events SET at=now()-%s*interval '1 hour' WHERE task_id=%s", (hours, task_id))
        await conn.execute("UPDATE tasks SET holder_since=now()-interval '10 days' WHERE id=%s", (task_id,))


@pytest.mark.parametrize("kind", ["none", "poll"])
async def test_core_wakes_create_hand_reopen_and_answer(ctx, kind):
    ctx.workspace.agents[0].wake = WakeConfig(type=kind)
    row = await make(ctx)
    async with ctx.pool.connection() as conn:
        await tasks.hand(conn, row["id"], workspace=ctx.workspace, viewer=ADA, holder="agent:build")
    await wake.tick(ctx)
    async with ctx.pool.connection() as conn:
        await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=BUILD, text="Which format?", question=True)
        await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=ADA, text="Use CSV.")
        await tasks.reopen(conn, row["id"], workspace=ctx.workspace, viewer=ADA, reason="Check again.")
    await wake.tick(ctx)
    await wake.tick(ctx)
    current, events = await record(ctx, row["id"])
    assert current["waits_for"] == "agent:build"
    assert [e["kind"] for e in events].count("woken") == 3
    row = await make(ctx, holder="agent:build")
    await wake.tick(ctx)
    assert any(e["kind"] == "woken" for e in (await record(ctx, row["id"]))[1])


async def setup_webhook(ctx, status=200):
    ctx.config.vault_key = SecretStr(Fernet.generate_key().decode())
    ctx.workspace.agents[0].wake = WebhookConfig(url="https://agents.example.com/wake?private=query", secret="wake-token")
    async with ctx.pool.connection() as conn:
        await Vault(ctx.config.vault_key.get_secret_value()).set(conn, "wake-token", "example-bearer")
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(status)
    ctx.wake_transport = httpx.MockTransport(transport)
    return calls


async def test_webhook_payload_token_and_one_http_retry(ctx):
    import json

    calls = await setup_webhook(ctx, 503)
    row = await make(ctx, holder="agent:build")
    await wake.tick(ctx)
    assert len(calls) == 2
    payload = json.loads(calls[0].content)
    assert payload == {"event": "task.handed", "task": row["id"], "event_id": payload["event_id"], "text": f"Read task {row['id']}."}
    assert calls[0].headers["Authorization"] == "Bearer example-bearer"
    assert calls[0].extensions["timeout"]["read"] == 10
    calls.clear()
    ctx.wake_transport = httpx.MockTransport(lambda request: httpx.Response(204))
    await wake.tick(ctx, datetime.now(UTC)+timedelta(seconds=20))
    await wake.tick(ctx, datetime.now(UTC)+timedelta(seconds=40))
    current, events = await record(ctx, row["id"])
    assert current["waits_for"] == "agent:build"
    assert [e["kind"] for e in events].count("woken") == 1
    assert [e["kind"] for e in events].count("wake_failed") == 1


async def test_failed_wake_three_tries_then_slack_notice(ctx):
    calls = await setup_webhook(ctx, 403)
    row = await make(ctx, holder="agent:build")
    for seconds in (0, 20, 40, 60):
        await wake.tick(ctx, datetime.now(UTC)+timedelta(seconds=seconds))
    current, events = await record(ctx, row["id"])
    assert len(calls) == 6 and current["waits_for"] == "person:ada"
    failures = [e for e in events if e["kind"] == "wake_failed"]
    assert len(failures) == 3 and failures[-1]["data"]["final"]
    assert "private=query" not in str(failures) and "example-bearer" not in str(failures)
    await surface.tick(ctx)
    posts = [call["text"] for call in ctx.slack_client.calls if call["method"] == "chat_postMessage"]
    assert any("Illo could not wake Build agents. Reason:" in text for text in posts)
    assert any("Build agents" in text and "agent:build" not in text for text in posts)


async def test_webhook_all_wake_reasons_and_stable_event(ctx):
    calls = await setup_webhook(ctx)
    row = await make(ctx, holder="agent:build")
    await wake.tick(ctx)
    async with ctx.pool.connection() as conn:
        await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=ADA, text="Please check the notes.")
        await tasks.reopen(conn, row["id"], workspace=ctx.workspace, viewer=ADA, reason="Check again.")
        await tasks.wake(conn, row["id"], workspace=ctx.workspace, viewer=ADA)
    await wake.tick(ctx)
    import json
    assert [json.loads(call.content)["event"] for call in calls] == ["task.handed", "task.message", "task.reopened", "task.released"]


async def test_rollback_has_no_wake_and_delivery_sees_only_commits(ctx):
    async with ctx.pool.connection() as conn:
        with pytest.raises(RuntimeError):
            async with conn.transaction():
                await tasks.create(conn, workspace=ctx.workspace, viewer=ADA, list_key="tasks", title="A task", holder="agent:build")
                await wake.tick(ctx)
                raise RuntimeError("Roll back.")
    async with ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM task_wakes")
        assert not await all_rows(conn, "SELECT * FROM task_events")


async def test_changed_holder_cancels_old_wake(ctx):
    row = await make(ctx, holder="agent:build")
    async with ctx.pool.connection() as conn:
        await tasks.hand(conn, row["id"], workspace=ctx.workspace, viewer=ADA, holder="person:ben")
    await wake.tick(ctx)
    assert not any(e["kind"] == "woken" for e in (await record(ctx, row["id"]))[1])


async def test_unknown_wake_fails_clearly(ctx):
    ctx.workspace.agents[0].wake = Wake(type="unknown-type")
    row = await make(ctx, holder="agent:build")
    await wake.tick(ctx)
    assert "unknown-type" in (await record(ctx, row["id"]))[1][-1]["text"]


async def test_release_twice_then_escalate_with_facts(ctx):
    row = await make(ctx, holder="agent:build")
    await wake.tick(ctx)
    for _ in range(3):
        await age(ctx, row["id"])
        await release.tick(ctx)
        await wake.tick(ctx)
    current, events = await record(ctx, row["id"])
    assert current["waits_for"] == "person:ada"
    assert len([e for e in events if e["kind"] == "released"]) == 2
    assert events[-1]["text"] == "The holder is silent after 2 restarts. A person must decide."
    await surface.tick(ctx)
    assert any("Illo tried 2 restarts" in call.get("text", "") for call in ctx.slack_client.calls)
    await release.tick(ctx)
    assert len([e for e in (await record(ctx, row["id"]))[1] if e["kind"] == "released"]) == 2


async def test_question_and_liveness_prevent_release(ctx):
    row = await make(ctx, holder="agent:build")
    await wake.tick(ctx)
    await age(ctx, row["id"])
    async with ctx.pool.connection() as conn:
        before = await one(conn, "SELECT count(*) AS n FROM task_events")
        await tasks.alive(conn, row["id"], "example")
        assert (await one(conn, "SELECT count(*) AS n FROM task_events"))["n"] == before["n"]
    await release.tick(ctx)
    assert not any(e["kind"] == "released" for e in (await record(ctx, row["id"]))[1])
    async with ctx.pool.connection() as conn:
        await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=BUILD, text="Pick one.", question=True)
    await age(ctx, row["id"])
    await release.tick(ctx)
    assert not any(e["kind"] == "released" for e in (await record(ctx, row["id"]))[1])


async def test_holding_period_resets_release_cap(ctx):
    row = await make(ctx, holder="agent:build")
    await wake.tick(ctx)
    for _ in range(2):
        await age(ctx, row["id"])
        await release.tick(ctx)
        await wake.tick(ctx)
    async with ctx.pool.connection() as conn:
        await tasks.hand(conn, row["id"], workspace=ctx.workspace, viewer=ADA, holder="agent:build")
    await wake.tick(ctx)
    await release.tick(ctx)
    assert (await record(ctx, row["id"]))[0]["waits_for"] == "agent:build"


async def test_person_reminders_window_period_cap_and_summary(ctx, monkeypatch):
    monkeypatch.setattr(surface, "in_window", lambda *_: True)
    ids = [(await make(ctx, holder="person:ada"))["id"] for _ in range(8)]
    for task_id in ids:
        await age(ctx, task_id, 30)
    noon = datetime.now(UTC).replace(hour=12)
    await release.tick(ctx, noon)
    async with ctx.pool.connection() as conn:
        reminders = await all_rows(conn, "SELECT * FROM task_events WHERE kind='reminded'")
        assert len(reminders) == 5
        assert "3 more tasks wait for you" in reminders[-1]["text"]
    await surface.tick(ctx)
    assert any("<@U_ACME_ADA> This task waits for you" in call.get("text", "") for call in ctx.slack_client.calls)
    await release.tick(ctx, noon)
    async with ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT * FROM task_events WHERE kind='reminded'")) == 8
    # Every task now has a reminder in this period.
    await release.tick(ctx, noon+timedelta(hours=1))
    async with ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT * FROM task_events WHERE kind='reminded'")) == 8
    await release.tick(ctx, noon+timedelta(days=2, hours=10))
    async with ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT * FROM task_events WHERE kind='reminded'")) == 8


async def test_person_notice_on_agent_capture_and_own_person_limit(ctx):
    async with ctx.pool.connection() as conn:
        row = await tasks.create(conn, workspace=ctx.workspace, viewer=BUILD, list_key="tasks", title="Read the report", holder="person:ada")
        with pytest.raises(tasks.TaskError):
            await tasks.create(conn, workspace=ctx.workspace, viewer=BUILD, list_key="tasks", title="No", holder="person:ben")
    await surface.tick(ctx)
    assert any("You hold this task now." in call.get("text", "") for call in ctx.slack_client.calls)
    current, _ = await record(ctx, row["id"])
    assert current["holder"] == "person:ada"
    assert current["silent_hours"] == 0.0


async def test_wake_delivery_serializes_two_instances(ctx):
    import asyncio

    row = await make(ctx, holder="agent:build")
    await asyncio.gather(wake.tick(ctx), wake.tick(ctx))
    assert len([e for e in (await record(ctx, row["id"]))[1] if e["kind"] == "woken"]) == 1


async def test_release_lock_serializes_two_instances(ctx):
    import asyncio

    row = await make(ctx, holder="agent:build")
    await wake.tick(ctx)
    await age(ctx, row["id"])
    await asyncio.gather(release.tick(ctx), release.tick(ctx))
    assert len([e for e in (await record(ctx, row["id"]))[1] if e["kind"] == "released"]) == 1


async def test_question_reminder_keeps_wait_start_after_messages(ctx):
    row = await make(ctx, holder="agent:build")
    await wake.tick(ctx)
    async with ctx.pool.connection() as conn:
        await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=BUILD, text="Which date?", question=True)
    await age(ctx, row["id"], 30)
    async with ctx.pool.connection() as conn:
        # A later message from the holder does not clear the question or reset its wait.
        await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=BUILD, text="I still need the date.")
    await release.tick(ctx, datetime.now(UTC).replace(hour=12))
    assert any(e["kind"] == "reminded" and "Which date?" in e["text"] for e in (await record(ctx, row["id"]))[1])


async def test_missing_webhook_secret_and_timeout_do_not_expose_url(ctx):
    await setup_webhook(ctx)
    ctx.workspace.agents[0].wake.secret = "missing"
    row = await make(ctx, holder="agent:build")
    await wake.tick(ctx)
    assert "secret is missing" in (await record(ctx, row["id"]))[1][-1]["text"]
    ctx.workspace.agents[0].wake.secret = "wake-token"
    def unavailable(request):
        raise httpx.ReadTimeout("https://agents.example.com/wake?private=query", request=request)
    ctx.wake_transport = httpx.MockTransport(unavailable)
    await wake.tick(ctx, datetime.now(UTC)+timedelta(seconds=20))
    last = (await record(ctx, row["id"]))[1][-1]
    assert "did not answer" in last["text"] and "private=query" not in last["text"]


async def test_reminder_retry_waits_for_window_and_drops_obsolete_target(ctx, monkeypatch):
    row = await make(ctx, holder="person:ada")
    await age(ctx, row["id"], 30)
    await release.tick(ctx, datetime.now(UTC).replace(hour=12))
    monkeypatch.setattr(surface, "in_window", lambda *_: False)
    await surface.tick(ctx)
    assert not any("This task waits for you" in call.get("text", "") for call in ctx.slack_client.calls)
    async with ctx.pool.connection() as conn:
        await tasks.hand(conn, row["id"], workspace=ctx.workspace, viewer=ADA, holder="person:ben")
    monkeypatch.setattr(surface, "in_window", lambda *_: True)
    await surface.tick(ctx)
    assert not any("This task waits for you" in call.get("text", "") for call in ctx.slack_client.calls)


async def test_old_holding_period_does_not_deliver_to_the_same_holder(ctx):
    row = await make(ctx, holder="agent:build")
    async with ctx.pool.connection() as conn:
        await tasks.hand(conn, row["id"], workspace=ctx.workspace, viewer=ADA, holder="person:ada")
        await tasks.hand(conn, row["id"], workspace=ctx.workspace, viewer=ADA, holder="agent:build")
    await wake.tick(ctx)
    assert len([e for e in (await record(ctx, row["id"]))[1] if e["kind"] == "woken"]) == 1


async def test_webhook_http_logs_omit_address_and_secret(ctx, caplog):
    import logging

    await setup_webhook(ctx)
    await make(ctx, holder="agent:build")
    with caplog.at_level(logging.INFO, logger="httpx"):
        await wake.tick(ctx)
    assert "private HTTP request completed" in caplog.text
    assert "agents.example.com" not in caplog.text
    assert "private=query" not in caplog.text
    assert "example-bearer" not in caplog.text


async def test_network_wake_releases_graph_lock_and_ignores_changed_holder(ctx):
    import asyncio

    await setup_webhook(ctx)
    row = await make(ctx, holder="agent:build")
    started, finish = asyncio.Event(), asyncio.Event()

    async def transport(request):
        started.set()
        await finish.wait()
        return httpx.Response(204)

    ctx.wake_transport = httpx.MockTransport(transport)
    delivery = asyncio.create_task(wake.tick(ctx))
    try:
        await asyncio.wait_for(started.wait(), 2)
        async with ctx.pool.connection() as conn:
            lock = await one(conn, "SELECT pg_try_advisory_xact_lock(12000001) AS ok")
            assert lock["ok"]
            await tasks.hand(conn, row["id"], workspace=ctx.workspace, viewer=ADA, holder="person:ben")
    finally:
        finish.set()
        await asyncio.wait_for(delivery, 2)
    current, events = await record(ctx, row["id"])
    assert current["waits_for"] == "person:ben"
    assert not any(e["kind"] in {"woken", "wake_failed"} for e in events)


async def test_lease_prevents_duplicate_claim_and_old_result(ctx):
    row = await make(ctx, holder="agent:build")
    now = datetime.now(UTC)
    async with ctx.pool.connection() as conn:
        pending = await one(conn, "SELECT * FROM task_wakes WHERE task_id=%s", (row["id"],))
        first = await wake.claim(ctx, conn, pending["event_id"], now)
    async with ctx.pool.connection() as conn:
        assert not await wake.claim(ctx, conn, pending["event_id"], now + timedelta(seconds=30))
    async with ctx.pool.connection() as conn:
        second = await wake.claim(ctx, conn, pending["event_id"], now + timedelta(seconds=61))
    await wake.deliver(ctx, first, now)
    assert not any(e["kind"] == "woken" for e in (await record(ctx, row["id"]))[1])
    await wake.deliver(ctx, second, now + timedelta(seconds=61))
    assert len([e for e in (await record(ctx, row["id"]))[1] if e["kind"] == "woken"]) == 1


async def test_last_lost_attempt_expires_without_an_extra_network_call(ctx):
    calls = await setup_webhook(ctx)
    row = await make(ctx, holder="agent:build")
    now = datetime.now(UTC)
    async with ctx.pool.connection() as conn:
        pending = await one(conn, "UPDATE task_wakes SET attempts=3,due_at=%s WHERE task_id=%s RETURNING *", (now, row["id"]))
    await wake.tick(ctx, now)
    current, events = await record(ctx, row["id"])
    assert not calls and current["waits_for"] == "person:ada"
    assert events[-1]["kind"] == "wake_failed" and events[-1]["data"]["final"]
    async with ctx.pool.connection() as conn:
        saved = await one(conn, "SELECT * FROM task_wakes WHERE event_id=%s", (pending["event_id"],))
        assert saved["attempts"] == 3 and saved["finished_at"]


async def test_final_failure_keeps_a_new_question_recipient(ctx):
    await setup_webhook(ctx, 403)
    row = await make(ctx, holder="agent:build")
    async with ctx.pool.connection() as conn:
        await tasks.post(conn, row["id"], workspace=ctx.workspace, viewer=BUILD,
                         text="Which format?", question=True, to="person:ben")
    for seconds in (0, 20, 40):
        await wake.tick(ctx, datetime.now(UTC) + timedelta(seconds=seconds))
    assert (await record(ctx, row["id"]))[0]["waits_for"] == "person:ben"
