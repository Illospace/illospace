"""Check job alerts and recovery against Postgres."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from illo.db import create_pool
from illo.ledger import kv, sensors
from illo.ledger.jobs import report_time, silent_jobs
from illo.routines import watchdog
from illo.routines.scheduler import Scheduler

JOB = "job:acme.example.check_briefs"


async def add_sensor(conn, key, last_ok=None, last_error=None, interval=30):
    await conn.execute(
        "INSERT INTO sensors (key,last_ok_at,last_error_at,expected_every_s) VALUES (%s,%s,%s,%s)",
        (key, last_ok, last_error, interval),
    )


def connect(pool):
    return AsyncConnection.connect(pool.conninfo, autocommit=True, row_factory=dict_row)


def posts(slack_client):
    return [call["text"] for call in slack_client.calls if call["method"] == "chat_postMessage"]


async def test_alert_and_recovery_go_once_to_each_active_owner(app, slack_client):
    ctx = app.state.ctx
    now = datetime.now(UTC)
    since = now - timedelta(minutes=11)
    ctx.scheduler.started_at = since - timedelta(minutes=1)
    ctx.scheduler.names.add(JOB)
    async with await connect(ctx.pool) as conn:
        await conn.execute("UPDATE people SET role='owner',active=(key<>'chloe')")
        await add_sensor(conn, JOB, since)
        await watchdog.check(ctx, conn, now)
        await watchdog.check(ctx, conn, now)
        job = JOB.removeprefix("job:")
        assert posts(slack_client) == [
            f"A background job stopped: `{job}`. It has not reported since {report_time(since)}. "
            "Work that needs this job can wait too, such as the briefs. "
            "Run `illo doctor` on the Illo host to see the silent jobs."
        ] * 2
        assert await kv.get(conn, "watchdog:" + JOB) == {
            "since": since.isoformat(), "notified_at": now.isoformat(),
        }
        await sensors.ok(conn, JOB)
        recovered = now + timedelta(minutes=1)
        await watchdog.check(ctx, conn, recovered)
        await watchdog.check(ctx, conn, recovered)
        assert posts(slack_client)[2:] == [
            f"The background job `{job}` works again. "
            f"It was silent from {report_time(since)} to {report_time(recovered)}."
        ] * 2
        assert [call["users"] for call in slack_client.calls if call["method"] == "conversations_open"] == [
            "U_ACME_ADA", "U_ACME_BEN", "U_ACME_ADA", "U_ACME_BEN",
        ]
        assert await kv.get(conn, "watchdog:" + JOB) is None


async def test_watchdog_loop_works_while_job_holds_the_pool(pool, workspace, slack_client, monkeypatch):
    entered, blocked, notified = asyncio.Event(), asyncio.Event(), asyncio.Event()
    post = slack_client.chat_postMessage

    async def notify(**kwargs):
        result = await post(**kwargs)
        notified.set()
        return result

    monkeypatch.setattr(watchdog, "CHECK_S", 0.01)
    monkeypatch.setattr(slack_client, "chat_postMessage", notify)
    job_pool = create_pool(pool.conninfo, max_size=1)
    await job_pool.open(wait=True)
    ctx = SimpleNamespace(pool=job_pool, slack_client=slack_client)
    ctx.scheduler = Scheduler(ctx)

    async def hung_job():
        async with job_pool.connection():
            entered.set()
            await blocked.wait()

    try:
        since = datetime.now(UTC) - timedelta(minutes=11)
        ctx.scheduler.started_at = since
        key = f"job:{hung_job.__module__}.{hung_job.__name__}"
        async with job_pool.connection() as conn:
            await workspace.sync_people(conn)
            await add_sensor(conn, key, since)
        task = ctx.scheduler.every(0.01, hung_job)
        async with asyncio.timeout(5):
            await entered.wait()
            await watchdog.setup(ctx)
            await notified.wait()
        assert not task.done()
        assert len(posts(slack_client)) == 1
        assert f"`{key.removeprefix('job:')}`" in posts(slack_client)[0]
    finally:
        await ctx.scheduler.close()
        await job_pool.close()


async def test_check_respects_the_limit_and_startup_grace(app, slack_client):
    ctx = app.state.ctx
    now = datetime.now(UTC)
    ctx.scheduler.names.add(JOB)
    ctx.scheduler.started_at = now - timedelta(minutes=10)
    async with await connect(ctx.pool) as conn:
        await add_sensor(conn, JOB, now - timedelta(hours=1))
        await watchdog.check(ctx, conn, now)
        ctx.scheduler.started_at -= timedelta(hours=1)
        await conn.execute("UPDATE sensors SET last_error_at=%s WHERE key=%s", (now - timedelta(minutes=9), JOB))
        await watchdog.check(ctx, conn, now)
        assert not posts(slack_client)


async def test_silent_job_reader_uses_intervals_and_skips_other_sensors(conn):
    now = datetime.now(UTC)
    since = now - timedelta(minutes=30)
    await add_sensor(conn, JOB, last_error=since, interval=120)
    await add_sensor(conn, "job:acme.example.slow", since, interval=240)
    await add_sensor(conn, "job:acme.example.no_interval", since, interval=None)
    await add_sensor(conn, "job:acme.example.no_report")
    await add_sensor(conn, "acme.example.integration", since)
    assert await silent_jobs(conn, now) == [
        {"key": JOB, "job": JOB.removeprefix("job:"), "since": since, "limit_s": 1200}
    ]
    assert await silent_jobs(conn, now, names=set()) == []
    assert await silent_jobs(conn, now, names={"job:acme.example.no_report"}, started_at=since) == [
        {"key": "job:acme.example.no_report", "job": "acme.example.no_report", "since": since, "limit_s": 600}
    ]


async def test_concurrent_checks_claim_one_alert_and_one_recovery(app, slack_client):
    ctx = app.state.ctx
    now = datetime.now(UTC)
    ctx.scheduler.started_at = now - timedelta(minutes=11)
    ctx.scheduler.names.add(JOB)
    async with await connect(ctx.pool) as first, await connect(ctx.pool) as second:
        await add_sensor(first, JOB, ctx.scheduler.started_at)
        await asyncio.gather(watchdog.check(ctx, first, now), watchdog.check(ctx, second, now))
        assert len(posts(slack_client)) == 1
        await sensors.ok(first, JOB)
        later = now + timedelta(minutes=1)
        await asyncio.gather(watchdog.check(ctx, first, later), watchdog.check(ctx, second, later))
        assert len(posts(slack_client)) == 2


async def test_only_an_undelivered_alert_retries(app, slack_client, monkeypatch, caplog):
    ctx = app.state.ctx
    now = datetime.now(UTC)
    ctx.scheduler.started_at = now - timedelta(minutes=11)
    ctx.scheduler.names.add(JOB)
    post = slack_client.chat_postMessage

    async def fail(**kwargs):
        raise RuntimeError("Private details")

    async with await connect(ctx.pool) as conn:
        await add_sensor(conn, JOB, ctx.scheduler.started_at)
        monkeypatch.setattr(slack_client, "chat_postMessage", fail)
        await watchdog.check(ctx, conn, now)
        assert await kv.get(conn, "watchdog:" + JOB) is None
        monkeypatch.setattr(slack_client, "chat_postMessage", post)
        await watchdog.check(ctx, conn, now)
        assert len(posts(slack_client)) == 1
        await sensors.ok(conn, JOB)
        await kv.set(conn, "watchdog:job:acme.example.removed", {"since": now.isoformat()})
        monkeypatch.setattr(slack_client, "chat_postMessage", fail)
        await watchdog.check(ctx, conn, now + timedelta(minutes=1))
        assert await kv.get(conn, "watchdog:" + JOB) is None
        assert await kv.get(conn, "watchdog:job:acme.example.removed") is None
        assert "Private details" not in caplog.text


async def test_healthz_reports_job_status_without_job_names(app, http, monkeypatch):
    ctx = app.state.ctx
    since = datetime.now(UTC) - timedelta(minutes=11)
    ctx.scheduler.started_at = since
    async with ctx.pool.connection() as conn:
        await add_sensor(conn, JOB, since)
    response = await http.get("/healthz")
    assert response.status_code == 200 and response.json()["jobs"] == "ok"
    ctx.scheduler.names.add(JOB)
    response = await http.get("/healthz")
    assert response.status_code == 200 and response.json()["jobs"] == "silent"
    assert response.json()["status"] == "ok" and JOB not in response.text

    async def fail(conn, *args, **kwargs):
        await conn.execute("SELECT missing_column FROM sensors")

    monkeypatch.setattr("illo.app.silent_jobs", fail)
    response = await http.get("/healthz")
    assert response.status_code == 200 and response.json()["jobs"] == "unknown"
