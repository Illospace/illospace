"""Publish the file that the outside deadman watcher reads."""

import asyncio
import json
import os
import time
from datetime import UTC, datetime

from illo.ledger import github as ledger
from illo.ledger import sensors


async def publish(ctx, *, loop_healthy: bool = True) -> bool:
    config = ctx.workspace.config.heartbeat
    repo = os.getenv("ILLO_HEARTBEAT_REPO") or config.repo
    branch = os.getenv("ILLO_HEARTBEAT_BRANCH") or config.branch
    if not repo or not ctx.github_client or not loop_healthy:
        return False
    async with ctx.pool.connection(timeout=3) as conn:
        if (
            ctx.config.slack_app_token or ctx.config.slack_bot_token
        ) and not await ledger.slack_healthy(conn, max_age=300):
            await sensors.error(conn, "heartbeat", "The Slack connection is not healthy",
                                expected_every_s=config.seconds)
            return False
        last = await ledger.latest_run(conn)
        payload = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "last_run_id": last["id"] if last else None,
            "last_surface": "illo",
        }
    await ctx.github_client.write_file(repo, branch, "heartbeat.json", json.dumps(payload) + "\n")
    async with ctx.pool.connection() as conn:
        await sensors.ok(conn, "heartbeat", expected_every_s=config.seconds)
    return True


async def setup(ctx) -> None:
    # A separate probe detects a stalled loop before a heartbeat can mask it.
    probe_at = time.monotonic()
    probe_ok = False

    async def probe_loop():
        nonlocal probe_at, probe_ok
        now = time.monotonic()
        probe_ok = now - probe_at < 45
        probe_at = now

    async def publish_heartbeat():
        healthy = probe_ok and time.monotonic() - probe_at < 45
        if not healthy:
            # Give the probe a turn after a long stall. This publication stays skipped.
            await asyncio.sleep(0)
        await publish(ctx, loop_healthy=healthy)

    ctx.every(15, probe_loop)
    ctx.every(ctx.workspace.config.heartbeat.seconds, publish_heartbeat)
