"""The heartbeat producer and the existing watcher use the same file contract."""

import json

import pytest
from pydantic import SecretStr

from illo.ledger import sensors
from illo.routines.heartbeat import publish
from ops.external_deadman.watcher import (
    DEFAULT_BRANCH,
    HEARTBEAT_PATH,
    DeadmanState,
    evaluate_deadman,
    parse_heartbeat,
)

REPO = "acme-example/engine"


async def test_healthy_heartbeat_matches_watcher(app, github_client, rpc_token):
    ctx = app.state.ctx
    ctx.workspace.config.heartbeat.repo = REPO
    assert await publish(ctx)
    call = github_client.calls[-1]
    assert (call["repo"], call["branch"], call["path"]) == (REPO, DEFAULT_BRANCH, HEARTBEAT_PATH)
    beat = parse_heartbeat(json.loads(call["content"]))
    assert beat.last_run_id == rpc_token[0]["id"]
    assert beat.last_surface == "illo"
    assert evaluate_deadman(beat, DeadmanState(), now=beat.ts).action == "none"


async def test_no_credentials_or_no_destination_skips(app, github_client):
    ctx = app.state.ctx
    assert not await publish(ctx)
    ctx.workspace.config.heartbeat.repo = REPO
    ctx.github_client = None
    assert not await publish(ctx)
    assert not github_client.calls


async def test_unhealthy_loop_or_slack_skips(app, github_client):
    ctx = app.state.ctx
    ctx.workspace.config.heartbeat.repo = REPO
    assert not await publish(ctx, loop_healthy=False)
    ctx.config.slack_bot_token = SecretStr("example-test-token")
    assert not await publish(ctx)
    async with ctx.pool.connection() as conn:
        await sensors.ok(conn, "slack")
    assert await publish(ctx)
    async with ctx.pool.connection() as conn:
        await sensors.error(conn, "slack", "Socket disconnected")
    github_client.calls.clear()
    assert not await publish(ctx)
    assert not github_client.calls


async def test_database_failure_never_publishes(app, github_client, monkeypatch):
    ctx = app.state.ctx
    ctx.workspace.config.heartbeat.repo = REPO

    async def fail(conn):
        raise RuntimeError("Database unavailable")

    monkeypatch.setattr("illo.routines.heartbeat.ledger.latest_run", fail)
    with pytest.raises(RuntimeError):
        await publish(ctx)
    assert not github_client.calls


async def test_heartbeat_env_overrides(app, github_client, monkeypatch):
    monkeypatch.setenv("ILLO_HEARTBEAT_REPO", REPO)
    monkeypatch.setenv("ILLO_HEARTBEAT_BRANCH", "ops/heartbeat")
    assert await publish(app.state.ctx)
    assert github_client.calls[-1]["repo"] == REPO


async def test_loop_probe_skips_publication_after_a_stall(app, github_client, monkeypatch):
    from illo.routines import heartbeat

    ctx = app.state.ctx
    ctx.workspace.config.heartbeat.repo = REPO
    now = [1000.0]
    jobs = {}
    monkeypatch.setattr(heartbeat.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ctx, "every", lambda seconds, fn: jobs.update({seconds: fn}))
    await heartbeat.setup(ctx)
    await jobs[180]()
    assert not github_client.calls
    now[0] += 15
    await jobs[15]()
    await jobs[180]()
    assert github_client.calls[-1]["method"] == "write_file"
    github_client.calls.clear()
    now[0] += 120
    await jobs[15]()
    await jobs[180]()
    assert not github_client.calls
