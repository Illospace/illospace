"""Bound each run and revoke its token on every exit path."""

import asyncio
import json
from contextlib import aclosing

import httpx

from illo.brain.client import BrainEvent, ProviderAuth, RunRequest
from illo.brain.prompts import build_for_run
from illo.connectors import chatgpt
from illo.db import one
from illo.gateway.replies import RunFailure, failure_category
from illo.ledger import runs, sensors


def credential_person(authority, workspace):
    """Select the model payer without a change to the run's authority."""
    if (
        workspace.config.brain.person_credential == "own"
        and not authority.tainted
        and authority.profile in {"person_dm", "agent_request"}
        and authority.person_id is not None
        and authority.principal.startswith("person:")
    ):
        return authority.principal[7:]
    return workspace.config.brain.team_credential


async def provider_auth(
    ctx, authority, runtime, *, force=False, rejected_token=None, auth_kind=None, notify=True
):
    config = ctx.workspace.config.brain
    if (auth_kind or config.auth) == "api_key":
        return ProviderAuth()
    if runtime != "openai_responses":
        raise ValueError("ChatGPT sign-in needs the openai_responses runtime")
    if authority.tainted or authority.profile in {"tainted", "outsider"}:
        if config.tainted_credential == "none":
            await chatgpt.notify_team_missing(ctx, tainted=True)
            return None
        if config.tainted_credential == "api_key":
            return ProviderAuth()
    key = credential_person(authority, ctx.workspace)
    personal = not authority.tainted and authority.profile in {"person_dm", "agent_request"}
    async with ctx.pool.connection() as conn:
        person = await one(conn, "SELECT id,key FROM people WHERE key=%s AND active", (key,))
    # In own mode, a mismatched authority cannot borrow the team grant, even with API fallback.
    if personal and config.person_credential == "own" and (
        not person or person["id"] != authority.person_id or authority.principal != "person:" + key
    ):
        return None
    auth = (
        await chatgpt.access(ctx, person, force=force, rejected_token=rejected_token, notify=notify)
        if person
        else None
    )
    if auth is not None:
        return auth
    if config.api_key_fallback:
        return ProviderAuth()
    if person:
        async with ctx.pool.connection() as conn:
            pending = await one(
                conn,
                "SELECT 1 FROM connector_grants WHERE connector_key='chatgpt' AND person_id=%s AND broken_at IS NULL AND next_refresh_at>clock_timestamp()",
                (person["id"],),
            )
        if pending:
            raise chatgpt.TemporaryRefresh(chatgpt.TEMPORARY)
    if not personal or config.person_credential == "team":
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
        try:
            auth = await provider_auth(ctx, authority, runtime, notify=event.channel not in {"digest", "team_digest"})
        except chatgpt.TemporaryRefresh:
            if event.channel in {"digest", "team_digest"}:
                raise RunFailure("provider") from None
            return chatgpt.TEMPORARY
        if auth is None:
            if event.channel in {"digest", "team_digest"}:
                raise RunFailure("provider")
            if authority.tainted or authority.profile == "tainted":
                async with ctx.pool.connection() as conn:
                    row = await runs.create(
                        conn, principal=authority.principal, person_id=authority.person_id,
                        profile=authority.profile, tainted=True, event_id=event_id,
                        runtime=runtime, model=model,
                    )
                    await runs.finish(
                        conn, row["id"], usage={"skipped": "no_model_credential"}
                    )
                return (
                    "Illo did not run the model for this event. "
                    "No model credential is allowed for tainted runs."
                )
            if config.person_credential == "team":
                return chatgpt.team_missing(ctx.workspace)
            return chatgpt.MISSING if authority.person_id is not None else (
                "Illo did not run the model for this event. No model credential is available."
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
        if runtime == "openai_responses":
            # The ledger shows which sign-in the run used, also for a failed run. The brain
            # reports "api_key" after a fallback. A run that reaches its time limit during
            # a fallback keeps the first value.
            usage = {"auth_kind": auth.kind}
            if auth.kind == "chatgpt":
                usage["credential_person"] = credential_person(authority, ctx.workspace)
        failure = "internal"
        try:
            async with asyncio.timeout(config.timeout_s):
                turns = 0
                for attempt in range(2):
                    retry = False
                    async with aclosing(ctx.brain_client.run(request)) as stream:
                        async for item in stream:
                            item = safe_event(item, auth)
                            if item.type == "tool_call":
                                turns += 1
                            if (
                                item.type == "error"
                                and item.category == "auth"
                                and auth.kind == "chatgpt"
                                and turns == 0
                                and attempt == 0
                            ):
                                fresh = await provider_auth(
                                    ctx,
                                    authority,
                                    runtime,
                                    force=True,
                                    rejected_token=auth.access_token.get_secret_value(),
                                    notify=event.channel not in {"digest", "team_digest"},
                                )
                                if fresh is not None:
                                    auth = fresh
                                    usage["auth_kind"] = auth.kind
                                    if auth.kind == "api_key":
                                        usage.pop("credential_person", None)
                                    request = request.model_copy(update={"provider_auth": auth})
                                    retry = True
                                    break
                                if config.person_credential == "team" and not authority.tainted:
                                    final = chatgpt.team_missing(ctx.workspace)
                                    error = "The team model sign-in is unavailable"
                                    failure = "provider"
                                    break
                            if on_event:
                                await on_event(item)
                            if item.type == "error":
                                failure = {"limit": "limit", "timeout": "timeout", "internal": "internal"}.get(
                                    item.category, "provider"
                                )
                                error = item.failure_reason()
                                # Keep the token counts of a failed run.
                                usage = {**usage, **item.usage}
                                if item.api_key_fallback_failed:
                                    usage["auth_kind"] = "api_key"
                                if usage.get("auth_kind") == "api_key":
                                    usage.pop("credential_person", None)
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
                                raise RunFailure(failure)
                            if item.type == "final":
                                if turns > config.max_turns and item.usage.get("limited") is not True:
                                    raise RunFailure("limit")
                                final, usage, status = item.text, {**usage, **item.usage}, "completed"
                                if usage.get("auth_kind") == "api_key":
                                    usage.pop("credential_person", None)
                                break
                    if not retry:
                        break
                if final is None:
                    raise RuntimeError("The brain service ended without a final answer")
        except chatgpt.TemporaryRefresh:
            if event.channel in {"digest", "team_digest"}:
                error = "The model sign-in service is unavailable"
                failure = "provider"
                raise RunFailure("provider") from None
            failure = "provider"
            final, error = chatgpt.TEMPORARY, "The model sign-in service is unavailable"
        except (TimeoutError, httpx.TimeoutException):
            failure = "timeout"
            status, error = "timeout", "The run reached its time limit"
            raise RunFailure("timeout") from None
        except asyncio.CancelledError:
            error = "The run was cancelled"
            raise
        except Exception as exc:
            failure = failure_category(exc)
            error = error or "The brain run failed"
            raise
        finally:
            if status != "completed":
                usage = {**usage, "failure_category": failure}
            async with ctx.pool.connection() as conn:
                await runs.finish(conn, row["id"], status, usage=usage, error=error)
                if status == "completed":
                    await sensors.ok(conn, "brain")
                else:
                    await sensors.error(conn, "brain", error or "The brain run failed")
        if event.channel in {"digest", "team_digest"} and status != "completed":
            raise RunFailure(failure)
        return final
