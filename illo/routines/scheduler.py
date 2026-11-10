"""Small asyncio timers with sensor reports for each job."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

from illo.db import all_rows, one
from illo.gateway.events import InboundEvent
from illo.ledger import audit, claims, drafts, kv, sensors
from illo.slackapi import dm_person

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, ctx):
        self.ctx = ctx
        self.tasks: list[asyncio.Task] = []

    async def setup(self) -> None:
        """Keep the scheduler compatible with the module setup contract."""

    async def _call(self, fn):
        name = f"job:{fn.__module__}.{fn.__name__}"
        try:
            await fn()
        except Exception:
            log.error("Scheduled job failed: %s", name)
            try:
                async with self.ctx.pool.connection() as conn:
                    await sensors.error(conn, name, "The scheduled job failed")
            except Exception:
                log.error("Could not record the failed job sensor: %s", name)
        else:
            try:
                async with self.ctx.pool.connection() as conn:
                    await sensors.ok(conn, name)
            except Exception:
                log.error("Could not record the job sensor: %s", name)

    def every(self, seconds: float, fn) -> asyncio.Task:
        """Run after each interval. Slow jobs never overlap with themselves."""
        if seconds <= 0:
            raise ValueError("The interval must be positive")

        async def loop():
            while True:
                await asyncio.sleep(seconds)
                await self._call(fn)

        task = asyncio.create_task(loop())
        self.tasks.append(task)
        return task

    def cron(self, expr: str, fn) -> asyncio.Task:
        """Use the team's IANA time zone for five-field cron schedules."""
        if len(expr.split()) != 5 or not croniter.is_valid(expr):
            raise ValueError("Use a five-field cron schedule")

        async def loop():
            while True:
                now = datetime.now(ZoneInfo(self.ctx.workspace.config.timezone))
                next_time = croniter(expr, now).get_next(datetime)
                await asyncio.sleep(max(0, next_time.timestamp() - now.timestamp()))
                await self._call(fn)

        task = asyncio.create_task(loop())
        self.tasks.append(task)
        return task

    async def close(self) -> None:
        """Cancel jobs before the database pool closes."""
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def setup(ctx) -> None:
    """Register persisted routines and run the startup catch-up once."""
    ctx.gateway.register("routine", RoutineAdapter(ctx))
    ctx.draft_performers["routine_delivery"] = perform_delivery

    async def run_routines():
        await tick_routines(ctx)

    ctx.every(60, run_routines)
    await run_routines()


def next_due(schedule: str, after: datetime, timezone: str) -> datetime:
    """Compare instants in UTC, including skipped and repeated local hours."""
    return (
        croniter(schedule, after.astimezone(ZoneInfo(timezone))).get_next(datetime).astimezone(UTC)
    )


class RoutineAdapter:
    def __init__(self, ctx):
        self.ctx = ctx

    async def show_working(self, event):
        pass

    async def deliver_final(self, event, text):
        destination = event.payload["destination"]
        if destination == "none":
            return
        async with self.ctx.pool.connection() as conn:
            row = await one(
                conn,
                """
                SELECT r.* FROM runs r JOIN events e ON e.id=r.event_id
                WHERE e.channel='routine' AND e.external_id=%s ORDER BY r.id DESC LIMIT 1
            """,
                (event.external_id,),
            )
            if row and row["personal_read"] and destination != row["principal"]:
                await drafts.create(
                    conn,
                    run_id=row["id"],
                    approver_person_id=row["person_id"],
                    kind="routine_delivery",
                    destination=destination,
                    content={"destination": destination, "text": text},
                )
                await audit.write(
                    conn,
                    run_id=row["id"],
                    principal=row["principal"],
                    tool="routine_delivery",
                    args={"destination": destination},
                    decision="draft",
                    reason="Personal data needs approval",
                )
                return
        await deliver_routine(self.ctx, destination, text)

    async def deliver_error(self, event):
        # Do not post an error to a shared destination from a personal run.
        log.error("Routine failed: %s", event.payload["routine"])


async def deliver_routine(ctx, destination: str, text: str):
    if destination.startswith("person:"):
        if not await dm_person(ctx, destination[7:], text):
            raise RuntimeError("The routine DM could not be sent")
    elif destination.startswith("channel:") and ctx.slack_client:
        await ctx.slack_client.chat_postMessage(channel=destination[8:], text=text)
    else:
        raise RuntimeError("The routine destination is unavailable")


async def perform_delivery(ctx, draft):
    if draft["state"] != "approved":
        raise ValueError("The draft needs approval")
    await deliver_routine(ctx, draft["destination"], draft["content"]["text"])


def routine_destination(ctx, value: str) -> str:
    if value.startswith("dm:"):
        return "person:" + value[3:]
    if value.startswith("channel:"):
        for channel in ctx.workspace.config.slack.channels:
            if value[8:] in {channel.name, channel.id}:
                return "channel:" + channel.id
    return value


async def tick_routines(ctx, now: datetime | None = None):
    """Commit each scheduled claim before the gateway starts its run."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    timezone = ctx.workspace.config.timezone
    for routine in list(ctx.workspace.routines):
        identity = f"{routine.schedule}|{timezone}|{routine.enabled}"
        async with ctx.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO routine_state (key) VALUES (%s) ON CONFLICT (key) DO NOTHING
            """,
                (routine.name,),
            )
            state = await one(
                conn, "SELECT * FROM routine_state WHERE key=%s FOR UPDATE", (routine.name,)
            )
            previous = await kv.get(conn, "routine-config:" + routine.name)
            scheduled = state["next_run_at"]
            if previous != identity or scheduled is None:
                scheduled = next_due(
                    routine.schedule,
                    now.replace(second=0, microsecond=0) - timedelta(microseconds=1),
                    timezone,
                )
                await kv.set(conn, "routine-config:" + routine.name, identity)
                await conn.execute(
                    "UPDATE routine_state SET next_run_at=%s WHERE key=%s",
                    (scheduled, routine.name),
                )
            if not routine.enabled:
                await conn.execute(
                    "UPDATE routine_state SET last_status='disabled',next_run_at=NULL WHERE key=%s",
                    (routine.name,),
                )
                continue
            if scheduled > now:
                continue
            next_time = next_due(routine.schedule, now, timezone)
            key = f"routine:{routine.name}:{scheduled.isoformat()}"
            claimed = await claims.take(conn, key, None, "Scheduled routine")
            late = now - scheduled >= timedelta(hours=6)
            await conn.execute(
                """
                UPDATE routine_state SET next_run_at=%s,last_run_at=%s,last_status=%s WHERE key=%s
            """,
                (
                    next_time,
                    scheduled,
                    ("skipped" if late else "running") if claimed else state["last_status"],
                    routine.name,
                ),
            )
        if not claimed:
            continue
        if late:
            log.warning("Skipped a routine more than six hours late: %s", routine.name)
            continue
        prompt = routine.body
        for skill in ctx.workspace.skills:
            if skill.name in routine.skills:
                prompt += f"\n\nSkill: {skill.name}\n{skill.body}"
        event = InboundEvent(
            channel="routine",
            external_id=key,
            kind="scheduled",
            text=prompt,
            payload={
                "routine": routine.name,
                "principal": routine.principal,
                "destination": routine_destination(ctx, routine.destination),
            },
        )
        result = await ctx.gateway.receive(event)
        async with ctx.pool.connection() as conn:
            await conn.execute(
                "UPDATE routine_state SET last_status=%s WHERE key=%s AND last_run_at=%s",
                ("failed" if result.get("error") else "completed", routine.name, scheduled),
            )


async def list_routines(conn, workspace):
    states = {row["key"]: row for row in await all_rows(conn, "SELECT * FROM routine_state")}
    return [
        {
            "name": r.name,
            "next_run_at": states.get(r.name, {}).get("next_run_at"),
            "last_status": states.get(r.name, {}).get("last_status") or "not run",
        }
        for r in workspace.routines
    ]
