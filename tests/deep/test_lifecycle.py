"""The shared lifecycle records setup and payer failures for deep work."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from illo.brain import run_manager
from illo.connectors import chatgpt
from illo.db import one
from illo.deep import ports
from illo.deep.worker import tick
from illo.gateway import InboundEvent
from illo.gateway.principal import Authority
from illo.gateway.replies import RunFailure
from tests.deep.test_lane import queue
from tests.tool_results import read_result


@pytest.mark.parametrize("category,reply", [
    ("no_payer", "The model service is unavailable."),
    ("sign_in", "The model service is unavailable."),
    ("inactive_person", "The deep run failed."),
    ("repo_refresh", "The model service is unavailable."),
    ("timeout", "The run reached its time limit."),
])
async def test_deep_setup_failure_finishes_one_linked_run(app, fake_brain, monkeypatch, category, reply):
    ctx = app.state.ctx
    deep_id = await queue(ctx)
    if category == "inactive_person":
        async with ctx.pool.connection() as conn:
            await conn.execute("UPDATE people SET active=false WHERE key='ada'")
    if category == "timeout":
        ctx.workspace.config.brain.routes["deep"].timeout_s = 0.2

    async def refresh(ctx):
        async with ctx.pool.connection() as conn:
            linked = await one(conn, """SELECT r.status,r.payer FROM runs r
                JOIN deep_runs d ON d.run_id=r.id AND r.deep_run_id=d.id WHERE d.id=%s""", (deep_id,))
        assert linked == {"status": "running", "payer": None}
        if category == "repo_refresh":
            raise httpx.ConnectError("Example refresh error")
        if category == "timeout":
            await asyncio.Event().wait()

    payer = AsyncMock(return_value=None, side_effect=chatgpt.TemporaryRefresh if category == "sign_in" else None)
    monkeypatch.setattr(run_manager, "select_payer", payer)
    monkeypatch.setattr(ports, "refresh_repos", refresh)
    deep = await tick(ctx)
    assert deep["id"] == deep_id and deep["state"] == "failed" and deep["error"] == reply
    assert not fake_brain.requests
    assert payer.await_count == (1 if category in {"no_payer", "sign_in"} else 0)
    async with ctx.pool.connection() as conn:
        runs = await (await conn.execute("SELECT * FROM runs")).fetchall()
        run, = runs
        event = await one(conn, "SELECT status FROM events WHERE id=%s", (run["event_id"],))
    assert run["id"] == deep["run_id"] and run["deep_run_id"] == deep_id
    assert run["route"] == "deep" and run["deep_kind"] == "request"
    assert run["status"] == ("timeout" if category == "timeout" else "failed")
    assert run["usage"]["failure_category"] == category and run["token_hash"] is None
    assert event["status"] == "failed"


async def test_sign_in_failure_raises_for_digest_and_replies_in_dm(app, replay, channel, monkeypatch):
    ctx = app.state.ctx
    payer = AsyncMock(side_effect=chatgpt.TemporaryRefresh)
    monkeypatch.setattr(run_manager, "select_payer", payer)
    async with ctx.pool.connection() as conn:
        person = await one(conn, "SELECT id FROM people WHERE key='ada'")
    event = InboundEvent(channel="digest", external_id="example", kind="scheduled")
    with pytest.raises(RunFailure) as failed:
        await ctx.gateway.run_manager.start_run(event, Authority("person:ada", "person_dm", person["id"]), None)
    assert failed.value.category == "provider"
    assert payer.await_args.kwargs["notify"] is False

    reply = await replay()
    assert payer.await_args.kwargs["notify"] is True
    assert reply["text"] == channel.final[-1][1] == chatgpt.TEMPORARY
    assert not channel.errors


async def test_deep_start_keeps_dm_authority_and_task_actor(app, replay, fake_brain):
    ctx = app.state.ctx
    fake_brain.script = [{"name": "deep_start", "arguments": {"job": "Check the plan.", "reason": "Research"}}]
    await replay(payload={"channel_id": "D_EXAMPLE", "message_ts": "1.2", "task_actor": "ada"})
    started = read_result(fake_brain.results[0])
    assert started["decision"] == "allow"
    fake_brain.script = []

    deep = await tick(ctx)
    assert deep["id"] == started["result"]["deep_run_id"] and deep["state"] == "done"
    async with ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs WHERE id=%s", (deep["run_id"],))
        event = await one(conn, "SELECT * FROM events WHERE id=%s", (run["event_id"],))
    assert run["route"] == "deep" and run["deep_run_id"] == deep["id"]
    assert run["provider"] == "fake" and run["payer"] == "api_key"
    assert (run["principal"], run["person_id"], run["profile"]) == (
        deep["principal"], deep["person_id"], deep["profile"],
    )
    assert event["principal"] == run["principal"] == "person:ada" and run["profile"] == "person_dm"
    assert event["person_id"] == run["person_id"]
    assert event["payload"]["task_actor"] == deep["context"]["task_actor"] == "ada"
