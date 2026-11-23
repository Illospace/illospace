"""Principal resolution and dedupe belong to the gateway, before the model."""

import asyncio
import uuid

import pytest

from illo.brain.client import BrainEvent
from illo.db import one
from illo.gateway import InboundEvent
from illo.gateway.principal import resolve
from illo.ledger import lanes, people, runs, tokens
from tests.tool_results import read_result


async def test_known_dm_end_to_end(app, replay, channel, fake_brain):
    async with app.state.ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        await lanes.upsert(
            conn,
            source="t3",
            external_id="thread-example",
            title="Fix a test",
            state="working",
            owner_person_id=ada["id"],
        )
    result = await replay(external_id="dm-e2e")
    assert result["decision"] == "run"
    assert len(channel.working) == 1
    assert channel.final[0][1] == "The lane is working."
    assert not channel.errors
    assert "Fix a test" in read_result(fake_brain.results[0])["result"]
    request = fake_brain.requests[0]
    async with app.state.ctx.pool.connection() as conn:
        row = await runs.get(conn, request.run_id)
        assert row["status"] == "completed" and row["principal"] == "person:ada"
        assert await runs.verify_token(conn, request.mcp_token) is None
        assert (await one(conn, "SELECT status FROM events WHERE external_id='dm-e2e'"))[
            "status"
        ] == "handled"
        assert (
            await one(conn, "SELECT tool,decision FROM audit WHERE run_id=%s", (request.run_id,))
        ) == {"tool": "lanes_search", "decision": "allow"}


async def test_gateway_dedupe(app, replay, channel, fake_brain):
    first, second = await asyncio.gather(
        replay(external_id="duplicate"), replay(external_id="duplicate")
    )
    assert sorted([first["decision"], second["decision"]]) == ["ignore", "run"]
    assert len(channel.final) == 1
    assert len(fake_brain.requests) == 1


async def test_unknown_dm_no_authority_no_answer(replay, channel, fake_brain):
    result = await replay(sender="U_UNKNOWN")
    assert result["decision"] == "ignore"
    assert not channel.working and not channel.final and not channel.errors
    assert not fake_brain.requests


@pytest.mark.parametrize(
    ("event", "principal", "profile", "tainted"),
    [
        (
            {"channel": "slack", "kind": "dm", "sender": "U_ACME_ADA"},
            "person:ada",
            "person_dm",
            False,
        ),
        (
            {"channel": "slack", "kind": "message", "sender": "U_ACME_ADA", "payload": {"channel_id": "C_ACME_ALERTS"}},
            "team",
            "team_channel",
            False,
        ),
        (
            {
                "channel": "slack",
                "kind": "message",
                "payload": {"channel_id": "C_ACME_ALERTS", "bot": True},
            },
            "team",
            "tainted",
            True,
        ),
        (
            {
                "channel": "slack",
                "kind": "message",
                "payload": {"channel_id": "C_ACME_ALERTS", "form": True},
            },
            "team",
            "tainted",
            True,
        ),
        (
            {"channel": "email", "kind": "message", "sender": "ada@example.com"},
            "person:ada",
            "person_dm",
            False,
        ),
        (
            {
                "channel": "email",
                "kind": "message",
                "sender": "ada@example.com",
                "payload": {"forwarded": True},
            },
            "person:ada",
            "tainted",
            True,
        ),
        (
            {"channel": "email", "kind": "message", "sender": "outsider@example.net"},
            "outsider",
            "tainted",
            True,
        ),
        ({"channel": "signals", "kind": "signal"}, "team", "tainted", True),
    ],
)
async def test_principal_matrix(conn, workspace, event, principal, profile, tainted):
    authority = await resolve(conn, InboundEvent(external_id=uuid.uuid4().hex, **event), workspace)
    assert (authority.principal, authority.profile, authority.tainted) == (
        principal,
        profile,
        tainted,
    )


async def test_agent_principal_comes_from_client_token(conn, workspace, ada):
    token = await tokens.create(conn, ada["id"], "machine")
    event = InboundEvent(
        channel="mcp",
        kind="request",
        external_id="agent-1",
        sender=token,
        payload={"principal": "person:ben"},
    )
    assert (await resolve(conn, event, workspace)).principal == "person:ada"
    await tokens.revoke(conn, 1)
    assert await resolve(conn, event, workspace) is None


async def test_channel_listen_and_off(conn, workspace):
    event = InboundEvent(
        channel="slack", kind="message", sender="U_ACME_ADA", external_id="x", payload={"channel_id": "C_ACME_TEAM"}
    )
    assert await resolve(conn, event, workspace) is None
    event.payload["mentioned"] = True
    assert (await resolve(conn, event, workspace)).principal == "team"
    off = workspace.model_copy(deep=True)
    off.config.slack.channels[0].mode = "off"
    assert await resolve(conn, event, off) is None


async def test_direct_handler_skips_brain(app, replay, fake_brain, channel):
    async def direct(ctx, event, authority):
        assert authority.principal == "person:ada"
        return "The action is complete."

    app.state.ctx.gateway.register_direct("slack", "dm", direct)
    result = await replay()
    assert result["decision"] == "direct"
    assert channel.final[0][1] == "The action is complete."
    assert not fake_brain.requests


async def test_timeout_finishes_and_revokes_token(app, replay, channel):
    class SlowBrain:
        async def run(self, request):
            self.request = request
            yield BrainEvent(type="started")
            await asyncio.sleep(1)

    brain = SlowBrain()
    app.state.ctx.brain_client = brain
    app.state.ctx.workspace.config.brain.timeout_s = 0.01
    result = await replay()
    assert result.get("error") and channel.errors
    async with app.state.ctx.pool.connection() as conn:
        assert (await runs.get(conn, brain.request.run_id))["status"] == "timeout"
        assert await runs.verify_token(conn, brain.request.mcp_token) is None


async def test_brain_failure_finishes_run(app, replay, channel):
    class FailedBrain:
        async def run(self, request):
            yield BrainEvent(type="error", text="Do not expose this detail")

    app.state.ctx.brain_client = FailedBrain()
    result = await replay()
    assert result.get("error") and channel.errors
    assert "Do not expose" not in str(channel.errors)
    async with app.state.ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs")
        assert run["status"] == "failed" and run["token_hash"] is None


async def test_turn_limit_finishes_run(app, replay):
    class ManyTurns:
        async def run(self, request):
            yield BrainEvent(type="tool_call")
            yield BrainEvent(type="tool_call")
            yield BrainEvent(type="final", text="This must not be sent")

    app.state.ctx.brain_client = ManyTurns()
    app.state.ctx.workspace.config.brain.max_turns = 1
    assert (await replay()).get("error")


async def test_first_working_message_precedes_the_brain(app, replay, channel):
    class CheckBrain:
        async def run(self, request):
            assert len(channel.working) == 1
            yield BrainEvent(type="final", text="Done.")

    app.state.ctx.brain_client = CheckBrain()
    assert (await replay())["text"] == "Done."


async def test_run_limit_serializes_brain_runs(app, replay, channel):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_working = asyncio.Event()

    class LimitedBrain:
        calls = 0
        active = 0
        max_active = 0

        async def run(self, request):
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.calls == 1:
                first_started.set()
                await release_first.wait()
            self.active -= 1
            yield BrainEvent(type="final", text="Done")

    original = channel.show_working

    async def working(event):
        await original(event)
        if len(channel.working) == 2:
            second_working.set()

    channel.show_working = working
    brain = LimitedBrain()
    app.state.ctx.brain_client = brain
    app.state.ctx.workspace.config.run_limits.max_concurrent = 1
    first = asyncio.create_task(replay())
    await asyncio.wait_for(first_started.wait(), 2)
    second = asyncio.create_task(replay())
    await asyncio.wait_for(second_working.wait(), 2)
    assert brain.calls == 1
    release_first.set()
    results = await asyncio.gather(first, second)
    assert all(result.get("text") == "Done" for result in results)
    assert brain.calls == 2 and brain.max_active == 1


@pytest.mark.parametrize("tainted", [False, True])
async def test_g1_limited_answer_keeps_token_until_the_final_request(app, replay, tainted):
    import httpx

    ctx = app.state.ctx
    results = []

    class LimitedBrain:
        async def run(self, request):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                for _ in range(2):
                    yield BrainEvent(type="tool_call")
                    response = await client.post(request.mcp_url, headers={
                        "Authorization": "Bearer " + request.mcp_token,
                    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                        "name": "github_search_issues", "arguments": {"query": "release"},
                    }})
                    assert response.status_code == 200
                    results.append(response.json()["result"])
            async with ctx.pool.connection() as conn:
                assert await runs.verify_token(conn, request.mcp_token)
            yield BrainEvent(type="final", text="I checked one source.", usage={"limited": True})

    ctx.brain_client = LimitedBrain()
    ctx.workspace.config.brain.max_turns = 1
    changes = ({"kind": "channel", "payload": {"channel_id": "C_ACME_ALERTS", "bot": True}}
               if tainted else {})
    result = await replay(**changes)
    assert result["text"] == "I checked one source."
    assert not results[0]["isError"] and results[1]["isError"]
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM runs")
        assert (await one(conn, "SELECT count(*) AS n FROM audit WHERE decision='allow'"))["n"] == 1
        assert (await one(conn, "SELECT count(*) AS n FROM audit WHERE decision='deny'"))["n"] == 1
    assert row["status"] == "completed" and row["usage"]["limited"] is True
    assert row["token_hash"] is None and row["tainted"] == tainted
