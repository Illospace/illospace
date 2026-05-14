"""Helpers for running the blocking direct-agent loop from async recipes."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from brain.platform.async_io import run_blocking
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.integrations.llm import async_resolve_llm_client
from brain.platform.providers.model_policy import (
    MODEL_TIERS,
    async_get_model_for_tier,
    infer_provider_from_model,
    normalize_model_tier,
)


def _required_openai_auth_mode(model: str | None) -> str | None:
    if not model:
        return None
    value = str(model)
    for prefix in ("anthropic/", "openai/", "anthropic:", "openai:"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return "chatgpt" if value.lower() == "gpt-5.5" else None


def sync_on_loop(loop: asyncio.AbstractEventLoop, async_fn):
    def _call(*args, **kwargs):
        future = asyncio.run_coroutine_threadsafe(async_fn(*args, **kwargs), loop)
        return future.result()

    return _call


def thread_sync_tool_handlers(loop: asyncio.AbstractEventLoop, handlers: dict[str, Any]) -> dict[str, Any]:
    bridged: dict[str, Any] = {}
    for name, handler in handlers.items():
        async def _invoke(_handler=handler, **kwargs):
            result = _handler(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result

        bridged[name] = sync_on_loop(loop, _invoke)
    return bridged


async def invoke_direct_agent_threaded(invoke_direct_agent, spec):
    if _is_default_direct_agent_invocation(invoke_direct_agent):
        spec, resolved_model, resolved_llm = await _resolve_direct_agent_runtime(spec)
        result = await run_blocking(_invoke_direct_agent_with_resolved_llm, spec, resolved_model, resolved_llm)
    else:
        result = await run_blocking(invoke_direct_agent, spec)
    if inspect.isawaitable(result):
        result = await result
    return result


def _is_default_direct_agent_invocation(invoke_direct_agent) -> bool:
    return (
        getattr(invoke_direct_agent, "__module__", "") == "brain.systems.runs.invocation"
        and getattr(invoke_direct_agent, "__name__", "") == "invoke_direct_agent"
    )


async def _resolve_direct_agent_runtime(spec):
    user_id = getattr(spec, "user_id", None)
    metadata = getattr(spec, "metadata", None) or {}
    org_id = metadata.get("org_id") if isinstance(metadata, dict) else None
    if not user_id and not org_id:
        return spec, None, None

    async with UnitOfWork() as uow:
        model = getattr(spec, "model", None)
        tier = normalize_model_tier(str(model), default=None) if model else None
        if not model or tier in MODEL_TIERS:
            model = await async_get_model_for_tier(
                uow.session,
                tier or "medium",
                include_provider_prefix=True,
                user_id=user_id,
                org_id=org_id,
            )
        provider = infer_provider_from_model(str(model))
        llm = await async_resolve_llm_client(
            user_id=user_id,
            org_id=org_id,
            provider=provider,
            auth_mode=_required_openai_auth_mode(str(model)) if provider == "openai" else None,
            session=uow.session,
        )
    return spec, str(model), llm


def _invoke_direct_agent_with_resolved_llm(spec, resolved_model, resolved_llm):
    from brain.kernel.runtime.kernel import invoke_run_envelope

    overrides = {}
    if resolved_model:
        overrides["model"] = resolved_model
    if resolved_llm is not None:
        overrides["resolved_llm"] = resolved_llm
    return invoke_run_envelope(spec.to_run_envelope(), **overrides)


__all__ = [
    "invoke_direct_agent_threaded",
    "sync_on_loop",
    "thread_sync_tool_handlers",
]
