"""The deep queue has one worker and one ledger run for each claimed job."""

import asyncio
from types import SimpleNamespace

import pytest

from illo.brain.client import BrainEvent
from illo.db import one
from illo.deep import start
from illo.deep.worker import tick
from illo.gateway.principal import Authority
from illo.tools._format import data_text
from illo.tools.registry import Refusal
from tests.tool_results import read_result


class Sandbox:
    def __init__(self):
        self.calls = []
        self.cleanups = []
        self.error = None
        self.timed_out = False

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            exit_code=0,
            stdout="repository text",
            stderr="warning text",
            truncated=False,
            timed_out=self.timed_out,
            duration_ms=2,
        )

    async def cleanup(self, run_id):
        self.cleanups.append(run_id)


async def queue(ctx, *, deliver=None, kind="request", dedupe_key=None, **changes):
    async with ctx.pool.connection() as conn:
        person = await one(conn, "SELECT id FROM people WHERE key='ada'")
        return await start(
            ctx,
            conn,
            kind=kind,
            job="Check the repository.",
            authority=Authority("person:ada", "person_dm", person["id"]),
            deliver=deliver or {"slack_dm": {"person_id": person["id"]}},
            dedupe_key=dedupe_key,
            **changes,
        )


async def test_queue_runs_ledger_shell_and_cleanup(app, fake_brain):
    ctx = app.state.ctx
    ctx.sandbox = sandbox = Sandbox()
    fake_brain.script = [{"name": "shell", "arguments": {"command": "rg 'model' /repos"}}]
    deep_id = await queue(ctx)
    row = await tick(ctx)
    assert row["id"] == deep_id and row["state"] == "done"
    request = fake_brain.requests[0]
    assert request.deep and request.effort == "high" and request.max_turns == 80
    assert request.timeout_s == 1200 and request.max_output_tokens == 16000
    assert not request.web_search
    assert "## This run: deep work" in request.system_prompt
    assert "• Deliver to: the person" in request.system_prompt
    result = read_result(fake_brain.results[0])
    assert result["decision"] == "allow"
    assert result["result"].count('<outside_data source="sandbox">') == 2
    assert sandbox.cleanups == [request.run_id]
    async with ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs WHERE id=%s", (request.run_id,))
        event = await one(conn, "SELECT * FROM events WHERE id=%s", (run["event_id"],))
        audit = await one(conn, "SELECT * FROM audit WHERE run_id=%s", (request.run_id,))
    assert run["status"] == "completed" and run["token_hash"] is None
    assert run["outside_read"] and not run["tainted"]
    assert event["channel"] == "deep" and event["status"] == "handled"
    assert audit["args"]["command"] == "rg 'model' /repos"
    assert "repository text" not in str(audit) and "warning text" not in str(audit)


async def test_queue_dedupe_limits_and_tainted_starters(app):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    first = await queue(ctx, dedupe_key="same")
    assert first and await queue(ctx, dedupe_key="same") is None
    ctx.workspace.config.deep.max_queue = 1
    with pytest.raises(Refusal, match="queue is full"):
        await queue(ctx)
    ctx.workspace.config.deep.max_queue = 10
    ctx.workspace.config.deep.max_per_person_per_day = 1
    with pytest.raises(Refusal, match="daily limit"):
        await queue(ctx)
    async with ctx.pool.connection() as conn:
        for changes in (
            {"tainted": True},
            {"outside_read": True},
            {"web_read": True},
            {"profile": "outsider"},
        ):
            with pytest.raises(Refusal, match="has not read outside text"):
                await start(
                    ctx,
                    conn,
                    kind="request",
                    job="Check",
                    authority={"principal": "team", "profile": "team_channel", **changes},
                    deliver={"none": True},
                )
    ctx.workspace.config.deep.enabled = False
    with pytest.raises(Refusal, match="off"):
        await queue(ctx)


async def test_workers_do_not_overlap_and_recover_lease(app):
    ctx = app.state.ctx
    ready, release = asyncio.Event(), asyncio.Event()

    class Brain:
        async def run(self, request):
            ready.set()
            await release.wait()
            yield BrainEvent(type="final", text="Ready")

    ctx.brain_client = Brain()
    first, second = await queue(ctx), await queue(ctx)
    work = asyncio.create_task(tick(ctx))
    try:
        await asyncio.wait_for(ready.wait(), 3)
        assert await tick(ctx) is None
    finally:
        release.set()
        await work
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE deep_runs SET state='running',lease_until=now()-interval '1 second' WHERE id=%s",
            (second,),
        )
    assert await tick(ctx) is None
    async with ctx.pool.connection() as conn:
        rows = await (await conn.execute("SELECT id,state FROM deep_runs ORDER BY id")).fetchall()
    assert rows == [{"id": first, "state": "done"}, {"id": second, "state": "failed"}]


@pytest.mark.parametrize("target", ["thread", "dm", "channel", "none"])
async def test_all_destinations(app, slack_client, target):
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        person = await one(conn, "SELECT id FROM people WHERE key='ada'")
        deliver = {
            "thread": {"slack_thread": {"channel_id": "C_ACME_TEAM", "thread_ts": "1.2"}},
            "dm": {"slack_dm": {"person_id": person["id"]}},
            "channel": {"slack_channel": {"channel_id": "C_ACME_TEAM"}},
            "none": {"none": True},
        }[target]
        # Team work cannot read a person's private data.
        await start(
            ctx,
            conn,
            kind="scout",
            job="Check",
            authority=Authority("team", "routine"),
            deliver=deliver,
        )
    row = await tick(ctx)
    assert row["state"] == "done"
    posts = [c for c in slack_client.calls if c["method"] == "chat_postMessage"]
    assert bool(posts) == (target != "none")
    if target == "thread":
        assert posts[0]["thread_ts"] == "1.2"
    if target == "dm":
        assert posts[0]["channel"] == "D_EXAMPLE"


async def test_failure_line_and_cleanup(app, slack_client):
    ctx = app.state.ctx
    ctx.sandbox = sandbox = Sandbox()

    class Brain:
        async def run(self, request):
            yield BrainEvent(type="error", category="timeout", code="timeout")

    ctx.brain_client = Brain()
    await queue(ctx)
    row = await tick(ctx)
    assert row["state"] == "failed"
    assert sandbox.cleanups == [row["run_id"]]
    posts = [c for c in slack_client.calls if c["method"] == "chat_postMessage"]
    assert (
        posts[0]["text"]
        == "I could not finish the deep work on this request. Reason: The run reached its time limit."
    )


@pytest.mark.parametrize("mode", ["absent", "busy", "timeout"])
async def test_shell_refusals(app, fake_brain, mode):
    ctx = app.state.ctx
    ctx.sandbox = None if mode == "absent" else Sandbox()
    if mode == "busy":
        ctx.sandbox.error = RuntimeError("PRIVATE PROVIDER ERROR")
    elif mode == "timeout":
        ctx.sandbox.timed_out = True
    fake_brain.script = [{"name": "shell", "arguments": {"command": "true"}}]
    await queue(ctx)
    await tick(ctx)
    result = read_result(fake_brain.results[0])
    assert result["decision"] == "deny"
    assert "PRIVATE" not in result["reason"]
    assert {"absent": "no sandbox", "busy": "busy", "timeout": "time limit"}[mode] in result[
        "reason"
    ]


async def test_tool_order_effects_use_persisted_flags(app, fake_brain, monkeypatch):
    ctx = app.state.ctx

    async def fetch(*args, **kwargs):
        return data_text("web", "public page")

    monkeypatch.setattr("illo.tools.deep.fetch", fetch)
    fake_brain.script = [
        {"name": "task_create", "arguments": {"title": "First", "list": "launches"}},
        {"name": "web_fetch", "arguments": {"url": "https://example.com"}},
        {"name": "task_create", "arguments": {"title": "Second", "list": "launches"}},
        {"name": "digest_snapshot", "arguments": {}},
        {"name": "web_fetch", "arguments": {"url": "https://example.com"}},
    ]
    await queue(ctx)
    row = await tick(ctx)
    assert row["state"] == "done"
    results = [read_result(r) for r in fake_brain.results]
    assert [r["decision"] for r in results] == ["allow", "allow", "deny", "allow", "deny"]
    assert "read the web" in results[2]["reason"] and "read private data" in results[4]["reason"]
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM tasks"))["n"] == 1
        run = await one(conn, "SELECT * FROM runs WHERE id=%s", (row["run_id"],))
        assert run["web_read"] and run["personal_read"] and not run["tainted"]


async def test_deep_start_keeps_channel_actor_and_provider_web_order(app, replay, fake_brain):
    ctx = app.state.ctx
    fake_brain.script = [
        {
            "name": "deep_start",
            "arguments": {"job": "Check the API config.", "reason": "The check needs research."},
        }
    ]
    await replay(
        kind="message",
        text="Check the API config.",
        payload={"channel_id": "C_ACME_TEAM", "mentioned": True, "message_ts": "10.2"},
    )
    started = read_result(fake_brain.results[0])
    assert started["decision"] == "allow" and started["result"]["queue_position"] == 1
    fake_brain.script = [
        {"name": "task_create", "arguments": {"list": "launches", "title": "First"}},
        {"name": "task_create", "arguments": {"list": "launches", "title": "Second"}},
    ]
    fake_brain.results.clear()
    original = fake_brain.run

    async def with_search(request):
        count = 0
        async for item in original(request):
            if item.type == "tool_call":
                count += 1
                if count == 2:
                    yield BrainEvent(type="web_search", call_id="ws1")
            yield item

    ctx.brain_client = SimpleNamespace(run=with_search)
    row = await tick(ctx)
    assert row["state"] == "done" and fake_brain.requests[-1].web_search
    assert [read_result(r)["decision"] for r in fake_brain.results] == ["allow", "deny"]
    async with ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs WHERE id=%s", (row["run_id"],))
        task = await one(conn, "SELECT * FROM tasks")
    assert run["principal"] == "team" and run["profile"] == "team_channel"
    assert run["web_read"] and run["usage"]["web_search_calls"] == 1
    assert task["created_by"] == "person:ada"


async def test_start_is_refused_after_outside_read(app, replay, fake_brain):
    fake_brain.script = [
        {"name": "github_get_issue", "arguments": {"repo": "acme-example/engine", "number": 1}},
        {"name": "deep_start", "arguments": {"job": "Check the config.", "reason": "Research"}},
    ]
    # A non-empty outside result sets the persisted flag, regardless of its text.
    app.state.ctx.github_client.issues = [
        {"number": 1, "title": "Test", "repo": "acme-example/engine", "body": "Check the config"}
    ]
    await replay(payload={"channel_id": "D_EXAMPLE", "message_ts": "1.1"})
    assert read_result(fake_brain.results[-1])["decision"] == "deny"
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM deep_runs"))["n"] == 0


async def test_result_delivery_retries_without_another_run(
    app, fake_brain, slack_client, monkeypatch
):
    ctx = app.state.ctx
    await queue(ctx)
    original = slack_client.chat_postMessage

    async def fail(**kwargs):
        raise RuntimeError("Slack is unavailable")

    monkeypatch.setattr(slack_client, "chat_postMessage", fail)
    row = await tick(ctx)
    assert row["state"] == "done"
    monkeypatch.setattr(slack_client, "chat_postMessage", original)
    assert await tick(ctx) is None
    async with ctx.pool.connection() as conn:
        saved = await one(conn, "SELECT * FROM deep_runs")
    assert saved["delivered_at"] and len(fake_brain.requests) == 1
    posts = [c for c in slack_client.calls if c["method"] == "chat_postMessage"]
    assert len(posts) == 1 and posts[0]["client_msg_id"]


@pytest.mark.parametrize("supports", [False, True])
async def test_hand_links_with_both_ledger_signatures(monkeypatch, supports):
    from illo.tools import tasks

    called = []

    async def viewer(ctx):
        return "viewer"

    async def hand(conn, task_id, **kwargs):
        called.append(kwargs)
        return {"id": task_id}

    async def full(*args, **kwargs):
        return {"id": 1}

    monkeypatch.setattr(tasks, "_HAND_LINKS", supports)
    monkeypatch.setattr(tasks, "run_viewer", viewer)
    monkeypatch.setattr(tasks.ledger, "hand", hand)
    monkeypatch.setattr(tasks, "full", full)
    ctx = SimpleNamespace(conn=None, app=SimpleNamespace(workspace=None))
    links = [{"kind": "proof", "url": "https://example.com/proof", "title": "Proof"}]
    await tasks.handle(ctx, "task_hand", {"id": 1, "holder": "person:ada", "links": links})
    assert ("links" in called[0]) is supports
    if supports:
        assert called[0]["links"] == links


async def test_private_channel_needs_the_original_mention(
    app, fake_brain, slack_client, monkeypatch
):
    from psycopg.types.json import Jsonb

    from illo.deep.delivery import send

    ctx = app.state.ctx

    async def private(*args, **kwargs):
        return True

    monkeypatch.setattr("illo.deep.delivery.channel_private", private)
    async with ctx.pool.connection() as conn:
        deep_id = await start(
            ctx,
            conn,
            kind="scout",
            job="Check",
            authority=Authority("team", "routine"),
            deliver={"slack_channel": {"channel_id": "C_ACME_TEAM"}},
        )
        row = await one(conn, "SELECT * FROM deep_runs WHERE id=%s", (deep_id,))
    with pytest.raises(Refusal, match="mention"):
        await send(ctx, row, "A result")
    async with ctx.pool.connection() as conn:
        event = await one(
            conn,
            "INSERT INTO events(channel,external_id,kind,payload) VALUES ('slack','mention','message',%s) RETURNING id",
            (Jsonb({"payload": {"channel_id": "C_ACME_TEAM", "mentioned": True}}),),
        )
    await send(ctx, {**row, "origin_event_id": event["id"]}, "A result")
    assert any(c["method"] == "chat_postMessage" for c in slack_client.calls)
