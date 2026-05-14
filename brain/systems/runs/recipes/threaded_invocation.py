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
from brain.systems.sessions import (
    async_load_session,
    async_load_session_handoff,
    async_save_session,
    async_save_session_handoff,
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
        loop = asyncio.get_running_loop()
        spec, resolved_model, resolved_llm = await _resolve_direct_agent_runtime(spec)
        result = await run_blocking(
            _invoke_direct_agent_with_resolved_llm,
            spec,
            resolved_model,
            resolved_llm,
            _direct_agent_loop_overrides(loop, spec),
        )
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


class _LoopBoundCancelEvent:
    def __init__(self, loop: asyncio.AbstractEventLoop, cancel_event: Any):
        self._loop = loop
        self._cancel_event = cancel_event

    async def a_is_set(self) -> bool:
        checker = getattr(self._cancel_event, "a_is_set", None)
        if checker is None:
            checker = getattr(self._cancel_event, "is_set", None)
        if checker is None:
            return False
        result = checker()
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    def is_set(self) -> bool:
        return bool(sync_on_loop(self._loop, self.a_is_set)())


def _direct_agent_loop_overrides(loop: asyncio.AbstractEventLoop, spec) -> dict[str, Any]:
    overrides: dict[str, Any] = {
        "load_session": sync_on_loop(loop, async_load_session),
        "load_session_handoff": sync_on_loop(loop, async_load_session_handoff),
        "save_session": sync_on_loop(loop, async_save_session),
        "save_session_handoff": sync_on_loop(loop, async_save_session_handoff),
    }
    cancel_event = getattr(spec, "cancel_event", None)
    if cancel_event is not None:
        overrides["cancel_event"] = _LoopBoundCancelEvent(loop, cancel_event)
    return overrides


def _invoke_direct_agent_with_resolved_llm(spec, resolved_model, resolved_llm, run_agent_overrides=None):
    from brain.kernel.runtime.kernel import invoke_run_envelope

    overrides = dict(run_agent_overrides or {})
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
