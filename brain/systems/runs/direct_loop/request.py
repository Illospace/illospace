"""Provider request construction and prompt-cache policy for the agent runtime."""

from __future__ import annotations

import copy
import json
import re
from hashlib import sha256

from brain.platform.integrations.providers import LLMRequest

_PROMPT_CACHE_KEY_MAX_LEN = 64
_PROMPT_CACHE_KEY_DIGEST_LEN = 24
_PROMPT_CACHE_KEY_PREFIX = "illo"


def normalize_model_name(model: str) -> str:
    """Strip provider prefixes before passing model names to provider SDKs."""

    for prefix in ("anthropic/", "openai/", "anthropic:", "openai:"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def build_system_blocks(_llm, system_prompt: str, cache: bool) -> list[dict] | None:
    """Build the system parameter with optional caching.

    The cache flag is accepted for direct-agent helper parity. The
    Anthropic cache marker is applied separately so provider-specific policy
    stays explicit at the call site.
    """

    blocks = []
    if system_prompt:
        blocks.append({"type": "text", "text": system_prompt})
    return blocks or None


def apply_anthropic_cache_breakpoint(blocks: list[dict] | None, cache: bool) -> list[dict] | None:
    """Attach Anthropic cache hints to the final system block only."""

    if cache and blocks:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def apply_provider_system_cache_policy(
    provider_name: str,
    system: list[dict] | str | None,
    cache: bool,
) -> list[dict] | str | None:
    """Apply native provider cache hints to the stable system scaffold."""

    if not cache or not system:
        return system
    if provider_name != "anthropic":
        return system
    blocks: list[dict]
    if isinstance(system, str):
        blocks = [{"type": "text", "text": system}]
    else:
        blocks = [dict(block) for block in system]
    return apply_anthropic_cache_breakpoint(blocks, True)


def mark_tools_cacheable(tools: list[dict]) -> list[dict]:
    """Add cache_control to the last tool so the full Anthropic tool block caches."""

    if not tools:
        return tools
    copied_tools = [tool for tool in tools]
    copied_tools[-1] = {**copied_tools[-1], "cache_control": {"type": "ephemeral"}}
    return copied_tools


def derive_prompt_cache_key(
    session_id: str,
    system: list[dict] | str | None,
    tools: list[dict] | None,
    persist_session: bool,
    operation_type: str | None = None,
) -> str:
    """Build a stable prompt cache key for similar repeated prefixes.

    Providers combine cache hints with the actual prompt prefix, so this key is
    a routing identity, not a correctness boundary. Keep it stable across
    runs that share the same system/tool scaffold; per-run session ids
    scatter identical prefixes across cache buckets.
    """

    cache_scope = _prompt_cache_scope(session_id, persist_session, operation_type)
    scaffold = {
        "cache_scope": cache_scope,
        "operation_type": operation_type or "",
        "system": system or "",
        "tools": [
            {
                "name": tool.get("name"),
                "description": tool.get("description"),
                "input_schema": tool.get("input_schema"),
                "parameters": tool.get("parameters"),
            }
            for tool in (tools or [])
        ],
    }
    digest = sha256(json.dumps(scaffold, sort_keys=True, default=str).encode("utf-8")).hexdigest()[
        :_PROMPT_CACHE_KEY_DIGEST_LEN
    ]
    max_scope_len = (
        _PROMPT_CACHE_KEY_MAX_LEN
        - len(_PROMPT_CACHE_KEY_PREFIX)
        - len(digest)
        - 2
    )
    scope_hint = re.sub(r"[^A-Za-z0-9:_-]+", "-", cache_scope).strip("-:_") or "agent"
    scope_hint = scope_hint[:max_scope_len]
    return f"{_PROMPT_CACHE_KEY_PREFIX}:{scope_hint}:{digest}"


def derive_openai_cache_key(
    session_id: str,
    system: list[dict] | str | None,
    tools: list[dict] | None,
    persist_session: bool,
    operation_type: str | None = None,
) -> str:
    """Compatibility wrapper for OpenAI-specific call sites/tests."""

    return derive_prompt_cache_key(
        session_id,
        system,
        tools,
        persist_session,
        operation_type=operation_type,
    )


def _prompt_cache_scope(session_id: str, persist_session: bool, operation_type: str | None) -> str:
    """Return a coarse routing scope that preserves cache reuse across runs."""

    explicit = (operation_type or "").strip().lower()
    if explicit in {"scout", "coordinator", "worker", "memory_extraction", "verifier"}:
        return explicit

    normalized_session = (session_id or "").strip().lower()
    if normalized_session.startswith("scout-"):
        return "scout"
    if normalized_session.startswith("run-verifier:"):
        return "verifier"
    if normalized_session.startswith("agent-worker-"):
        return "worker"
    if normalized_session.startswith("coordinator-"):
        return "coordinator"
    if normalized_session.startswith("memory-"):
        return "memory_extraction"
    if persist_session:
        return "session"
    return "ephemeral"


def get_extended_prompt_cache_retention(model: str) -> str | None:
    """Return an extended retention hint for OpenAI models that support it."""

    normalized = normalize_model_name(model)
    supported_prefixes = (
        "gpt-5",
        "gpt-4.1",
    )
    if normalized.startswith(supported_prefixes):
        return "24h"
    return None


def get_openai_cache_retention(model: str) -> str | None:
    """Compatibility wrapper for OpenAI retention call sites/tests."""

    return get_extended_prompt_cache_retention(model)


def provider_prompt_cache_hints(
    provider_name: str,
    model: str,
    session_id: str,
    system: list[dict] | str | None,
    tools: list[dict] | None,
    persist_session: bool,
    cache: bool,
    operation_type: str | None = None,
) -> tuple[str | None, str | None]:
    """Return provider-native prompt cache identity and retention hints."""

    if not cache:
        return None, None
    if provider_name != "openai":
        return None, None
    return (
        derive_prompt_cache_key(
            session_id,
            system,
            tools,
            persist_session,
            operation_type=operation_type,
        ),
        get_extended_prompt_cache_retention(model),
    )


def apply_provider_request_cache_policy(
    provider_name: str,
    model: str,
    session_id: str,
    system: list[dict] | str | None,
    tools: list[dict] | None,
    persist_session: bool,
    cache: bool,
    operation_type: str | None = None,
) -> tuple[list[dict] | None, str | None, str | None]:
    """Apply provider-native cache hints for request-level prompt scaffolds."""

    request_tools = tools
    if cache and provider_name == "anthropic":
        request_tools = mark_tools_cacheable(tools) if tools else tools
    cache_key, cache_retention = provider_prompt_cache_hints(
        provider_name,
        model,
        session_id,
        system,
        tools,
        persist_session,
        cache,
        operation_type=operation_type,
    )
    return request_tools, cache_key, cache_retention


def default_max_output_tokens(provider_name: str, max_tokens: int) -> int | None:
    """Only force a default output cap for providers that need one."""

    if provider_name == "openai":
        return None
    return max_tokens


def build_api_request(
    model: str,
    messages: list,
    max_tokens: int,
    system: list | None,
    tools: list | None,
    reasoning_effort: str | None,
    extra_headers: dict | None,
    provider_name: str,
    session_id: str,
    persist_session: bool,
    cache_tools: bool = False,
    operation_type: str | None = None,
) -> LLMRequest:
    """Build a provider-neutral request for the runtime."""

    request_max_tokens = default_max_output_tokens(provider_name, max_tokens)
    request_tools, cache_key, cache_retention = apply_provider_request_cache_policy(
        provider_name,
        model,
        session_id,
        system,
        tools,
        persist_session,
        cache_tools,
        operation_type=operation_type,
    )

    return LLMRequest(
        model=model,
        messages=copy.deepcopy(messages),
        max_output_tokens=request_max_tokens,
        system=system,
        tools=request_tools,
        reasoning_effort=reasoning_effort,
        cache_key=cache_key,
        cache_retention=cache_retention,
        extra_headers=dict(extra_headers) if extra_headers else None,
        operation_type=operation_type,
    )


def infer_provider_operation_type(
    *,
    session_id: str,
    tool_call_source: str,
    metadata: dict,
) -> str:
    explicit = metadata.get("provider_operation_type") or metadata.get("role")
    if explicit in {"scout", "coordinator", "worker", "memory_extraction", "verifier"}:
        return str(explicit)
    if session_id.startswith("scout-"):
        return "scout"
    if tool_call_source.startswith("worker:") or session_id.startswith("agent-worker-"):
        return "worker"
    if session_id.startswith("coordinator-"):
        return "coordinator"
    return "coordinator"


def response_has_text(response) -> bool:
    """Return True when the assistant response includes any non-empty text block."""

    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) != "text":
            continue
        text = getattr(block, "text", "") or ""
        if text.strip():
            return True
    return False
