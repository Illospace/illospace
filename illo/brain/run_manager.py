"""Bound each run and revoke its token on every exit path."""

import asyncio

from illo.brain.client import RunRequest
from illo.brain.prompts import build_for_run
from illo.ledger import runs, sensors


class RunManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.capacity = asyncio.Condition()
        self.active = 0

    async def execute(self, event, authority, event_id: int, on_event=None) -> str:
        """The channel can relay progress. Only final text leaves through the gateway."""
        async with self.capacity:
            await self.capacity.wait_for(
                lambda: self.active < self.ctx.workspace.config.run_limits.max_concurrent
            )
            self.active += 1
        try:
            return await self._execute(event, authority, event_id, on_event)
        finally:
            async with self.capacity:
                self.active -= 1
                self.capacity.notify_all()

    async def _execute(self, event, authority, event_id, on_event):
        ctx = self.ctx
        config = ctx.workspace.config.brain
        runtime = ctx.config.brain_runtime or config.runtime
        model = ctx.config.brain_model or config.model
        async with ctx.pool.connection() as conn:
            row = await runs.create(
                conn,
                principal=authority.principal,
                person_id=authority.person_id,
                profile=authority.profile,
                tainted=authority.tainted,
                event_id=event_id,
                runtime=runtime,
                model=model,
            )
            token = await runs.mint_token(conn, row["id"], config.timeout_s)
            system_prompt = await build_for_run(conn, ctx.workspace, authority, event)
        request = RunRequest(
            run_id=row["id"],
            system_prompt=system_prompt,
            user_message=event.text,
            mcp_url=ctx.config.public_url.rstrip("/") + "/brain/mcp",
            mcp_token=token,
            runtime=runtime,
            model=model,
            max_turns=config.max_turns,
            timeout_s=config.timeout_s,
        )
        status, error, usage, final = "failed", None, {}, None
        try:
            async with asyncio.timeout(config.timeout_s):
                turns = 0
                async for item in ctx.brain_client.run(request):
                    if item.type == "tool_call":
                        turns += 1
                        if turns > config.max_turns:
                            raise RuntimeError("The run reached its turn limit")
                    if on_event:
                        await on_event(item)
                    if item.type == "error":
                        error = item.failure_reason()
                        if item.category == "timeout":
                            status = "timeout"
                        raise RuntimeError(error)
                    if item.type == "final":
                        final, usage, status = item.text, item.usage, "completed"
                        break
                if final is None:
                    raise RuntimeError("The brain service ended without a final answer")
        except TimeoutError:
            status, error = "timeout", "The run reached its time limit"
            raise RuntimeError(error) from None
        except asyncio.CancelledError:
            error = "The run was cancelled"
            raise
        except Exception:
            error = error or "The brain run failed"
            raise
        finally:
            async with ctx.pool.connection() as conn:
                await runs.finish(conn, row["id"], status, usage=usage, error=error)
                if status == "completed":
                    await sensors.ok(conn, "brain")
                else:
                    await sensors.error(conn, "brain", error or "The brain run failed")
        return final
