"""Bound each run and revoke its token on every exit path."""

import asyncio
import json

from illo.brain.client import BrainEvent, ProviderAuth, RunRequest
from illo.brain.prompts import build_for_run
from illo.connectors import chatgpt
from illo.db import one
from illo.ledger import runs, sensors


def credential_person(authority, workspace):
    """Only an untainted person request uses that person's subscription."""
    if (
        not authority.tainted
        and authority.profile in {"person_dm", "agent_request"}
        and authority.person_id is not None
        and authority.principal.startswith("person:")
    ):
        return authority.principal[7:]
    return workspace.config.brain.team_credential or workspace.config.default_owner


async def provider_auth(ctx, authority, runtime):
    config = ctx.workspace.config.brain
    if config.auth == "api_key":
        return ProviderAuth()
    if runtime != "openai_responses":
        raise ValueError("ChatGPT sign-in needs the openai_responses runtime")
    key = credential_person(authority, ctx.workspace)
    personal = not authority.tainted and authority.profile in {"person_dm", "agent_request"}
    async with ctx.pool.connection() as conn:
        person = await one(conn, "SELECT id,key FROM people WHERE key=%s AND active", (key,))
    # A mismatched authority cannot borrow the team grant, even with API fallback.
    if personal and (
        not person or person["id"] != authority.person_id or authority.principal != "person:" + key
    ):
        return None
    auth = await chatgpt.access(ctx, person) if person else None
    if auth is not None:
        return auth
    if config.api_key_fallback:
        return ProviderAuth()
    if not personal:
        await chatgpt.notify_team_missing(ctx)
    return None


def safe_event(item, auth):
    """Keep a provider echo out of progress, delivery, and all ledger fields."""
    line = item.model_dump_json()
    for value in (auth.access_token, auth.account_id):
        if value:
            line = line.replace(json.dumps(value.get_secret_value())[1:-1], "[redacted]")
    return BrainEvent.model_validate_json(line)


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
        auth = await provider_auth(ctx, authority, runtime)
        if auth is None:
            return (
                chatgpt.MISSING if not authority.tainted and authority.person_id is not None else ""
            )
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
            provider_auth=auth,
            api_key_fallback=config.api_key_fallback,
        )
        status, error, usage, final = "failed", None, {}, None
        try:
            async with asyncio.timeout(config.timeout_s):
                turns = 0
                async for item in ctx.brain_client.run(request):
                    item = safe_event(item, auth)
                    if item.type == "tool_call":
                        turns += 1
                        if turns > config.max_turns:
                            raise RuntimeError("The run reached its turn limit")
                    if on_event:
                        await on_event(item)
                    if item.type == "error":
                        error = item.failure_reason()
                        if auth.kind == "chatgpt" and item.code == "usage_limit_reached":
                            from datetime import UTC, datetime

                            final = "Your ChatGPT limit is reached."
                            if item.resets_at and 0 < item.resets_at < 253402300800:
                                final += (
                                    " It resets "
                                    + datetime.fromtimestamp(item.resets_at, UTC).strftime(
                                        "%Y-%m-%d %H:%M UTC"
                                    )
                                    + "."
                                )
                            if item.api_key_fallback_failed:
                                final += " The API fallback failed."
                            break
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
