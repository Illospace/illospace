import asyncio

import pytest

from illo.channels.mcp import submissions
from illo.db import one
from illo.ledger import mcp, signals
from tests.mcp.client import AgentClient


async def test_concurrent_saves_create_one_item_and_receipt(app, http):
    agent = await AgentClient.create(app, http)
    args = {
        "message": "The cobalt decision.",
        "desired_outcome": "preserve_knowledge",
        "idempotency_key": "same-key",
    }
    results = await asyncio.gather(*(agent.call("illo_submit", args) for _ in range(6)))
    assert len({r["event_id"] for r in results}) == 1
    assert sum(not r["idempotent_replay"] for r in results) == 1
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM items WHERE kind='saved'"))["n"] == 1
        assert (await one(conn, "SELECT count(*) AS n FROM audit"))["n"] == 6


async def test_idempotency_key_belongs_to_one_token(app, http):
    agent = await AgentClient.create(app, http)
    other_token = await AgentClient.create(app, http)
    args = {
        "message": "The cobalt decision.",
        "desired_outcome": "preserve_knowledge",
        "idempotency_key": "same-key",
    }
    first, second = await asyncio.gather(
        agent.call("illo_submit", args), other_token.call("illo_submit", args)
    )
    assert first["event_id"] != second["event_id"]


async def test_save_and_receipt_rollback_together(app, http, monkeypatch):
    agent = await AgentClient.create(app, http)

    async def fail(*args, **kwargs):
        raise RuntimeError("A fake receipt failure")

    monkeypatch.setattr(submissions.ledger, "finish_submission", fail)
    await agent.call(
        "illo_submit",
        {"message": "The cobalt decision.", "desired_outcome": "preserve_knowledge"},
        error=True,
    )
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM items WHERE kind='saved'"))["n"] == 0
        assert (await one(conn, "SELECT count(*) AS n FROM submissions"))["n"] == 0
        assert (await one(conn, "SELECT decision FROM audit"))["decision"] == "deny"


async def test_concurrent_ask_and_lane_reports_are_idempotent(app, http):
    from tests.mcp.test_asks import ASK

    agent = await AgentClient.create(app, http)
    created = await asyncio.gather(*(agent.call("ask_create", ASK) for _ in range(4)))
    assert len({r["ask_id"] for r in created}) == 1
    other = await AgentClient.create(app, http, person="ben")
    lane = {"source": "t3", "external_id": "acme-2", "state": "working"}
    results = await asyncio.gather(agent.call("lane_report", lane), other.call("lane_report", lane))
    assert results[0] == results[1]
    async with app.state.ctx.pool.connection() as conn:
        assert (
            await one(
                conn,
                "SELECT count(*) AS n FROM lane_events WHERE lane_id=%s",
                (results[0]["lane_id"],),
            )
        )["n"] == 1


async def test_restart_marks_interrupted_requests_failed_without_repeating_writes(app, http):
    agent = await AgentClient.create(app, http)
    envelope = {"desired_outcome": "request", "source": {"source_tool": "codex"}}
    async with app.state.ctx.pool.connection() as conn:
        row, _ = await mcp.reserve(
            conn, agent.token_id, "interrupted-event", None, envelope, "digest"
        )
        await mcp.finish_submission(
            conn, row["event_id"], submissions.receipt(row["event_id"]), "accepted"
        )
        await mcp.recover_submissions(conn)
    result = await agent.call("illo_get_result", {"event_id": row["event_id"]})
    assert result["terminal"] and result["status"] == "failed"
    assert result["failure"]["category"] == "interrupted"
    assert result["mutated_target_refs"] == []


async def test_signal_receipt_can_recover_after_intake_restart(app, http):
    from illo.channels.signals import event

    agent = await AgentClient.create(app, http)
    async with app.state.ctx.pool.connection() as conn:
        row = await signals.insert(
            conn,
            source="acme-monitor",
            dedupe_key="acme-1",
            severity="warning",
            title="An alert",
            body="A failure.",
        )
        pending = await mcp.pending_signals(conn)
        assert [p["id"] for p in pending] == [row["id"]]
    await app.state.ctx.gateway.receive(event(pending[0]))
    async with app.state.ctx.pool.connection() as conn:
        assert await mcp.pending_signals(conn) == []
    await agent.settle()


@pytest.mark.parametrize(
    "name,args",
    [
        (
            "illo_read",
            {
                "capability": "knowledge.search",
                "arguments": {"query": "cobalt", "principal": "person:ben"},
            },
        ),
        ("ask_ack", {"ask_id": -1}),
        ("ask_cancel", {}),
        ("ask_pull", {"scope": "all"}),
        ("lane_report", {"lanes": [], "source": "t3"}),
    ],
)
async def test_invalid_fields_cannot_expand_authority(app, http, name, args):
    agent = await AgentClient.create(app, http)
    await agent.call(name, args, error=True)
