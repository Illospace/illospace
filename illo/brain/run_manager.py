"""Bound each run and revoke its token on every exit path."""

import asyncio
import json
from contextlib import aclosing
from dataclasses import dataclass

import httpx

from illo.brain.client import BrainEvent, RunRequest
from illo.brain.prompts import build_for_run
from illo.brain.routes import (
    Payer,
    Route,
    raises_failure,
    resolve_route,
    run_kind,
    select_payer,
    sends_refresh_notice,
    web_search_allowed,
)
from illo.connectors import chatgpt
from illo.db import one
from illo.gateway.principal import Authority
from illo.gateway.replies import FAILURE_TEXTS, RunFailure, failure_category
from illo.ledger import runs, sensors
from illo.tools._format import data_text


@dataclass(frozen=True)
class DeepContext:
    deep_run_id: int
    kind: str
    authority: Authority
    task_actor: str | None
    deliver: dict
    # Team requests have a requester even when the run has no person ID.
    requester_id: int | None = None
    customer_read: bool = False
    personal_read: bool = False


async def _setup_deep(ctx, deep: DeepContext) -> None:
    from illo.deep.ports import refresh_repos

    if deep.requester_id is not None:
        async with ctx.pool.connection() as conn:
            if not await one(conn, "SELECT id FROM people WHERE id=%s AND active", (deep.requester_id,)):
                raise RunFailure("internal", detail="inactive_person")
    try:
        await refresh_repos(ctx)
    except Exception as exc:
        raise RunFailure(failure_category(exc), detail="repo_refresh") from exc


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

    async def start_run(self, event, authority, event_id: int | None, on_event=None, *,
                        deep: DeepContext | None = None) -> str:
        """The channel can relay progress. Only final text leaves through the gateway."""
        return await self._run(event, authority, event_id, on_event, deep=deep)

    async def smoke(self, event, authority, route: Route, check, on_event) -> None:
        """The operator CLI supplies the test route. Use the normal run lifecycle."""
        await self._run(event, authority, None, on_event, route=route, smoke=check)

    async def _run(self, event, authority, event_id, on_event, *,
                   route: Route | None = None, deep=None, smoke=None):
        async with self.capacity:
            await self.capacity.wait_for(
                lambda: self.active < self.ctx.workspace.config.run_limits.max_concurrent
            )
            self.active += 1
        try:
            return await self._start_run(event, authority, event_id, on_event, route, deep, smoke)
        finally:
            async with self.capacity:
                self.active -= 1
                self.capacity.notify_all()

    async def _start_run(self, event, authority, event_id, on_event, route, deep, smoke):
        ctx = self.ctx
        policy = ctx.workspace.config.brain.chatgpt
        if route is None:
            route = resolve_route(ctx.workspace, run_kind(event, authority, deep=deep is not None))
        notify = smoke is None and sends_refresh_notice(route.kind)
        async with ctx.pool.connection() as conn:
            if event.channel == "board":
                from illo.board.coordinator import authorized_packet

                if not await authorized_packet(conn, ctx, event, authority):
                    raise RunFailure("internal")
            row = await runs.create(
                conn, principal=authority.principal, person_id=authority.person_id,
                profile=authority.profile, tainted=authority.tainted, event_id=event_id,
                route=route.kind, provider=route.provider, model=route.model, tool_limit=route.max_tool_calls,
                deep_run_id=deep.deep_run_id if deep else None, deep_kind=deep.kind if deep else None,
                customer_read=bool(deep and deep.customer_read), personal_read=authority.personal_read or bool(deep and deep.personal_read),
            )
            run_id = row["id"]
            if event.channel == "board":
                await conn.execute("UPDATE board_coordinator_runs SET event_id=%s,run_id=%s WHERE id=%s",
                                   (event_id, run_id, event.payload["coordinator_run_id"]))
            if deep:
                await conn.execute("UPDATE deep_runs SET run_id=%s WHERE id=%s", (run_id, deep.deep_run_id))
        status, error, usage, final = "failed", None, {}, None
        if smoke:
            usage["smoke"] = True
        payer = provider = request = None
        failure, detail = "internal", None
        try:
            async with asyncio.timeout(route.timeout_s):
                if deep:
                    await _setup_deep(ctx, deep)
                payer = await select_payer(ctx, route, authority, notify=notify)
                if payer is None:
                    detail, error = "no_payer", "No model sign-in is available"
                    if smoke or raises_failure(route.kind):
                        raise RunFailure("provider", detail=detail)
                    if authority.tainted or authority.profile == "tainted":
                        usage["skipped"] = "no_model_credential"
                        return 'I did not answer this message. No model sign-in is allowed for messages from outside the team.'
                    if policy.person_credential == "team":
                        return chatgpt.team_missing(ctx.workspace)
                    return chatgpt.MISSING if authority.person_id is not None else (
                        'I did not answer this message. I have no model sign-in for it.'
                    )
                from illo.channels.slack.images import for_run

                usage_payer, auth = payer, payer.auth
                provider = ctx.providers[payer.provider]
                async with ctx.pool.connection() as conn:
                    token = await runs.mint_token(conn, run_id, route.timeout_s)
                    system_prompt = (smoke.system_prompt if smoke else
                                     await build_for_run(conn, ctx.workspace, authority, event))
                shows_images = provider["capabilities"]["images"]
                if smoke:
                    images = [smoke.image] if shows_images else []
                    user_message = smoke.question if images else event.text
                else:
                    images, image_context = await for_run(ctx, event, authority, shows_images)
                    user_message = (data_text("inbound-message", event.text)
                                    if authority.tainted or authority.profile in {"tainted", "outsider"}
                                    or authority.principal == "outsider" else event.text)
                    if image_context:
                        user_message += "\n\n" + image_context
                request = RunRequest(
                    images=images,
                    run_id=run_id,
                    system_prompt=system_prompt,
                    user_message=user_message,
                    mcp_url=ctx.config.public_url.rstrip("/") + "/brain/mcp",
                    mcp_token=token,
                    **payer.request(),
                    model=route.model,
                    effort=route.effort,
                    max_tool_calls=route.max_tool_calls,
                    timeout_s=route.timeout_s,
                    max_output_tokens=route.max_output_tokens,
                    web_search=web_search_allowed(ctx.workspace, route, provider, deep),
                    smoke=smoke is not None,
                )
                turns = 0
                for attempt in range(2):
                    retry = False
                    async with aclosing(ctx.brain_client.run(request)) as stream:
                        async for item in stream:
                            item = safe_event(item, auth, request.images)
                            if item.type == "web_search":
                                async with ctx.pool.connection() as conn:
                                    await conn.execute("UPDATE runs SET web_read=true,outside_read=true,usage=jsonb_set(usage,'{web_search_calls}',to_jsonb(COALESCE((usage->>'web_search_calls')::int,0)+1)) WHERE id=%s", (run_id,))
                            if item.type == "payer" and payer.fallback:
                                # Keep the first payer in runs.payer/payer_person_id (S5); legacy
                                # usage tracks the final payer so S5 marks fallback rows unsplit.
                                usage_payer = Payer("api_key", payer.fallback)
                                if smoke and on_event:
                                    await on_event(item)
                                continue
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
                                    notify=notify,
                                )
                                if fresh is not None:
                                    payer = usage_payer = fresh
                                    auth = fresh.auth
                                    request = request.model_copy(update={
                                        **payer.request(),
                                        "web_search": web_search_allowed(ctx.workspace, route, ctx.providers[payer.provider], deep),
                                    })
                                    retry = True
                                    break
                                detail = "sign_in"
                                if not smoke and policy.person_credential == "team" and not authority.tainted:
                                    final = chatgpt.team_missing(ctx.workspace)
                                    error = "The team model sign-in is unavailable"
                                    failure = "provider"
                                    break
                            if on_event:
                                await on_event(item)
                            if item.type == "error":
                                # Preserve recognized categories, including transient, in the receipt.
                                failure = item.category if item.category in FAILURE_TEXTS else "provider"
                                error = "The board model run failed." if event.channel == "board" else item.failure_reason()
                                # Keep the token counts of a failed run.
                                usage = {**usage, **item.usage}
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
                                usage.update(item.usage)
                                if smoke:
                                    smoke.verify(item, bool(request.images))
                                final, status = item.text, "completed"
                                break
                    if not retry:
                        break
                if final is None:
                    raise RuntimeError("The brain service ended without a final answer")
            if (smoke or raises_failure(route.kind)) and status != "completed":
                raise RunFailure(failure)
            if event.channel in {"deep", "digest", "email", "routine", "slack", "team_digest"}:
                from illo.brain.final_text import guard

                async with ctx.pool.connection() as conn:
                    final = await guard(conn, run_id, final)
            return final
        except chatgpt.TemporaryRefresh:
            detail, error = "sign_in", "The model sign-in service is unavailable"
            if smoke or raises_failure(route.kind):
                raise RunFailure("provider") from None
            return chatgpt.TEMPORARY
        except (TimeoutError, httpx.TimeoutException):
            failure = "timeout"
            status, error = "timeout", "The run reached its time limit"
            raise RunFailure("timeout") from None
        except asyncio.CancelledError:
            status, error = "failed", "The run was cancelled"
            raise
        except Exception as exc:
            failure = failure_category(exc)
            if isinstance(exc, RunFailure) and exc.detail:
                detail = exc.detail
            if status == "completed":
                status = "failed"
            error = error or "The brain run failed"
            raise
        finally:
            if status != "completed":
                usage["failure_category"] = detail or failure
            if provider and provider["wire"] == "responses":
                usage = {k: v for k, v in usage.items() if k != "credential_person"} | usage_payer.usage()
            async with ctx.pool.connection() as conn:
                if payer is not None:
                    await conn.execute(
                        "UPDATE runs SET provider=%s,payer=%s,payer_person_id=%s WHERE id=%s",
                        (payer.provider, payer.kind, payer.person_id, run_id))
                await runs.finish(conn, run_id, status, usage=usage, error=error)
                if request is not None:
                    if status == "completed":
                        await sensors.ok(conn, "brain")
                    else:
                        await sensors.error(conn, "brain", error or "The brain run failed")
