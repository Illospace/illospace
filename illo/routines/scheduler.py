"""Small asyncio timers with sensor reports for each job."""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from illo.ledger import sensors

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
    """The foundation owns the timer engine. Other routines register their jobs."""
