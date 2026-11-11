"""Only a person's own DM can change the time or stop their brief."""

from datetime import timedelta

import pytest

from illo.db import one
from illo.ledger import asks, lanes, people, runs, sensors
from illo.routines.digest import FIRST_BRIEF, send_due
from tests.asks.test_digest import NOW, only_ada, posts
from tests.tool_results import read_result


async def settings_token(
    ctx, *, principal="person:ada", profile="person_dm", channel="slack", kind="dm", tainted=False
):
    async with ctx.pool.connection() as conn:
        person = await people.get(conn, "ada")
        event = await one(
            conn,
            "INSERT INTO events (channel,external_id,kind,payload) VALUES (%s,%s,%s,'{}') RETURNING id",
            (channel, f"settings:{profile}:{principal}:{kind}", kind),
        )
        row = await runs.create(
            conn,
            principal=principal,
            profile=profile,
            person_id=person["id"] if principal.startswith("person:") else None,
            tainted=tainted,
            event_id=event["id"],
            runtime="fake",
            model="fake",
        )
        return row, await runs.mint_token(conn, row["id"], 60)


async def call_settings(rpc, token, **args):
    response = await rpc(token, "tools/call", {"name": "brief_settings", "arguments": args})
    return read_result(response.json()["result"])


async def test_settings_get_set_and_owner_binding(app, rpc):
    ctx = app.state.ctx
    _, token = await settings_token(ctx)
    got = await call_settings(rpc, token, action="get")
    assert got["decision"] == "allow"
    assert got["result"] == {
        "enabled": True,
        "time": None,
        "schedule": "0 9 * * 1-5",
        "timezone": "Europe/Paris",
    }
    changed = await call_settings(rpc, token, action="set", time="08:30", enabled=False)
    assert changed["result"] == {
        "enabled": False,
        "time": "08:30",
        "schedule": "30 8 * * 1-5",
        "timezone": "Europe/Paris",
    }
    assert (await call_settings(rpc, token, action="set", enabled=True))["result"][
        "time"
    ] == "08:30"
    assert (await call_settings(rpc, token, action="get", person="ben"))["decision"] == "deny"
    async with ctx.pool.connection() as conn:
        assert not await one(
            conn,
            "SELECT 1 FROM brief_preferences b JOIN people p ON p.id=b.person_id WHERE p.key='ben'",
        )


@pytest.mark.parametrize(
    "args",
    [
        {"action": "set"},
        {"action": "get", "enabled": False},
        {"action": "set", "time": "8:30"},
        {"action": "set", "time": "24:00"},
        {"action": "set", "time": "08:31"},
        {"action": "set", "enabled": "false"},
    ],
)
async def test_settings_reject_invalid_arguments_without_change(app, rpc, args):
    _, token = await settings_token(app.state.ctx)
    assert (await call_settings(rpc, token, **args))["decision"] == "deny"
    got = (await call_settings(rpc, token))["result"]
    assert got["enabled"] is True and got["time"] is None


@pytest.mark.parametrize(
    "profile,principal,tainted,channel,kind",
    [
        ("team_channel", "team", False, "slack", "message"),
        ("routine", "team", False, "routine", "scheduled"),
        ("agent_request", "person:ada", False, "mcp", "request"),
        ("person_dm", "person:ada", True, "slack", "dm"),
        ("person_dm", "person:ben", False, "slack", "dm"),
        ("person_dm", "person:ada", False, "email", "message"),
        ("person_dm", "person:ada", False, "routine", "scheduled"),
    ],
)
async def test_settings_scope_profile_and_dm_checks(
    app, rpc, profile, principal, tainted, channel, kind
):
    ctx = app.state.ctx
    _, token = await settings_token(
        ctx, profile=profile, principal=principal, tainted=tainted, channel=channel, kind=kind
    )
    result = await call_settings(rpc, token, action="set", enabled=False)
    assert result["decision"] == "deny"
    async with ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT 1 FROM brief_preferences")


async def test_dm_model_can_change_preferences(app, replay, fake_brain):
    fake_brain.script = [
        {"name": "brief_settings", "arguments": {"action": "set", "time": "08:30"}}
    ]
    await replay(text="send my brief at 8:30")
    result = read_result(fake_brain.results[0])
    assert result["decision"] == "allow" and result["result"]["schedule"] == "30 8 * * 1-5"


async def test_person_time_off_on_and_no_second_send_same_day(app, rpc, fake_brain, slack_client):
    ctx = app.state.ctx
    await only_ada(ctx, "America/New_York")
    _, token = await settings_token(ctx)
    await call_settings(rpc, token, action="set", time="08:30")
    due = NOW + timedelta(hours=4, minutes=30)
    await send_due(ctx, due - timedelta(minutes=1))
    assert not posts(slack_client)
    await send_due(ctx, due)
    assert len(posts(slack_client)) == 1
    await call_settings(rpc, token, action="set", time="09:00")
    await send_due(ctx, due + timedelta(minutes=30))
    assert len(posts(slack_client)) == 1
    await call_settings(rpc, token, action="set", enabled=False)
    tomorrow = due + timedelta(days=1, minutes=30)
    await send_due(ctx, tomorrow)
    assert len(posts(slack_client)) == 1
    await call_settings(rpc, token, action="set", enabled=True)
    await send_due(ctx, tomorrow + timedelta(minutes=1))
    await send_due(ctx, tomorrow + timedelta(minutes=1))
    assert len(posts(slack_client)) == 2
    assert posts(slack_client)[0]["text"].endswith(FIRST_BRIEF)
    assert FIRST_BRIEF not in posts(slack_client)[1]["text"]
    # The configured weekdays still apply after a time override.
    await send_due(ctx, tomorrow + timedelta(days=3))
    assert len(posts(slack_client)) == 2


@pytest.mark.parametrize("trigger", ["ask", "stuck", "fault"])
async def test_needs_mode_records_finished_only_day_then_sends_for_work(
    app, fake_brain, slack_client, trigger
):
    ctx = app.state.ctx
    await only_ada(ctx)
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.digest.personal = "needs"
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        row = await lanes.upsert(
            conn,
            source="github",
            external_id="closed",
            title="Closed work",
            state="done",
            owner_person_id=ada["id"],
        )
        await conn.execute(
            "UPDATE lanes SET closed_at=%s WHERE id=%s", (NOW - timedelta(hours=1), row["id"])
        )
    await send_due(ctx, NOW)
    assert not posts(slack_client) and not fake_brain.requests
    async with ctx.pool.connection() as conn:
        state = await one(conn, "SELECT * FROM routine_state WHERE key='digest:ada'")
        assert state["last_status"] == "skipped" and state["last_run_at"] == NOW
        if trigger == "ask":
            await asks.create(conn, owner_person_id=ada["id"], question="Which plan?")
        elif trigger == "stuck":
            await lanes.upsert(
                conn,
                source="manual",
                external_id="blocked",
                title="Blocked work",
                state="blocked",
                owner_person_id=ada["id"],
            )
        else:
            await sensors.error(conn, "github", "Example failure")
    await send_due(ctx, NOW + timedelta(minutes=1))
    assert not posts(slack_client)
    await send_due(ctx, NOW + timedelta(days=1))
    assert len(posts(slack_client)) == 1
    assert posts(slack_client)[0]["text"].endswith(FIRST_BRIEF)


@pytest.mark.parametrize("fallback", [True, False])
async def test_first_brief_notice_only_once(app, fake_brain, slack_client, monkeypatch, fallback):
    from illo.brain import run_manager

    ctx = app.state.ctx
    await only_ada(ctx)
    if fallback:

        async def no_auth(*args, **kwargs):
            return None

        monkeypatch.setattr(run_manager, "provider_auth", no_auth)
    await send_due(ctx, NOW)
    await send_due(ctx, NOW + timedelta(days=1))
    assert len(posts(slack_client)) == 2
    assert posts(slack_client)[0]["text"].endswith("\n\n" + FIRST_BRIEF)
    assert FIRST_BRIEF not in posts(slack_client)[1]["text"]
    if fallback:
        assert (
            "Do you want me to coordinate something today? Tell me here."
            in posts(slack_client)[0]["text"]
        )


async def test_existing_digest_state_does_not_repeat_first_notice(app, slack_client):
    ctx = app.state.ctx
    await only_ada(ctx)
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO routine_state (key,last_run_at,last_status) VALUES ('digest:ada',%s,'ok')",
            (NOW - timedelta(days=1),),
        )
    await send_due(ctx, NOW)
    assert len(posts(slack_client)) == 1 and FIRST_BRIEF not in posts(slack_client)[0]["text"]


async def test_needs_retry_uses_frozen_waiting_work(app, fake_brain, slack_client, monkeypatch):
    from illo.routines import digest

    ctx = app.state.ctx
    await only_ada(ctx)
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.digest.personal = "needs"
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        await asks.create(conn, owner_person_id=ada["id"], question="Which plan?")
    post = digest.post_dm
    attempts = []

    async def uncertain(*args):
        attempts.append(args[2:])
        if len(attempts) == 1:
            raise RuntimeError("Example send failure")
        return await post(*args)

    monkeypatch.setattr(digest, "post_dm", uncertain)
    with pytest.raises(RuntimeError):
        await send_due(ctx, NOW)
    async with ctx.pool.connection() as conn:
        await conn.execute("DELETE FROM asks")
    await send_due(ctx, NOW)
    assert attempts[0] == attempts[1] and len(posts(slack_client)) == 1
