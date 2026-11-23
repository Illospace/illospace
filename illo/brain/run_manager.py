"""Bound each run and revoke its token on every exit path."""

import asyncio
import json
from contextlib import aclosing
from dataclasses import dataclass

import httpx

from illo.brain.client import BrainEvent, RunRequest
from illo.brain.prompts import build_for_run
from illo.brain.routes import Payer, resolve_route, run_kind, select_payer
from illo.connectors import chatgpt
from illo.gateway.replies import RunFailure, failure_category
from illo.ledger import runs, sensors
from illo.tools._format import data_text


@dataclass(frozen=True)
class RunOptions:
    """The deep context of a run. S3 moves deep runs onto the normal lifecycle."""

    deep_run_id: int
    kind: str
    web_search: bool = False
    customer_read: bool = False
    personal_read: bool = False


def safe_event(item, auth, images=()):
    """Keep a provider echo out of progress, delivery, and all ledger fields."""
    line = item.model_dump_json()
    for value in (auth.access_token, auth.account_id):
        if value:
            line = line.replace(json.dumps(value.get_secret_value())[1:-1], "[redacted]")
    for image in images:
        line = line.replace(image.data.get_secret_value(), "[redacted]")
    return BrainEvent.model_validate_json(line)


class RunManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.capacity = asyncio.Condition()
        self.active = 0

    async def execute(self, event, authority, event_id: int, on_event=None, *,
                      deep: RunOptions | None = None) -> str:
        """The channel can relay progress. Only final text leaves through the gateway."""
        async with self.capacity:
            await self.capacity.wait_for(
                lambda: self.active < self.ctx.workspace.config.run_limits.max_concurrent
            )
            self.active += 1
        try:
            return await self._execute(event, authority, event_id, on_event, deep)
        finally:
            async with self.capacity:
                self.active -= 1
                self.capacity.notify_all()

    async def _execute(self, event, authority, event_id, on_event, deep):
        ctx = self.ctx
        policy = ctx.workspace.config.brain.chatgpt
        background = event.channel in {"digest", "team_digest", "deep"}
        route = resolve_route(ctx.workspace, run_kind(event, authority, deep=deep is not None))
        try:
            payer = await select_payer(ctx, route, authority, notify=not background)
        except chatgpt.TemporaryRefresh:
            if background:
                raise RunFailure("provider") from None
            return chatgpt.TEMPORARY
        if payer is None:
            if background:
                raise RunFailure("provider")
            if authority.tainted or authority.profile == "tainted":
                async with ctx.pool.connection() as conn:
                    row = await runs.create(
                        conn, principal=authority.principal, person_id=authority.person_id,
                        profile=authority.profile, tainted=True, event_id=event_id,
                        route=route.kind, provider=route.provider, model=route.model,
                        tool_limit=route.max_tool_calls,
                    )
                    await runs.finish(
                        conn, row["id"], usage={"skipped": "no_model_credential"}
                    )
                return (
                    'I did not answer this message. No model sign-in is allowed for messages from outside the team.'
                )
            if policy.person_credential == "team":
                return chatgpt.team_missing(ctx.workspace)
            return chatgpt.MISSING if authority.person_id is not None else (
                'I did not answer this message. I have no model sign-in for it.'
            )
        provider = ctx.providers[payer.provider]
        capabilities = provider["capabilities"]
        async with ctx.pool.connection() as conn:
            row = await runs.create(
                conn,
                principal=authority.principal,
                person_id=authority.person_id,
                profile=authority.profile,
                tainted=authority.tainted,
                event_id=event_id,
                route=route.kind,
                provider=payer.provider,
                model=route.model,
                payer=payer.kind,
                payer_person_id=payer.person_id,
                tool_limit=route.max_tool_calls,
            )
            if deep:
                await conn.execute("UPDATE runs SET deep_run_id=%s,deep_kind=%s,customer_read=%s,personal_read=%s WHERE id=%s",
                                   (deep.deep_run_id, deep.kind, deep.customer_read, deep.personal_read, row["id"]))
                await conn.execute("UPDATE deep_runs SET run_id=%s WHERE id=%s", (row["id"], deep.deep_run_id))
            token = await runs.mint_token(conn, row["id"], route.timeout_s)
            system_prompt = await build_for_run(conn, ctx.workspace, authority, event)
        from illo.channels.slack.images import for_run

        images, image_context = await for_run(ctx, event, authority, capabilities["images"])
        auth = payer.auth
        request = RunRequest(
            images=images,
            run_id=row["id"],
            system_prompt=system_prompt,
            user_message=(data_text("inbound-message", event.text)
                          if authority.tainted or authority.profile in {"tainted", "outsider"}
                          or authority.principal == "outsider" else event.text),
            mcp_url=ctx.config.public_url.rstrip("/") + "/brain/mcp",
            mcp_token=token,
            provider=payer.provider,
            model=route.model,
            effort=route.effort,
            max_tool_calls=route.max_tool_calls,
            timeout_s=route.timeout_s,
            max_output_tokens=route.max_output_tokens,
            web_search=bool(deep and deep.web_search and not deep.customer_read
                            and not deep.personal_read and capabilities["web_search"]),
            provider_auth=auth,
            fallback_provider=policy.fallback if payer.kind == "chatgpt" and policy.fallback != "none" else None,
        )
        if image_context:
            request.user_message += "\n\n" + image_context
        status, error, usage, final = "failed", None, {}, None
        if provider["wire"] == "responses":
            # The ledger shows which sign-in the run used, also for a failed run. The brain
            # reports "api_key" after a fallback. A run that reaches its time limit during
            # a fallback keeps the first value.
            usage = {"auth_kind": auth.kind}
            if auth.kind == "chatgpt":
                usage["credential_person"] = payer.person_key
        failure = "internal"
        try:
            async with asyncio.timeout(route.timeout_s):
                turns = 0
                for attempt in range(2):
                    retry = False
                    async with aclosing(ctx.brain_client.run(request)) as stream:
                        async for item in stream:
                            item = safe_event(item, auth, request.images)
                            if item.type == "web_search":
                                async with ctx.pool.connection() as conn:
                                    await conn.execute("UPDATE runs SET web_read=true,outside_read=true,usage=jsonb_set(usage,'{web_search_calls}',to_jsonb(COALESCE((usage->>'web_search_calls')::int,0)+1)) WHERE id=%s", (row["id"],))
                            if item.type == "tool_call":
                                turns += 1
                            if (
                                item.type == "error"
                                and item.category == "auth"
                                and auth.kind == "chatgpt"
                                and turns == 0
                                and attempt == 0
                            ):
                                fresh = await select_payer(
                                    ctx,
                                    route,
                                    authority,
                                    force=True,
                                    rejected_token=auth.access_token.get_secret_value(),
                                    notify=not background,
                                )
                                if fresh is not None:
                                    payer, auth = fresh, fresh.auth
                                    usage["auth_kind"] = auth.kind
                                    if auth.kind == "api_key":
                                        usage.pop("credential_person", None)
                                    request = request.model_copy(update={
                                        "provider_auth": auth, "provider": payer.provider,
                                        "fallback_provider": request.fallback_provider
                                        if payer.kind == "chatgpt" else None})
                                    retry = True
                                    break
                                if policy.person_credential == "team" and not authority.tainted:
                                    final = chatgpt.team_missing(ctx.workspace)
                                    error = "The team model sign-in is unavailable"
                                    failure = "provider"
                                    break
                            if on_event:
                                await on_event(item)
                            if item.type == "error":
                                failure = {"limit": "limit", "timeout": "timeout", "internal": "internal",
                                           "config": "config"}.get(item.category, "provider")
                                error = item.failure_reason()
                                # Keep the token counts of a failed run.
                                usage = {**usage, **item.usage}
                                if item.fallback_failed:
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
                                    if item.fallback_failed:
                                        final += " The API fallback failed."
                                    break
                                if item.category == "timeout":
                                    status = "timeout"
                                raise RunFailure(failure)
                            if item.type == "final":
                                if turns > route.max_tool_calls and item.usage.get("limited") is not True:
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
            if background:
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
            if payer.kind == "chatgpt" and usage.get("auth_kind") == "api_key":
                payer = Payer("api_key", policy.fallback)  # The brain used the fallback.
            async with ctx.pool.connection() as conn:
                if (payer.provider, payer.kind, payer.person_id) != (
                        row["provider"], row["payer"], row["payer_person_id"]):
                    await conn.execute(
                        "UPDATE runs SET provider=%s,payer=%s,payer_person_id=%s WHERE id=%s",
                        (payer.provider, payer.kind, payer.person_id, row["id"]))
                await runs.finish(conn, row["id"], status, usage=usage, error=error)
                if status == "completed":
                    await sensors.ok(conn, "brain")
                else:
                    await sensors.error(conn, "brain", error or "The brain run failed")
        if background and status != "completed":
            raise RunFailure(failure)
        if event.channel in {"slack", "email", "routine", "digest", "team_digest", "deep"}:
            from illo.brain.final_text import guard

            async with ctx.pool.connection() as conn:
                final = await guard(conn, row["id"], final)
        return final
