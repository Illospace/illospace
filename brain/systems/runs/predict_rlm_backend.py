"""PredictRLM worker backend integration for Illo AgentRun workers."""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems.runs.events import async_record_tool_call
from brain.platform.db.models.org import Org, User
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.integrations.llm import _resolve_key_from_db, _resolve_key_from_env, resolve_llm_client
from brain.platform.integrations.providers import LLMRequest, _merge_streamed_output_into_response, get_provider
from brain.systems.runs.direct_agent import (
    AgentResult,
    _invoke_tool_handler,
    _record_api_call,
    _required_openai_auth_mode,
    _agent_context,
)
from brain.platform.providers.model_policy import (
    DEFAULT_PROVIDER_MODEL_MAPS,
    HIGH_MODEL_TIER,
    LOW_MODEL_TIER,
    MEDIUM_MODEL_TIER,
    async_get_model_for_tier,
    async_get_provider_model_map,
    async_resolve_default_provider,
    get_model_for_tier,
    get_provider_model_map,
    infer_provider_from_model,
    normalize_model_tier,
    normalize_runtime_provider,
    resolve_default_provider,
)

logger = logging.getLogger("agent_runtime")


@contextmanager
def _predict_rlm_unit_of_work():
    factory = UnitOfWork.blocking if getattr(UnitOfWork, "__name__", "") == "UnitOfWork" else UnitOfWork
    with factory() as uow:
        yield uow

_SUPPORTED_BACKENDS = {"auto", "native", "predict_rlm"}
_DEFAULT_BACKEND = os.environ.get("AGENT_WORKER_BACKEND", "auto").strip().lower() or "auto"
_DEFAULT_MAX_ITERATIONS = int(os.environ.get("PREDICT_RLM_MAX_ITERATIONS", "24"))
_DEFAULT_MAX_LLM_CALLS = int(os.environ.get("PREDICT_RLM_MAX_LLM_CALLS", "48"))

_SCHEMA_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


@dataclass(frozen=True)
class WorkerBackendSettings:
    requested_backend: str
    effective_backend: str
    predict_rlm_package_available: bool
    predict_rlm_deno_available: bool
    predict_rlm_ready: bool
    predict_rlm_version: str | None
    predict_rlm_sub_lm: str | None
    predict_rlm_max_iterations: int
    predict_rlm_max_llm_calls: int
    fallback_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_worker_backend": self.requested_backend,
            "agent_effective_worker_backend": self.effective_backend,
            "predict_rlm_available": self.predict_rlm_package_available,
            "predict_rlm_deno_available": self.predict_rlm_deno_available,
            "predict_rlm_ready": self.predict_rlm_ready,
            "predict_rlm_version": self.predict_rlm_version,
            "predict_rlm_sub_lm": self.predict_rlm_sub_lm,
            "predict_rlm_max_iterations": self.predict_rlm_max_iterations,
            "predict_rlm_max_llm_calls": self.predict_rlm_max_llm_calls,
            "predict_rlm_fallback_reason": self.fallback_reason,
        }


def _normalize_backend(value: str | None) -> str:
    normalized = (value or _DEFAULT_BACKEND or "auto").strip().lower()
    return normalized if normalized in _SUPPORTED_BACKENDS else "auto"


def _normalize_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _load_org_memory_model_config(
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    try:
        with _predict_rlm_unit_of_work() as uow:
            resolved_org_id = org_id
            if not resolved_org_id and user_id:
                db_user = uow.session.get(User, user_id)
                resolved_org_id = getattr(db_user, "org_id", None)
            if not resolved_org_id:
                return {}
            org = uow.session.get(Org, resolved_org_id)
            return dict(org.memory_model_config or {}) if org else {}
    except Exception:
        return {}


async def _async_load_org_memory_model_config(
    session: AsyncSession,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    try:
        resolved_org_id = org_id
        if not resolved_org_id and user_id:
            db_user = await session.get(User, user_id)
            resolved_org_id = getattr(db_user, "org_id", None)
        if not resolved_org_id:
            return {}
        org = await session.get(Org, resolved_org_id)
        return dict(org.memory_model_config or {}) if org else {}
    except Exception:
        return {}


def _predict_rlm_support() -> dict[str, Any]:
    package_available = False
    version = None
    try:
        importlib.import_module("predict_rlm.predict_rlm")
        importlib.import_module("predict_rlm.interpreter")
        importlib.import_module("predict_rlm.rlm_skills")
        package_available = True
        version = importlib.metadata.version("predict-rlm")
    except Exception:
        package_available = False
        version = None

    deno_available = shutil.which("deno") is not None
    return {
        "package_available": package_available,
        "deno_available": deno_available,
        "ready": package_available and deno_available,
        "version": version,
    }


def get_agent_worker_backend_settings(
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    provider: str | None = None,
) -> WorkerBackendSettings:
    """Resolve worker-backend config plus local runtime availability."""
    config = _load_org_memory_model_config(user_id=user_id, org_id=org_id)
    support = _predict_rlm_support()
    selected_provider = (
        provider
        or resolve_default_provider(user_id=user_id, org_id=org_id)
    )
    selected_provider = normalize_runtime_provider(str(selected_provider or "").strip().lower())
    raw_sub_lm = (config.get("predict_rlm_sub_lm") or os.environ.get("PREDICT_RLM_SUB_LM") or "").strip() or None

    requested_backend = _normalize_backend(config.get("agent_worker_backend"))
    if requested_backend == "auto":
        effective_backend = "predict_rlm" if support["ready"] else "native"
    elif requested_backend == "predict_rlm":
        effective_backend = "predict_rlm" if support["ready"] else "native"
    else:
        effective_backend = "native"

    fallback_reason = None
    if requested_backend == "predict_rlm" and effective_backend != "predict_rlm":
        if not support["package_available"]:
            fallback_reason = "predict-rlm package is not installed"
        elif not support["deno_available"]:
            fallback_reason = "deno is not installed on the server"
        else:
            fallback_reason = "predict-rlm runtime is unavailable"
    elif requested_backend == "auto" and effective_backend != "predict_rlm":
        if not support["package_available"]:
            fallback_reason = "predict-rlm package missing; auto fell back to native"
        elif not support["deno_available"]:
            fallback_reason = "deno missing; auto fell back to native"

    return WorkerBackendSettings(
        requested_backend=requested_backend,
        effective_backend=effective_backend,
        predict_rlm_package_available=support["package_available"],
        predict_rlm_deno_available=support["deno_available"],
        predict_rlm_ready=support["ready"],
        predict_rlm_version=support["version"],
        predict_rlm_sub_lm=_resolve_predict_rlm_sub_lm(
            raw_sub_lm,
            provider=selected_provider,
            user_id=user_id,
            org_id=org_id,
        ),
        predict_rlm_max_iterations=_normalize_positive_int(
            config.get("predict_rlm_max_iterations", os.environ.get("PREDICT_RLM_MAX_ITERATIONS")),
            _DEFAULT_MAX_ITERATIONS,
        ),
        predict_rlm_max_llm_calls=_normalize_positive_int(
            config.get("predict_rlm_max_llm_calls", os.environ.get("PREDICT_RLM_MAX_LLM_CALLS")),
            _DEFAULT_MAX_LLM_CALLS,
        ),
        fallback_reason=fallback_reason,
    )


async def async_get_agent_worker_backend_settings(
    session: AsyncSession,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    provider: str | None = None,
) -> WorkerBackendSettings:
    """Resolve worker-backend config plus local runtime availability using an async session."""
    config = await _async_load_org_memory_model_config(session, user_id=user_id, org_id=org_id)
    support = _predict_rlm_support()
    selected_provider = (
        provider
        or await async_resolve_default_provider(session, user_id=user_id, org_id=org_id)
    )
    selected_provider = normalize_runtime_provider(str(selected_provider or "").strip().lower())
    raw_sub_lm = (config.get("predict_rlm_sub_lm") or os.environ.get("PREDICT_RLM_SUB_LM") or "").strip() or None

    requested_backend = _normalize_backend(config.get("agent_worker_backend"))
    if requested_backend == "auto":
        effective_backend = "predict_rlm" if support["ready"] else "native"
    elif requested_backend == "predict_rlm":
        effective_backend = "predict_rlm" if support["ready"] else "native"
    else:
        effective_backend = "native"

    fallback_reason = None
    if requested_backend == "predict_rlm" and effective_backend != "predict_rlm":
        if not support["package_available"]:
            fallback_reason = "predict-rlm package is not installed"
        elif not support["deno_available"]:
            fallback_reason = "deno is not installed on the server"
        else:
            fallback_reason = "predict-rlm runtime is unavailable"
    elif requested_backend == "auto" and effective_backend != "predict_rlm":
        if not support["package_available"]:
            fallback_reason = "predict-rlm package missing; auto fell back to native"
        elif not support["deno_available"]:
            fallback_reason = "deno missing; auto fell back to native"

    return WorkerBackendSettings(
        requested_backend=requested_backend,
        effective_backend=effective_backend,
        predict_rlm_package_available=support["package_available"],
        predict_rlm_deno_available=support["deno_available"],
        predict_rlm_ready=support["ready"],
        predict_rlm_version=support["version"],
        predict_rlm_sub_lm=await _async_resolve_predict_rlm_sub_lm(
            session,
            raw_sub_lm,
            provider=selected_provider,
            user_id=user_id,
            org_id=org_id,
        ),
        predict_rlm_max_iterations=_normalize_positive_int(
            config.get("predict_rlm_max_iterations", os.environ.get("PREDICT_RLM_MAX_ITERATIONS")),
            _DEFAULT_MAX_ITERATIONS,
        ),
        predict_rlm_max_llm_calls=_normalize_positive_int(
            config.get("predict_rlm_max_llm_calls", os.environ.get("PREDICT_RLM_MAX_LLM_CALLS")),
            _DEFAULT_MAX_LLM_CALLS,
        ),
        fallback_reason=fallback_reason,
    )


def _canonical_model_name(model: str, *, provider: str) -> str:
    return model if "/" in model else f"{provider}/{model}"


def _strip_known_provider_prefix(model: str | None) -> str:
    value = (model or "").strip()
    for provider in DEFAULT_PROVIDER_MODEL_MAPS:
        for separator in ("/", ":"):
            prefix = f"{provider}{separator}"
            if value.startswith(prefix):
                return value[len(prefix):]
    return value


def _explicit_provider_from_model(model: str | None) -> str | None:
    """Return only a provider implied by the model string itself.

    Unlike infer_provider_from_model(), this never falls back to the active
    process default. It is used to keep helper-LM overrides inside the selected
    user/org provider boundary.
    """
    value = (model or "").strip()
    if not value:
        return None
    lowered = value.lower()
    if "/" in value:
        prefix = value.split("/", 1)[0].strip().lower()
        if prefix in DEFAULT_PROVIDER_MODEL_MAPS:
            return prefix
    if lowered.startswith("anthropic:") or lowered.startswith("claude-"):
        return "anthropic"
    if lowered.startswith("openai:") or lowered.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    bare = _strip_known_provider_prefix(value)
    for provider, defaults in DEFAULT_PROVIDER_MODEL_MAPS.items():
        if bare in defaults.values():
            return provider
    return None


def _tier_for_model_override(
    model: str | None,
    *,
    user_id: str | None,
    org_id: str | None,
) -> str | None:
    bare = _strip_known_provider_prefix(model)
    if not bare:
        return None
    checked: set[str] = set()
    for provider in DEFAULT_PROVIDER_MODEL_MAPS:
        if provider in checked:
            continue
        checked.add(provider)
        try:
            model_map = get_provider_model_map(provider, user_id=user_id, org_id=org_id)
        except Exception:
            model_map = DEFAULT_PROVIDER_MODEL_MAPS.get(provider, {})
        for tier, mapped_model in model_map.items():
            if bare == _strip_known_provider_prefix(mapped_model):
                return normalize_model_tier(tier, default=None)

    lowered = bare.lower()
    if lowered.startswith("claude-") and "opus" in lowered:
        return HIGH_MODEL_TIER
    if lowered.startswith("claude-") and "haiku" in lowered:
        return LOW_MODEL_TIER
    if lowered.startswith("claude-") and "sonnet" in lowered:
        return MEDIUM_MODEL_TIER
    if "pro" in lowered:
        return HIGH_MODEL_TIER
    if "mini" in lowered or "nano" in lowered:
        return LOW_MODEL_TIER
    if lowered.startswith("claude-") or lowered.startswith("gpt-") or lowered.startswith(("o1", "o3", "o4")):
        return MEDIUM_MODEL_TIER
    return None


async def _async_tier_for_model_override(
    session: AsyncSession,
    model: str | None,
    *,
    user_id: str | None,
    org_id: str | None,
) -> str | None:
    bare = _strip_known_provider_prefix(model)
    if not bare:
        return None
    checked: set[str] = set()
    for provider in DEFAULT_PROVIDER_MODEL_MAPS:
        if provider in checked:
            continue
        checked.add(provider)
        try:
            model_map = await async_get_provider_model_map(session, provider, user_id=user_id, org_id=org_id)
        except Exception:
            model_map = DEFAULT_PROVIDER_MODEL_MAPS.get(provider, {})
        for tier, mapped_model in model_map.items():
            if bare == _strip_known_provider_prefix(mapped_model):
                return normalize_model_tier(tier, default=None)

    lowered = bare.lower()
    if lowered.startswith("claude-") and "opus" in lowered:
        return HIGH_MODEL_TIER
    if lowered.startswith("claude-") and "haiku" in lowered:
        return LOW_MODEL_TIER
    if lowered.startswith("claude-") and "sonnet" in lowered:
        return MEDIUM_MODEL_TIER
    if "pro" in lowered:
        return HIGH_MODEL_TIER
    if "mini" in lowered or "nano" in lowered:
        return LOW_MODEL_TIER
    if lowered.startswith("claude-") or lowered.startswith("gpt-") or lowered.startswith(("o1", "o3", "o4")):
        return MEDIUM_MODEL_TIER
    return None


def _resolve_predict_rlm_sub_lm(
    configured_model: str | None,
    *,
    provider: str,
    user_id: str | None,
    org_id: str | None,
) -> str:
    """Resolve PredictRLM's helper LM inside the selected provider boundary."""
    selected_provider = normalize_runtime_provider(provider or resolve_default_provider(user_id=user_id, org_id=org_id))
    configured = (configured_model or "").strip()
    if not configured:
        return _default_sub_lm(provider=selected_provider, user_id=user_id, org_id=org_id)

    configured_provider = _explicit_provider_from_model(configured)
    if configured_provider and configured_provider != selected_provider:
        tier = _tier_for_model_override(configured, user_id=user_id, org_id=org_id) or LOW_MODEL_TIER
        if tier == "local":
            tier = LOW_MODEL_TIER
        remapped = get_model_for_tier(
            tier,
            provider=selected_provider,
            include_provider_prefix=False,
            user_id=user_id,
            org_id=org_id,
        )
        logger.info(
            "PredictRLM sub_lm provider override remapped from %s to %s/%s for selected provider %s",
            configured,
            selected_provider,
            remapped,
            selected_provider,
        )
        return _canonical_model_name(remapped, provider=selected_provider)

    return _canonical_model_name(_strip_known_provider_prefix(configured), provider=selected_provider)


async def _async_resolve_predict_rlm_sub_lm(
    session: AsyncSession,
    configured_model: str | None,
    *,
    provider: str,
    user_id: str | None,
    org_id: str | None,
) -> str:
    """Resolve PredictRLM's helper LM inside the selected provider boundary using async DB access."""
    selected_provider = normalize_runtime_provider(
        provider or await async_resolve_default_provider(session, user_id=user_id, org_id=org_id)
    )
    configured = (configured_model or "").strip()
    if not configured:
        return await _async_default_sub_lm(session, provider=selected_provider, user_id=user_id, org_id=org_id)

    configured_provider = _explicit_provider_from_model(configured)
    if configured_provider and configured_provider != selected_provider:
        tier = await _async_tier_for_model_override(session, configured, user_id=user_id, org_id=org_id) or LOW_MODEL_TIER
        if tier == "local":
            tier = LOW_MODEL_TIER
        remapped = await async_get_model_for_tier(
            session,
            tier,
            provider=selected_provider,
            include_provider_prefix=False,
            user_id=user_id,
            org_id=org_id,
        )
        logger.info(
            "PredictRLM sub_lm provider override remapped from %s to %s/%s for selected provider %s",
            configured,
            selected_provider,
            remapped,
            selected_provider,
        )
        return _canonical_model_name(remapped, provider=selected_provider)

    return _canonical_model_name(_strip_known_provider_prefix(configured), provider=selected_provider)


def _default_sub_lm(
    *,
    provider: str,
    user_id: str | None,
    org_id: str | None,
) -> str:
    return _canonical_model_name(
        get_model_for_tier(
            LOW_MODEL_TIER,
            provider=provider or resolve_default_provider(user_id=user_id, org_id=org_id),
            include_provider_prefix=False,
            user_id=user_id,
            org_id=org_id,
        ),
        provider=provider,
    )


async def _async_default_sub_lm(
    session: AsyncSession,
    *,
    provider: str,
    user_id: str | None,
    org_id: str | None,
) -> str:
    resolved_provider = provider or await async_resolve_default_provider(session, user_id=user_id, org_id=org_id)
    return _canonical_model_name(
        await async_get_model_for_tier(
            session,
            LOW_MODEL_TIER,
            provider=resolved_provider,
            include_provider_prefix=False,
            user_id=user_id,
            org_id=org_id,
        ),
        provider=provider,
    )


class _AttrDict(dict):
    """Small dict wrapper that also exposes nested values via attributes."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def model_dump(self) -> dict[str, Any]:
        return _unwrap_attr_object(self)


def _wrap_attr_object(value: Any) -> Any:
    if isinstance(value, dict):
        return _AttrDict({key: _wrap_attr_object(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_wrap_attr_object(item) for item in value]
    return value


def _unwrap_attr_object(value: Any) -> Any:
    if isinstance(value, _AttrDict):
        return {key: _unwrap_attr_object(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unwrap_attr_object(item) for item in value]
    return value


def _usage_metric(usage: Any, field: str) -> int:
    """Best-effort usage extraction across SDK object/dict variants."""
    if usage is None:
        return 0
    if isinstance(usage, dict):
        value = usage.get(field, 0)
    else:
        value = getattr(usage, field, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _instrument_predict_rlm_lm(
    lm: Any,
    *,
    session_id: str | None,
    run_id: int | None,
    label: str,
    call_counter: dict[str, Any] | None = None,
    on_llm_call: Callable[[dict[str, Any]], None] | None = None,
) -> Any:
    """Wrap an LM so every internal PredictRLM call is recorded in agent_api_calls.

    PredictRLM performs many LM invocations inside its own planning loop. Without
    this wrapper we only record a single summary row after the whole worker
    returns, which makes long-running workers opaque in telemetry.
    """
    if getattr(lm, "_illo_predict_rlm_instrumented", False):
        return lm

    counter_lock = None
    if call_counter is not None:
        counter_lock = call_counter.setdefault("_lock", threading.Lock())

    def _next_turn() -> int:
        if call_counter is None:
            return 0
        with counter_lock:
            call_counter["turn"] = int(call_counter.get("turn", 0)) + 1
            return call_counter["turn"]

    def _emit_call(response: Any, error: str | None, started_at: float, kwargs: dict[str, Any]) -> None:
        usage = getattr(response, "usage", None) if response is not None else None
        messages = kwargs.get("messages")
        prompt = kwargs.get("prompt")
        metadata = {
            "label": label,
            "model": getattr(lm, "model", None) or getattr(lm, "model_name", None),
            "latency_ms": int((time.time() - started_at) * 1000),
            "tokens_input": _usage_metric(usage, "input_tokens"),
            "tokens_output": _usage_metric(usage, "output_tokens"),
            "cache_read": _usage_metric(usage, "cache_read_input_tokens"),
            "cache_write": _usage_metric(usage, "cache_creation_input_tokens"),
            "turn_number": _next_turn(),
            "error": error,
        }
        _record_api_call(
            session_id=session_id,
            run_id=run_id,
            turn=metadata["turn_number"],
            model=metadata["model"] or label,
            tokens_input=metadata["tokens_input"],
            tokens_output=metadata["tokens_output"],
            cache_read=metadata["cache_read"],
            cache_write=metadata["cache_write"],
            context_messages=len(messages) if isinstance(messages, list) else (1 if prompt else 0),
            status="error" if error else "success",
            stop_reason=f"predict_rlm_{label}",
            latency_ms=metadata["latency_ms"],
            error=error,
        )
        if on_llm_call:
            try:
                on_llm_call(metadata)
            except Exception:
                logger.debug("PredictRLM LM-call callback failed", exc_info=True)

    original_forward = getattr(lm, "forward", None)
    if callable(original_forward):
        def wrapped_forward(*args, **kwargs):
            started_at = time.time()
            response = None
            error = None
            try:
                response = original_forward(*args, **kwargs)
                return response
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                _emit_call(response, error, started_at, kwargs)

        setattr(lm, "forward", wrapped_forward)

    original_aforward = getattr(lm, "aforward", None)
    if callable(original_aforward):
        async def wrapped_aforward(*args, **kwargs):
            started_at = time.time()
            response = None
            error = None
            try:
                response = await original_aforward(*args, **kwargs)
                return response
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                _emit_call(response, error, started_at, kwargs)

        setattr(lm, "aforward", wrapped_aforward)

    setattr(lm, "_illo_predict_rlm_instrumented", True)
    return lm


def _normalize_dspy_response_format(response_format: Any) -> dict[str, Any] | None:
    if response_format is None:
        return None

    try:
        from pydantic import BaseModel as PydanticBaseModel
    except Exception:  # pragma: no cover - pydantic is a core dependency
        PydanticBaseModel = None  # type: ignore[assignment]

    if PydanticBaseModel and isinstance(response_format, type) and issubclass(response_format, PydanticBaseModel):
        return {
            "name": response_format.__name__,
            "type": "json_schema",
            "schema": response_format.model_json_schema(),
        }
    if isinstance(response_format, dict):
        return dict(response_format)
    return None


def _collect_openai_stream_response(stream: Any) -> dict[str, Any]:
    final_response = None
    streamed_output_items: dict[int, Any] = {}
    collected_text: list[str] = []

    for event in stream:
        event_type = event.get("type", "")
        if event_type in {"response.output_item.added", "response.output_item.done"}:
            output_index = event.get("output_index")
            item = event.get("item")
            if isinstance(output_index, int) and item is not None:
                streamed_output_items[output_index] = item
            continue
        if event_type in {"response.output_text.delta", "response.text.delta"}:
            delta = event.get("delta") or ""
            if delta:
                collected_text.append(delta)
            continue
        if event_type in {"response.output_text.done", "response.text.done"}:
            text = event.get("text") or ""
            if text and not collected_text:
                collected_text.append(text)
            continue
        if event_type == "response.completed":
            final_response = event.get("response")

    if final_response is None:
        joined = "".join(collected_text)
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": joined}],
                }
            ] if joined else [],
            "usage": {},
        }

    if streamed_output_items:
        return _merge_streamed_output_into_response(final_response, streamed_output_items)
    return final_response


def _extract_system_blocks_and_messages(
    messages: list[dict[str, Any]] | None,
    prompt: str | None,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    system_blocks: list[dict[str, Any]] = []
    input_messages: list[dict[str, Any]] = []

    for message in messages or []:
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                system_blocks.append({"type": "text", "text": content.strip()})
            continue
        input_messages.append(dict(message))

    if not input_messages:
        input_messages = [{"role": "user", "content": prompt or ""}]

    if not system_blocks:
        system_blocks = [{"type": "text", "text": "Follow the user's request exactly and return the requested output."}]

    return system_blocks, input_messages


def _build_openai_illo_dspy_lm(
    *,
    model: str,
    user_id: str | None,
    org_id: str | None,
    session_id: str | None,
) -> Any:
    import dspy
    required_auth_mode = _required_openai_auth_mode(model)

    class IlloOpenAIDSPyLM(dspy.LM):
        def __init__(self, model_name: str):
            super().__init__(model_name, model_type="responses", cache=False, max_tokens=None)
            self._resolved_llm = resolve_llm_client(
                user_id=user_id,
                org_id=org_id,
                provider="openai",
                auth_mode=required_auth_mode,
            )
            self._provider = get_provider(self._resolved_llm.provider, self._resolved_llm.client)

        def forward(
            self,
            prompt: str | None = None,
            messages: list[dict[str, Any]] | None = None,
            **kwargs: Any,
        ):
            request_kwargs = dict(kwargs)
            response_format = _normalize_dspy_response_format(request_kwargs.pop("response_format", None))
            reasoning_effort = request_kwargs.pop("reasoning_effort", None)
            max_tokens = request_kwargs.pop("max_tokens", None)
            max_completion_tokens = request_kwargs.pop("max_completion_tokens", None)
            system_blocks, input_messages = _extract_system_blocks_and_messages(messages, prompt)

            extra_headers = self._resolved_llm.build_request_headers(session_id=session_id)

            request = LLMRequest(
                model=self.model,
                messages=input_messages,
                max_output_tokens=max_completion_tokens if max_completion_tokens is not None else max_tokens,
                system=system_blocks,
                reasoning_effort=reasoning_effort,
                extra_headers=extra_headers or None,
                response_format=response_format,
            )
            payload = self._provider._translate_request(request)
            payload["stream"] = True
            stream = self._provider._create_with_fallback(payload)
            response = _collect_openai_stream_response(stream)
            return _wrap_attr_object(response)

        async def aforward(
            self,
            prompt: str | None = None,
            messages: list[dict[str, Any]] | None = None,
            **kwargs: Any,
        ):
            return await asyncio.to_thread(
                self.forward,
                prompt,
                messages,
                **kwargs,
            )

    return IlloOpenAIDSPyLM(model)


def _build_predict_rlm_lm(
    *,
    model: str,
    provider: str,
    user_id: str | None,
    org_id: str | None,
    session_id: str | None = None,
) -> Any:
    import dspy
    provider = normalize_runtime_provider(provider)

    if provider == "openai":
        return _build_openai_illo_dspy_lm(
            model=model,
            user_id=user_id,
            org_id=org_id,
            session_id=session_id,
        )

    if provider == "anthropic":
        token, _ = _resolve_key_from_db(user_id=user_id, org_id=org_id, provider="anthropic")
        if not token:
            token, _ = _resolve_key_from_env(provider="anthropic")
        if not token:
            raise RuntimeError(
                "PredictRLM requires an Anthropic API key. "
                "Add one in Settings. ANTHROPIC_API_KEY is only a development fallback."
            )
        return dspy.LM(model, api_key=token, cache=False)

    raise RuntimeError(f"PredictRLM DSPy LM construction does not support provider '{provider}'")


def _tool_parameter_annotation(schema: dict[str, Any]) -> type:
    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        raw_type = next((item for item in raw_type if item != "null"), "string")
    return _SCHEMA_TYPE_MAP.get(raw_type, str)


def _tool_docstring(tool_name: str, definition: dict[str, Any]) -> str:
    lines = [definition.get("description") or f"Invoke the {tool_name} Illo tool."]
    properties = (definition.get("input_schema") or {}).get("properties") or {}
    if properties:
        lines.append("")
        lines.append("Args:")
        for param_name, schema in properties.items():
            type_name = schema.get("type", "string")
            description = schema.get("description") or ""
            default = schema.get("default", ...)
            suffix = f" Default: {default!r}." if default is not ... else ""
            lines.append(f"    {param_name} ({type_name}): {description}{suffix}".rstrip())
    return "\n".join(lines)


def _make_async_tool_wrapper(
    *,
    tool_name: str,
    handler: Callable[..., Any],
    definition: dict[str, Any],
    threadlocal_context: dict[str, Any],
    on_tool_call: Callable[[str, dict, str], None] | None,
    run_id: int | None,
    idea_id: str | None,
    tool_call_source: str,
):
    properties = list(((definition.get("input_schema") or {}).get("properties") or {}).items())
    required = set(((definition.get("input_schema") or {}).get("required") or []))
    params: list[str] = []
    annotations: dict[str, Any] = {"return": str}
    namespace: dict[str, Any] = {
        "_run": None,
        "json": json,
        "Any": Any,
    }

    async def _run(**kwargs):
        try:
            result = await asyncio.to_thread(
                _invoke_tool_handler,
                handler,
                kwargs,
                threadlocal_context,
            )
        except Exception as exc:
            result_text = f"Error: {exc}"
            safe_result_text = "[secret redacted]" if tool_name == "brain_vault" else result_text
            if on_tool_call:
                on_tool_call(tool_name, kwargs, safe_result_text)
            if run_id and idea_id:
                await async_record_tool_call(
                    run_id,
                    idea_id,
                    tool_name,
                    kwargs,
                    safe_result_text,
                    source=tool_call_source,
                )
            raise

        result_text = json.dumps(result, default=str)
        safe_result_text = "[secret redacted]" if tool_name == "brain_vault" else result_text
        if on_tool_call:
            on_tool_call(tool_name, kwargs, safe_result_text)
        if run_id and idea_id:
            await async_record_tool_call(
                run_id,
                idea_id,
                tool_name,
                kwargs,
                safe_result_text,
                source=tool_call_source,
            )
        return result

    namespace["_run"] = _run

    ordered_properties = [
        *[(name, schema) for name, schema in properties if name in required and schema.get("default", ...) is ...],
        *[(name, schema) for name, schema in properties if not (name in required and schema.get("default", ...) is ...)],
    ]

    for param_name, schema in ordered_properties:
        default = schema.get("default", ...)
        annotation = _tool_parameter_annotation(schema)
        annotations[param_name] = annotation
        if param_name in required and default is ...:
            params.append(param_name)
        else:
            namespace[f"__default_{param_name}"] = None if default is ... else default
            params.append(f"{param_name}=__default_{param_name}")

    signature_args = ", ".join(params)
    kwargs_expr = ", ".join(f"'{name}': {name}" for name, _ in properties)
    function_src = (
        f"async def {tool_name}({signature_args}):\n"
        f"    return await _run(**{{{kwargs_expr}}})\n"
    )
    exec_namespace = dict(namespace)
    exec(function_src, exec_namespace)  # noqa: S102 - controlled local generation from trusted schema
    fn = exec_namespace[tool_name]
    fn.__annotations__ = annotations
    fn.__doc__ = _tool_docstring(tool_name, definition)
    return fn


def build_predict_rlm_tools(
    *,
    tool_definitions: list[dict[str, Any]],
    tool_handlers: dict[str, Callable[..., Any]] | None,
    on_tool_call: Callable[[str, dict, str], None] | None,
    run_id: int | None,
    idea_id: str | None,
    tool_call_source: str,
    threadlocal_context: dict[str, Any],
) -> dict[str, Callable[..., Any]]:
    """Translate Illo tool schemas + handlers into PredictRLM async tools."""
    handlers = tool_handlers or {}
    tools: dict[str, Callable[..., Any]] = {}
    for definition in tool_definitions:
        tool_name = definition.get("name")
        handler = handlers.get(tool_name)
        if not tool_name or not callable(handler):
            continue
        tools[tool_name] = _make_async_tool_wrapper(
            tool_name=tool_name,
            handler=handler,
            definition=definition,
            threadlocal_context=threadlocal_context,
            on_tool_call=on_tool_call,
            run_id=run_id,
            idea_id=idea_id,
            tool_call_source=tool_call_source,
        )
    return tools


def invoke_predict_rlm_agent(
    spec,
    *,
    provider: str,
    backend_settings: WorkerBackendSettings,
    user_id: str | None = None,
    org_id: str | None = None,
) -> AgentResult:
    """Execute a worker via PredictRLM and normalize back to AgentResult."""
    if not backend_settings.predict_rlm_ready:
        reason = backend_settings.fallback_reason or "predict-rlm runtime is not ready"
        raise RuntimeError(reason)

    from predict_rlm.interpreter import JspiInterpreter
    from predict_rlm.predict_rlm import PredictRLM
    from predict_rlm.rlm_skills import Skill

    start_time = time.time()
    threadlocal_context = vars(_agent_context).copy()
    tool_calls_made: list[str] = []
    llm_call_counter: dict[str, Any] = {"turn": 0}
    llm_usage_totals: dict[str, int] = {
        "tokens_input": 0,
        "tokens_output": 0,
        "cache_read": 0,
        "cache_write": 0,
    }

    def _track_tool_call(name: str, args: dict, result_text: str) -> None:
        tool_calls_made.append(name)
        if spec.on_tool_call:
            spec.on_tool_call(name, args, result_text)

    def _track_llm_call(metadata: dict[str, Any]) -> None:
        llm_usage_totals["tokens_input"] += int(metadata.get("tokens_input") or 0)
        llm_usage_totals["tokens_output"] += int(metadata.get("tokens_output") or 0)
        llm_usage_totals["cache_read"] += int(metadata.get("cache_read") or 0)
        llm_usage_totals["cache_write"] += int(metadata.get("cache_write") or 0)
        if not spec.on_stream_activity:
            return
        model = (metadata.get("model") or "").split("/")[-1] or metadata.get("label")
        spec.on_stream_activity(
            f"PredictRLM {metadata.get('label')} call {metadata.get('turn_number')} "
            f"({model}, {metadata.get('latency_ms')}ms)"
        )

    tools = build_predict_rlm_tools(
        tool_definitions=spec.tools or [],
        tool_handlers=spec.tool_handlers,
        on_tool_call=_track_tool_call,
        run_id=spec.run_id,
        idea_id=spec.idea_id,
        tool_call_source=spec.tool_call_source,
        threadlocal_context=threadlocal_context,
    )

    main_provider = infer_provider_from_model(spec.model, default=provider)
    main_model = _canonical_model_name(spec.model, provider=main_provider)
    sub_lm = _resolve_predict_rlm_sub_lm(
        backend_settings.predict_rlm_sub_lm,
        provider=main_provider,
        user_id=user_id,
        org_id=org_id,
    )
    main_lm = _build_predict_rlm_lm(
        model=main_model,
        provider=infer_provider_from_model(main_model, default=provider),
        user_id=user_id,
        org_id=org_id,
        session_id=spec.session_id,
    )
    sub_lm_instance = _build_predict_rlm_lm(
        model=sub_lm,
        provider=infer_provider_from_model(sub_lm, default=provider),
        user_id=user_id,
        org_id=org_id,
        session_id=spec.session_id,
    )
    main_lm = _instrument_predict_rlm_lm(
        main_lm,
        session_id=spec.session_id,
        run_id=spec.run_id,
        label="main",
        call_counter=llm_call_counter,
        on_llm_call=_track_llm_call,
    )
    sub_lm_instance = _instrument_predict_rlm_lm(
        sub_lm_instance,
        session_id=spec.session_id,
        run_id=spec.run_id,
        label="sub",
        call_counter=llm_call_counter,
        on_llm_call=_track_llm_call,
    )

    output = ""
    success = False
    error = None
    interpreter = JspiInterpreter(
        preinstall_packages=False,
        allowed_domains=[],
        enable_read_paths=[],
        enable_write_paths=[],
        sync_files=False,
    )

    rlm = PredictRLM(
        "task: str -> output: str",
        lm=main_lm,
        sub_lm=sub_lm_instance,
        max_iterations=backend_settings.predict_rlm_max_iterations,
        max_llm_calls=backend_settings.predict_rlm_max_llm_calls,
        tools=tools,
        interpreter=interpreter,
        skills=[
            Skill(
                name="illo-worker",
                instructions=(
                    f"{spec.system_prompt.strip()}\n\n"
                    "Use the provided Illo tools for filesystem, shell, browser, memory, and worker orchestration work. "
                    "Do not import or call subprocess, os.system, multiprocessing, or other process APIs inside the JSPI interpreter; "
                    "that runtime cannot spawn OS processes. Use the provided Illo shell/file tools instead. "
                    "For independent read/search/fetch operations, prefer the `parallel_tool_batch` tool so the runtime "
                    "executes them concurrently. Use asyncio.gather() only when you are already writing a custom script "
                    "via run_script and need in-script orchestration."
                ),
            )
        ],
        output_dir=spec.workspace_root or None,
    )

    try:
        prediction = rlm(task=spec.message)
        output = getattr(prediction, "output", "") or ""
        if not isinstance(output, str):
            output = json.dumps(output, default=str)
        success = True
    except Exception as exc:
        error = str(exc)
        logger.warning("PredictRLM worker %s failed: %s", spec.session_id, exc)
    finally:
        try:
            interpreter.shutdown()
        except Exception:
            pass

    duration_ms = int((time.time() - start_time) * 1000)
    _record_api_call(
        session_id=spec.session_id,
        run_id=spec.run_id,
        turn=int(llm_call_counter.get("turn", 0)) + 1,
        model=main_model,
        context_messages=1,
        system_prompt_chars=len(spec.system_prompt or ""),
        status="success" if success else "error",
        stop_reason="predict_rlm_summary",
        latency_ms=duration_ms,
        error=error,
    )

    return AgentResult(
        output=output,
        success=success,
        session_id=spec.session_id,
        tokens_input=llm_usage_totals["tokens_input"],
        tokens_output=llm_usage_totals["tokens_output"],
        tokens_cache_read=llm_usage_totals["cache_read"],
        tokens_cache_creation=llm_usage_totals["cache_write"],
        duration_sec=int(duration_ms / 1000),
        tool_calls=tool_calls_made,
        error=error,
    )
