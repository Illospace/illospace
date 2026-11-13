"""One personal brief uses one snapshot and one delivery key."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from illo.brain.client import BrainEvent, ProviderAuth
from illo.db import one
from illo.gateway.principal import Authority
from illo.ledger import lanes, people
from illo.ledger.digest_snapshot import sensor_fault
from illo.routines.digest import (
    DEEP_INSTRUCTIONS,
    DEFAULT_INSTRUCTIONS,
    FIRST_BRIEF,
    fallback_text,
    send_due,
)
from illo.routines.scheduler import tick_routines
from illo.workspace import Routine
from tests.tool_results import snapshot_data

NOW = datetime(2026, 11, 10, 9, tzinfo=UTC)


def empty(**changes):
    return {
        "name": "Ada",
        "needs_you": [],
        "your_agents": [],
        "due": [],
        "asks": [],
        "finished": [],
        "finished_total": 0,
        "faults": [],
        **changes,
    }


def test_sensor_fault_rules():
    base = {"key": "asks:slack", "last_ok_at": NOW - timedelta(days=2), "last_error_at": None}
    assert sensor_fault(base, NOW) is None
    assert sensor_fault({**base, "last_ok_at": None}, NOW) is None
    failed = sensor_fault({**base, "last_error_at": NOW}, NOW)
    assert failed["since"] == NOW
    assert "asks:slack" not in failed["what"]
    assert "ask card" in failed["what"]
    assert sensor_fault({**base, "last_error_at": NOW - timedelta(days=3)}, NOW) is None
    # An event failure appears in one brief only.
    old = {**base, "last_error_at": NOW - timedelta(hours=30)}
    assert sensor_fault(old, NOW, since=NOW - timedelta(hours=24)) is None
    assert sensor_fault(old, NOW, since=NOW - timedelta(hours=48))["since"] == old["last_error_at"]
    timer = {**base, "key": "job:example", "expected_every_s": 60}
    stopped = {**timer, "last_error_at": NOW - timedelta(hours=30)}
    assert sensor_fault(stopped, NOW, since=NOW - timedelta(hours=24)) is not None
    assert sensor_fault(timer, NOW)["what"] == "One of my background jobs stopped."
    assert sensor_fault({**timer, "last_ok_at": NOW - timedelta(minutes=29)}, NOW) is None
    assert sensor_fault({**timer, "expected_every_s": 86400}, NOW) is None
    assert (
        sensor_fault({**base, "key": "chatgpt:ada", "last_error_at": NOW}, NOW, {"ada": "Ada"})[
            "what"
        ]
        == "The ChatGPT sign-in of Ada failed."
    )


async def only_ada(ctx, timezone="UTC"):
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE people SET active=(key='ada'),timezone=%s", (timezone,))


def posts(slack_client):
    return [call for call in slack_client.calls if call["method"] == "chat_postMessage"]


async def test_one_model_brief_with_digest_file_and_person_credentials(
    app, fake_brain, slack_client, monkeypatch
):
    from illo.brain import run_manager
    from illo.connectors import chatgpt

    ctx = app.state.ctx
    await only_ada(ctx, "America/New_York")
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.brain.auth = "chatgpt"
    ctx.workspace.config.brain.runtime = "openai_responses"
    ctx.workspace.config.brain.team_credential = "ben"
    owners = []

    async def access(ctx, person, **kwargs):
        owners.append(person["key"])
        return ProviderAuth(
            kind="chatgpt", access_token="example-token", account_id="example-account"
        )

    monkeypatch.setattr(chatgpt, "access", access)
    assert (
        run_manager.credential_person(
            Authority("person:ada", "person_dm", 1),
            ctx.workspace,
        )
        == "ada"
    )
    fake_brain.script = [{"name": "digest_snapshot", "arguments": {}}]
    fake_brain.final_text = "Morning, Ada. Please [review the plan](https://example.com/plan)."
    ctx.workspace.routines.append(
        Routine(
            name="daily-digest",
            schedule="* * * * *",
            principal="person:ada",
            destination="person:ada",
            body="Do not send twice.",
        )
    )
    due = NOW + timedelta(hours=5)
    await tick_routines(ctx, due)
    await asyncio.gather(send_due(ctx, due), send_due(ctx, due))
    await send_due(ctx, due + timedelta(minutes=1))
    assert owners == ["ada"]
    assert len(fake_brain.requests) == 1
    request = fake_brain.requests[0]
    assert request.user_message == DEFAULT_INSTRUCTIONS.replace("{name}", "Ada") + "\n\n" + DEEP_INSTRUCTIONS
    assert request.provider_auth.kind == "chatgpt"
    assert "Ada" in request.user_message and "{name}" not in request.user_message
    from tests.tool_results import read_result

    result = read_result(fake_brain.results[0])
    assert result["decision"] == "allow" and snapshot_data(result["result"])["needs_you"] == []
    assert len(posts(slack_client)) == 1
    assert (
        posts(slack_client)[0]["text"]
        == "Morning, Ada. Please <https://example.com/plan|review the plan>.\n\n" + FIRST_BRIEF
    )
    async with ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs WHERE id=%s", (request.run_id,))
        assert run["principal"] == "person:ada" and run["profile"] == "person_dm"
        assert run["personal_read"]
        assert not await one(conn, "SELECT 1 FROM events WHERE channel='routine'")


@pytest.mark.parametrize("failure", ["failed", "timeout", "missing", "temporary"])
async def test_fallback_on_model_failure_once(app, fake_brain, slack_client, monkeypatch, failure):
    from illo.brain import run_manager
    from illo.connectors import chatgpt

    ctx = app.state.ctx
    await only_ada(ctx)
    if failure in {"missing", "temporary"}:

        async def auth(*args, **kwargs):
            if failure == "temporary":
                raise chatgpt.TemporaryRefresh(chatgpt.TEMPORARY)
            return None

        monkeypatch.setattr(run_manager, "provider_auth", auth)
    else:

        async def broken(request):
            yield BrainEvent(type="started")
            if failure == "timeout":
                raise TimeoutError()
            raise RuntimeError("Example provider failure")

        monkeypatch.setattr(fake_brain, "run", broken)
    await send_due(ctx, NOW)
    await send_due(ctx, NOW)
    assert len(posts(slack_client)) == 1
    assert posts(slack_client)[0]["text"] == fallback_text(empty()) + "\n\n" + FIRST_BRIEF


async def test_brief_queue_timeout_uses_fallback(app, slack_client, monkeypatch):
    ctx = app.state.ctx
    await only_ada(ctx)
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.brain.deep.timeout_s = 0.01

    async def wait(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(ctx.gateway.run_manager, "execute", wait)
    await send_due(ctx, NOW)
    await send_due(ctx, NOW)
    assert len(posts(slack_client)) == 1
    assert posts(slack_client)[0]["text"] == fallback_text(empty()) + "\n\n" + FIRST_BRIEF


async def test_person_zone_reload_does_not_repeat_brief(app, fake_brain, slack_client):
    ctx = app.state.ctx
    await only_ada(ctx)
    fake_brain.script = [{"name": "digest_snapshot", "arguments": {}}]
    await send_due(ctx, NOW)
    await only_ada(ctx, "America/New_York")
    await send_due(ctx, NOW + timedelta(hours=5))
    assert len(posts(slack_client)) == 1
    await send_due(ctx, NOW + timedelta(days=1, hours=5))
    assert len(posts(slack_client)) == 2


async def test_brief_cannot_send_through_a_tool(app, fake_brain, slack_client):
    from tests.tool_results import read_result

    ctx = app.state.ctx
    await only_ada(ctx)
    fake_brain.script = [
        {"name": "slack_send_dm", "arguments": {"person": "ada", "text": "Extra message"}}
    ]
    await send_due(ctx, NOW)
    assert read_result(fake_brain.results[0])["decision"] == "deny"
    assert len(posts(slack_client)) == 1


async def test_tool_scope_and_snapshot_agree(app, rpc, rpc_token, monkeypatch):
    from illo.ledger import tasks
    from tests.tool_results import read_result

    class Clock:
        @staticmethod
        def now(tz):
            return NOW

    monkeypatch.setattr("illo.tools.digest.datetime", Clock)
    _, token = rpc_token
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        viewer = tasks.Viewer("ada", "person:ada", human=True)
        for i in range(21):
            row = await tasks.create(conn, workspace=ctx.workspace, viewer=viewer,
                list_key="launches", title="Complete example title " * 20, holder="person:ada")
            await tasks.done(conn, row["id"], workspace=ctx.workspace, viewer=viewer)
            await conn.execute("UPDATE tasks SET done_at=%s WHERE id=%s", (NOW - timedelta(hours=1), row["id"]))
    results = []
    for name in ("digest_snapshot", "asks_digest"):
        response = await rpc(token, "tools/call", {"name": name, "arguments": {}})
        result = read_result(response.json()["result"])
        assert result["decision"] == "allow"
        results.append(snapshot_data(result["result"]))
    assert results[0] == results[1]
    assert len(results[0]["finished"]) == 5 and results[0]["finished_total"] == 21
    assert all(item["title"] == "Complete example title " * 20 for item in results[0]["finished"])
    for name, args in (("digest_snapshot", {"person": "ben"}), ("lanes_attention", {})):
        response = await rpc(token, "tools/call", {"name": name, "arguments": args})
        assert read_result(response.json()["result"])["decision"] == "deny"


@pytest.mark.parametrize("model", [True, False])
async def test_delivery_retry_keeps_key_text_and_snapshot(
    app, fake_brain, slack_client, seed, monkeypatch, model
):
    from illo.brain import run_manager
    from illo.routines import digest

    ctx = app.state.ctx
    await only_ada(ctx)
    fake_brain.script = [{"name": "digest_snapshot", "arguments": {}}]
    if not model:

        async def no_auth(*args, **kwargs):
            return None

        monkeypatch.setattr(run_manager, "provider_auth", no_auth)
    send = digest.post_dm
    attempts = []

    async def post(ctx, person, text, blocks, kind, key):
        attempts.append((text, kind, key))
        if len(attempts) == 1:
            raise RuntimeError("Example send failure")
        return await send(ctx, person, text, blocks, kind, key)

    monkeypatch.setattr(digest, "post_dm", post)
    with pytest.raises(RuntimeError):
        await send_due(ctx, NOW)
    await seed()
    await send_due(ctx, NOW)
    await send_due(ctx, NOW)
    assert attempts[0] == attempts[1]
    assert attempts[0][1] == "digest"
    assert len(fake_brain.requests) == (1 if model else 0)
    assert len(posts(slack_client)) == 1


async def test_custom_instructions_name_and_frozen_snapshot(
    app, fake_brain, slack_client, monkeypatch
):
    ctx = app.state.ctx
    await only_ada(ctx)
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.digest_instructions = "Write a brief for {name}. Call digest_snapshot."
    fake_brain.script = [{"name": "digest_snapshot", "arguments": {}}]
    original = fake_brain.run

    async def change_after_snapshot(request):
        async with ctx.pool.connection() as conn:
            ada = await people.get(conn, "ada")
            await lanes.upsert(
                conn,
                source="manual",
                external_id="new-review",
                title="New review",
                state="review",
                owner_person_id=ada["id"],
            )
        async for item in original(request):
            yield item

    monkeypatch.setattr(fake_brain, "run", change_after_snapshot)
    await send_due(ctx, NOW)
    from tests.tool_results import read_result

    assert snapshot_data(read_result(fake_brain.results[0])["result"])["needs_you"] == []
    assert fake_brain.requests[0].user_message == "Write a brief for Ada. Call digest_snapshot.\n\n" + DEEP_INSTRUCTIONS
    assert len(posts(slack_client)) == 1


@pytest.mark.parametrize(
    "profile,tainted",
    [("routine", False), ("team_channel", False), ("agent_request", False), ("person_dm", True)],
)
async def test_snapshot_personal_scope(app, rpc, profile, tainted):
    from illo.ledger import runs
    from tests.tool_results import read_result

    async with app.state.ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        run = await runs.create(
            conn,
            principal="person:ada",
            person_id=ada["id"],
            profile=profile,
            tainted=tainted,
            runtime="fake",
            model="fake",
        )
        token = await runs.mint_token(conn, run["id"], 60)
    response = await rpc(token, "tools/call", {"name": "digest_snapshot", "arguments": {}})
    assert read_result(response.json()["result"])["decision"] == "deny"


async def test_repeated_report_keeps_the_first_closing_time(app, seed):
    """A restart reports each closed thread again. The brief must not count it as new."""
    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        base = {
            "source": "t3",
            "external_id": "closed-1",
            "title": "Example",
            "owner_person_id": ada["id"],
        }
        first = await lanes.upsert(conn, **base, state="done")
        await conn.execute(
            "UPDATE lanes SET closed_at=%s WHERE id=%s", (NOW - timedelta(days=2), first["id"])
        )
        again = await lanes.upsert(conn, **base, state="done")
        assert again["closed_at"] == NOW - timedelta(days=2)
        failed = await lanes.upsert(conn, **base, state="failed")
        assert failed["closed_at"] == NOW - timedelta(days=2)
        opened = await lanes.upsert(conn, **base, state="working")
        assert opened["closed_at"] is None
        closed = await lanes.upsert(conn, **base, state="done")
        assert closed["closed_at"] > NOW - timedelta(days=2)
