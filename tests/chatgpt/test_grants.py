"""Refresh, selection, notices, and the run request use fake credentials only."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from illo.brain.client import BrainEvent
from illo.brain.run_manager import credential_person, provider_auth
from illo.connectors import chatgpt
from illo.connectors.store import vault
from illo.db import one
from illo.gateway.principal import Authority
from illo.ledger import chatgpt as grants
from tests.chatgpt.conftest import save_grant


async def test_refresh_rotation_commits_before_run(chatgpt_app, replay, fake_brain):
    app, backend = chatgpt_app
    ctx = app.state.ctx
    ada = await save_grant(ctx, expires=299)
    result = await replay()
    assert result["decision"] == "run"
    assert backend.refresh_count == 1
    assert fake_brain.requests[-1].provider_auth.access_token.get_secret_value() == "fresh-access"
    async with ctx.pool.connection() as conn:
        row = await grants.load(conn, ada["id"])
        token = json.loads(vault(ctx).decrypt(row["secret"]))
        assert token["refresh_token"] == "rotated-refresh"
        assert abs(token["expires_at"] - row["expires_at"].timestamp()) < 0.000001
        assert b"rotated-refresh" not in row["secret"]


async def test_two_runs_refresh_once(chatgpt_app, replay):
    app, backend = chatgpt_app
    backend.delay = 0.05
    await save_grant(app.state.ctx, expires=-1)
    results = await asyncio.gather(replay(), replay())
    assert all(r["decision"] == "run" and "error" not in r for r in results)
    assert backend.refresh_count == 1


async def test_refresh_failure_broken_and_one_dm(chatgpt_app, replay, fake_brain, slack_client):
    app, backend = chatgpt_app
    ada = await save_grant(app.state.ctx, expires=0)
    backend.broken = True
    await asyncio.gather(replay(), replay())
    await replay()
    assert not fake_brain.requests
    assert backend.refresh_count == 1
    notices = [c for c in slack_client.calls if c.get("text") == chatgpt.BROKEN]
    assert len(notices) == 1
    async with app.state.ctx.pool.connection() as conn:
        row = await grants.load(conn, ada["id"])
        assert row["broken_at"] and row["notified_at"]
        assert (await one(conn, "SELECT detail FROM sensors WHERE key='chatgpt:ada'"))["detail"]


@pytest.mark.parametrize(
    "profile,principal,person_id,tainted,expected",
    [
        ("person_dm", "person:ben", 2, False, "ben"),
        ("agent_request", "person:ben", 2, False, "ben"),
        ("team_channel", "team", None, False, None),
        ("tainted", "team", None, True, None),
        ("tainted", "person:ben", 2, True, None),
        ("routine", "team", None, False, None),
        ("person_dm", "person:ada", 1, False, "ada"),
    ],
)
def test_credential_selection(workspace, profile, principal, person_id, tainted, expected):
    assert (
        credential_person(Authority(principal, profile, person_id, tainted), workspace) == expected
    )


async def test_missing_person_never_borrows_team_grant(chatgpt_app, replay, fake_brain):
    app, _ = chatgpt_app
    await save_grant(app.state.ctx)
    result = await replay(sender="U_ACME_BEN")
    assert result["text"] == chatgpt.MISSING
    assert not fake_brain.requests
    async with app.state.ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT 1 FROM runs")


async def test_missing_can_use_explicit_api_fallback(chatgpt_app, replay, fake_brain):
    app, _ = chatgpt_app
    app.state.ctx.workspace.config.brain.api_key_fallback = True
    await replay()
    assert fake_brain.requests[-1].provider_auth.kind == "api_key"


async def test_team_missing_notice_once_per_hour(chatgpt_app, slack_client):
    app, _ = chatgpt_app
    ctx = app.state.ctx
    authority = Authority("team", "team_channel")
    await asyncio.gather(
        provider_auth(ctx, authority, "openai_responses"),
        provider_auth(ctx, authority, "openai_responses"),
    )
    assert (
        len(
            [
                c
                for c in slack_client.calls
                if c.get("text")
                == "Illo has no model sign-in for team runs. Set brain.team_credential in illo.yaml, or allow the API key."
            ]
        )
        == 1
    )
    async with ctx.pool.connection() as conn:
        assert await one(conn, "SELECT 1 FROM sensors WHERE key='chatgpt:team'")
        await conn.execute(
            "UPDATE kv SET value=jsonb_build_object('at',clock_timestamp()-interval '61 minutes') WHERE key='chatgpt:team-notice'"
        )
    await provider_auth(ctx, authority, "openai_responses")
    assert (
        len(
            [
                c
                for c in slack_client.calls
                if c.get("text")
                == "Illo has no model sign-in for team runs. Set brain.team_credential in illo.yaml, or allow the API key."
            ]
        )
        == 2
    )


async def test_configured_team_credential(chatgpt_app):
    app, _ = chatgpt_app
    ctx = app.state.ctx
    ctx.workspace.config.brain.team_credential = "ben"
    await save_grant(ctx, "ben", access="ben-token")
    auth = await provider_auth(ctx, Authority("team", "routine"), "openai_responses")
    assert auth.access_token.get_secret_value() == "ben-token"


async def test_limit_reply_is_clear_and_run_token_revoked(chatgpt_app, replay):
    app, _ = chatgpt_app
    ctx = app.state.ctx
    await save_grant(ctx)
    reset = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())

    class LimitBrain:
        async def run(self, request):
            yield BrainEvent(
                type="error", category="limit", code="usage_limit_reached", resets_at=reset
            )

    ctx.brain_client = LimitBrain()
    result = await replay()
    assert result["text"].startswith("Your ChatGPT limit is reached. It resets ")
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM runs")
        assert row["status"] == "failed" and row["token_hash"] is None
        assert row["error"] == "The brain run failed: limit usage_limit_reached"


async def test_cancellation_does_not_lose_rotated_refresh(chatgpt_app):
    app, backend = chatgpt_app
    ctx = app.state.ctx
    person = await save_grant(ctx, expires=0)
    backend.delay = 0.1
    task = asyncio.create_task(chatgpt.access(ctx, person))
    while not backend.requests:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with ctx.pool.connection() as conn:
        row = await grants.load(conn, person["id"])
        assert json.loads(vault(ctx).decrypt(row["secret"]))["refresh_token"] == "rotated-refresh"


async def test_failed_run_does_not_rollback_rotation(chatgpt_app, replay):
    app, backend = chatgpt_app
    ctx = app.state.ctx
    person = await save_grant(ctx, expires=0)

    class FailedBrain:
        async def run(self, request):
            yield BrainEvent(type="error", category="runtime", code="server_error")

    ctx.brain_client = FailedBrain()
    await replay()
    assert backend.refresh_count == 1
    async with ctx.pool.connection() as conn:
        row = await grants.load(conn, person["id"])
        assert json.loads(vault(ctx).decrypt(row["secret"]))["refresh_token"] == "rotated-refresh"
        assert (await one(conn, "SELECT status FROM runs"))["status"] == "failed"
