"""Illo Brain — Agent Loop.

Provider-neutral agent loop. Provides tool use, prompt caching,
conversation persistence, and configurable model/thinking per skill tier.

The agent loop follows the standard pattern:
    messages.create → check stop_reason → if tool_use, execute tools,
    append tool_result, loop → if end_turn, return.

Usage:
    from brain.systems.runs.direct_agent import run_agent, BRAIN_TOOLS, COORDINATOR_TOOLS

    result = run_agent(
        message="fix the timeout bug",
        system_prompt="You are a debugging expert.",
        model="openai/gpt-5.4",
        thinking="medium",
        tools=BRAIN_TOOLS,
    )

Public release note: internal issue links were removed from source comments.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import time
import uuid
from typing import Callable

from brain.platform.integrations.llm import (
    resolve_llm_client,
    _degrade_betas,
)
from brain.platform.integrations.providers import get_provider
from brain.platform.integrations.providers import ContentBlockType, LLMRequest, MessageRole, StopReason
from brain.platform.providers.model_policy import (
    MODEL_TIERS,
    get_default_model,
    get_model_for_tier,
    infer_provider_from_model,
    normalize_model_tier,
    resolve_default_provider,
)
from brain.systems.runs.introspection import required_introspection_tool as resolve_required_introspection_tool
from brain.systems.runs.direct_loop.final_reply import (
    cache_final_reply_review,
    cached_final_reply_review,
    extract_latest_user_intent,
    normalize_final_reply_candidate,
    parse_checker_payload,
)
from brain.systems.runs.direct_loop.final_reply_checker import (
    review_candidate_final_reply as _runtime_review_candidate_final_reply,
    review_final_reply_once as _runtime_review_final_reply_once,
)
from brain.systems.runs.direct_loop.gates import (
    GateState as _GateState,
    check_gate_violations as _runtime_check_gate_violations,
)
from brain.systems.runs.direct_loop.loop_control import (
    _detect_stuck_loop,
    _inject_nudges,
)
from brain.systems.runs.direct_loop.request import (
    apply_anthropic_cache_breakpoint as _runtime_apply_anthropic_cache_breakpoint,
    apply_provider_system_cache_policy as _runtime_apply_provider_system_cache_policy,
    build_api_request as _runtime_build_api_request,
    build_system_blocks as _runtime_build_system_blocks,
    derive_openai_cache_key as _runtime_derive_openai_cache_key,
    derive_prompt_cache_key as _runtime_derive_prompt_cache_key,
    get_extended_prompt_cache_retention as _runtime_get_extended_prompt_cache_retention,
    get_openai_cache_retention as _runtime_get_openai_cache_retention,
    infer_provider_operation_type as _runtime_infer_provider_operation_type,
    mark_tools_cacheable as _runtime_mark_tools_cacheable,
    response_has_text as _runtime_response_has_text,
)
from brain.systems.runs.direct_loop.result import (
    AgentResult,
    _TokenAccumulator,
    make_result as _runtime_make_result,
)
from brain.systems.runs.direct_loop.context_recovery import (
    context_overflow_payload,
    is_context_overflow_error,
)
from brain.systems.runs.direct_loop.retry import api_call_with_retry as _runtime_api_call_with_retry
from brain.systems.runs.direct_loop.session_effects import (
    _auto_encode_if_needed,
    _memory_org_for_user,
    apply_agent_session_side_effects as _runtime_apply_agent_session_side_effects,
)
from brain.systems.runs.direct_loop.state import AgentLoopState
from brain.systems.runs.direct_loop.streaming import streaming_call as _runtime_streaming_call
from brain.systems.runs.execution_context import (
    bind_agent_context,
    current_agent_context,
)
from brain.systems.runs.direct_loop.telemetry import record_api_call as _record_api_call
from brain.systems.runs.direct_loop.tool_execution import (
    PendingToolCall as _PendingToolCall,
    ResolvedToolCall as _ResolvedToolCall,
    emit_resolved_tool_call as _runtime_emit_resolved_tool_call,
    execute_parallel_tool_batch as _runtime_execute_parallel_tool_batch,
    execute_tool_calls as _runtime_execute_tool_calls,
    invoke_tool_handler as _runtime_invoke_tool_handler,
    resolve_tool_call as _runtime_resolve_tool_call,
    run_tool_awaitable as _runtime_run_tool_awaitable,
)
from brain.systems.runs.tool_catalog.registry import parallel_safe_tool_names

logger = logging.getLogger("agent")

# ── Re-exports from submodules ────────────────────────────────
# All production code imports from brain.systems.runs.direct_agent — these
# Re-export the split runtime helpers through this module's public API.

from brain.systems.runs.tool_definitions import (  # noqa: F401
    BRAIN_TOOLS,
    EXEC_TOOLS,
    WORKER_TOOLS,
    COORDINATOR_TOOLS,
    CORTEX_REPLY_TOOL,
    CORTEX_VISUAL_REPLY_TOOL,
    MY_ACTIVITY_TOOL,
    _BRAIN_TOOL_NAMES,
    _GATED_TOOL_NAMES,
)

from brain.systems.runs.tool_handlers import (  # noqa: F401
    _build_final_reply_check_context,
    _get_tool_handlers,
    get_tools_with_extended,
    WORKSPACE_ROOT,
    _resolve_path,
    _handle_exec_command,
    _handle_run_script,
    _handle_read_file,
    _handle_write_file,
    _handle_edit_file,
    _handle_search_files,
    _handle_list_files,
    _handle_manage_cycle,
    _handle_my_activity,
    _handle_cortex_reply,
    _handle_cortex_visual_reply,
)

_agent_context = current_agent_context()

from brain.systems.sessions import (  # noqa: F401
    _sanitize_tool_pairs,
    _load_session,
    _load_session_handoff,
    _save_session,
    _save_session_handoff,
    _strip_thinking_from_messages,
    _summarize_trimmed_messages,
    _clear_message_cache_breakpoints,
    _set_cache_breakpoint,
    _content_to_dicts,
    _sanitize_content_blocks,
    _ALLOWED_BLOCK_FIELDS,
)

from brain.systems.sessions.harvest import (  # noqa: F401
    _harvest_session,
    _extract_text,
)


# ── Client ───────────────────────────────────────────────────
# All client creation goes through brain.platform.integrations.llm.resolve_llm_client().
# No singleton, no ALLOW_* flags, no filesystem credential files.

def _normalize_model(model: str) -> str:
    """Strip provider prefix before passing a model name to the provider SDK."""
    for prefix in ("anthropic/", "openai/", "anthropic:", "openai:"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def _required_openai_auth_mode(model: str) -> str | None:
    """Return the OpenAI auth mode required by model availability."""
    return "chatgpt" if _normalize_model(model).lower() == "gpt-5.5" else None


# ── Constants ──────────────────────────────────────────────────

_MAX_PERSISTED_MESSAGES = 40
_DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
_RESEARCH_TOOL_NAMES = frozenset({
    "list_files", "read_file", "search_files", "brain_recall", "project_context",
})
_RESEARCH_BUDGET = 6
_MAX_PARALLEL_TOOL_CALLS = int(os.environ.get("AGENT_MAX_PARALLEL_TOOL_CALLS", "10"))
_PARALLEL_SAFE_TOOL_NAMES = parallel_safe_tool_names(scope="agent")
_AGENT_CACHE_DEBUG = os.environ.get("AGENT_CACHE_DEBUG", "").lower() in {"1", "true", "yes", "on"}


# ── Extracted Helpers ──────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _metadata_int(metadata: dict, key: str, default: int) -> int:
    try:
        value = metadata.get(key)
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _initial_user_content(message: str, metadata: dict) -> str | list[dict]:
    """Attach immediate thread files/images to the first user turn."""

    context = metadata.get("thread_attachment_context")
    if not isinstance(context, dict):
        for container_key in ("target_ref", "workspace_ref"):
            container = metadata.get(container_key)
            if isinstance(container, dict) and isinstance(container.get("thread_attachment_context"), dict):
                context = container.get("thread_attachment_context")
                break
    if not isinstance(context, dict):
        return message
    try:
        from brain.systems.cortex.thread_attachments import initial_user_content_blocks

        return initial_user_content_blocks(message, context)
    except Exception as exc:
        logger.warning("thread_attachment_context_initialization_failed: %s", exc)
        return message


def _messages_without_inline_attachment_binary(messages: list[dict]) -> list[dict]:
    """Keep persisted sessions useful without storing base64 image payloads."""

    sanitized: list[dict] = []
    for message in messages:
        item = dict(message)
        content = item.get("content")
        if isinstance(content, list):
            next_blocks: list[dict] = []
            for block in content:
                if not isinstance(block, dict):
                    next_blocks.append(block)
                    continue
                next_block = dict(block)
                source = next_block.get("source")
                if next_block.get("type") == "image" and isinstance(source, dict) and source.get("data"):
                    next_block["source"] = {
                        "type": "redacted",
                        "media_type": source.get("media_type") or source.get("mime_type"),
                        "description": "Inline image data omitted from persisted session.",
                    }
                next_blocks.append(next_block)
            item["content"] = next_blocks
        sanitized.append(item)
    return sanitized


def _agent_context_window_tokens(model: str) -> int:
    """Return the active context window used for local compaction decisions."""
    from brain.systems.context.budget import resolve_model_context_budget

    return resolve_model_context_budget(model=model).context_window_tokens


def _agent_auto_compact_token_limit(model: str) -> int:
    from brain.systems.context.budget import resolve_model_context_budget

    return resolve_model_context_budget(model=model).auto_compact_threshold_tokens


def _agent_auto_compact_target_tokens(model: str) -> int:
    from brain.systems.context.budget import resolve_model_context_budget

    return resolve_model_context_budget(model=model).target_tokens


def _extract_latest_user_intent(message: str) -> str:
    """Extract the latest user request from coordinator task wrappers when present."""
    return extract_latest_user_intent(message)


def _parse_checker_payload(raw_output: str) -> dict | None:
    """Parse checker output as either compact tokens or structured JSON."""
    return parse_checker_payload(raw_output)


def _normalize_final_reply_candidate(candidate_output: str) -> str:
    """Normalize candidate text so identical replies reuse the same checker verdict."""
    return normalize_final_reply_candidate(candidate_output)


def _get_cached_final_reply_review(candidate_output: str) -> dict | None:
    """Return a cached checker verdict for the same candidate reply when present."""
    return cached_final_reply_review(_agent_context, candidate_output)


def _cache_final_reply_review(candidate_output: str, review: dict) -> dict:
    """Persist the checker verdict for the current candidate reply on AgentRun context."""
    return cache_final_reply_review(_agent_context, candidate_output, review)


def review_candidate_final_reply(
    *,
    user_request: str,
    candidate_output: str,
    execution_context: str | None = None,
    intent_profile: dict | None = None,
    user_id: str | None = None,
    provider=None,
    llm=None,
    model: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Run the final-reply checker and return a structured approval decision."""
    return _runtime_review_candidate_final_reply(
        user_request=user_request,
        candidate_output=candidate_output,
        execution_context=execution_context,
        intent_profile=intent_profile,
        user_id=user_id,
        provider=provider,
        llm=llm,
        model=model,
        session_id=session_id,
        normalize_model=_normalize_model,
        init_llm=_init_llm,
        build_request=_build_api_request,
        extract_text=_extract_text,
        content_to_dicts=_content_to_dicts,
    )


def review_final_reply_once(
    *,
    user_request: str,
    candidate_output: str,
    execution_context: str | None = None,
    intent_profile: dict | None = None,
    user_id: str | None = None,
    provider=None,
    llm=None,
    model: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Review a candidate final reply once per unique candidate text."""
    return _runtime_review_final_reply_once(
        user_request=user_request,
        candidate_output=candidate_output,
        execution_context=execution_context,
        intent_profile=intent_profile,
        user_id=user_id,
        provider=provider,
        llm=llm,
        model=model,
        session_id=session_id,
        agent_context=_agent_context,
        review_candidate=review_candidate_final_reply,
    )


def _init_llm(
    user_id: str | None,
    session_id: str,
    model: str,
    *,
    org_id: str | None = None,
):
    """Resolve LLM client, provider, and extra headers."""
    default_provider = resolve_default_provider(user_id=user_id, org_id=org_id)
    requested_provider = infer_provider_from_model(model, default=default_provider)
    llm = resolve_llm_client(
        user_id=user_id,
        org_id=org_id,
        provider=requested_provider,
        auth_mode=_required_openai_auth_mode(model) if requested_provider == "openai" else None,
    )
    provider = get_provider(llm.provider, llm.client)
    extra_headers = llm.build_request_headers(session_id=session_id)
    logger.info(
        "Agent %s: provider=%s, source=%s, auth_mode=%s, token=%s…, oauth=%s",
        session_id,
        llm.provider,
        llm.source,
        getattr(llm, "auth_mode", None),
        llm.token_prefix,
        llm.is_oauth,
    )
    return llm, provider, extra_headers


def _build_system_blocks(llm, system_prompt: str, cache: bool) -> list[dict] | None:
    """Build the system parameter with optional caching."""
    return _runtime_build_system_blocks(llm, system_prompt, cache)


def _apply_anthropic_cache_breakpoint(blocks: list[dict] | None, cache: bool) -> list[dict] | None:
    """Attach Anthropic cache hints to the final system block only."""
    return _runtime_apply_anthropic_cache_breakpoint(blocks, cache)


def _apply_provider_system_cache_policy(
    provider_name: str,
    system: list[dict] | str | None,
    cache: bool,
) -> list[dict] | str | None:
    """Apply provider-native cache hints to system instructions."""
    return _runtime_apply_provider_system_cache_policy(provider_name, system, cache)


def _build_reasoning_effort(thinking: str | None) -> tuple[str | None, int]:
    """Normalize reasoning effort and compute a conservative capped budget."""
    if not thinking or thinking == "none":
        return None, 16_384
    return thinking, 32_768


def _is_tool_use_assistant(msg: dict) -> bool:
    if msg.get("role") != MessageRole.ASSISTANT:
        return False
    content = msg.get("content", "")
    if isinstance(content, list):
        return any(
            (isinstance(b, dict) and b.get("type") == ContentBlockType.TOOL_USE)
            or (hasattr(b, "type") and b.type == ContentBlockType.TOOL_USE)
            for b in content
        )
    return False


def _is_tool_result_user(msg: dict) -> bool:
    if msg.get("role") != MessageRole.USER:
        return False
    content = msg.get("content", "")
    if isinstance(content, list):
        return any(
            (isinstance(b, dict) and b.get("type") == ContentBlockType.TOOL_RESULT)
            or (hasattr(b, "type") and b.type == ContentBlockType.TOOL_RESULT)
            for b in content
        )
    return False


def _trim_session_messages(messages: list[dict], session_id: str) -> list[dict]:
    """Trim old messages while preserving tool-pair boundaries."""
    from brain.systems.context.compaction import compact_session_messages

    result, report = compact_session_messages(
        messages,
        max_messages=_MAX_PERSISTED_MESSAGES,
        session_id=session_id,
        keep_early=2,
    )
    logger.info(
        "Session %s: compacted %d old messages (kept %d, strategy=%s)",
        session_id,
        report.omitted_count,
        len(result),
        report.strategy,
    )
    return result


def _semantic_compactor_from_metadata(metadata: dict):
    """Return an injected harness compactor, when direct callers provide one."""
    for key in ("semantic_compactor", "context_compactor", "context_compaction_summarizer"):
        candidate = metadata.get(key) if isinstance(metadata, dict) else None
        if callable(candidate):
            return candidate
    return None


def _thread_handoff_compactor_from_metadata(metadata: dict):
    """Return an injected handoff summarizer, when direct callers provide one."""
    for key in ("thread_handoff_compactor", "thread_handoff_summarizer", "handoff_summarizer"):
        candidate = metadata.get(key) if isinstance(metadata, dict) else None
        if callable(candidate):
            return candidate
    return None


def _message_excerpt_for_handoff_llm(message: dict, *, max_chars: int = 1_200) -> dict:
    """Return a bounded but faithful message excerpt for the handoff LLM."""
    role = str(message.get("role") or "message")
    raw = json.dumps(message.get("content"), sort_keys=True, default=str)
    if len(raw) > max_chars:
        raw = raw[:max_chars] + "\n... (content truncated for handoff summarizer)"
    return {"role": role, "content": raw}


def _handoff_llm_message_payload(messages: list[dict], *, max_messages: int = 80) -> dict:
    """Bound transcript input to keep the handoff summarizer cheap and stable."""
    if len(messages) <= max_messages:
        selected = list(enumerate(messages))
        skipped_middle = 0
    else:
        head_count = min(20, max_messages // 4)
        tail_count = max_messages - head_count
        selected = list(enumerate(messages[:head_count])) + list(
            enumerate(messages[-tail_count:], start=len(messages) - tail_count)
        )
        skipped_middle = len(messages) - len(selected)
    return {
        "message_count": len(messages),
        "skipped_middle_messages": skipped_middle,
        "messages": [
            {
                "index": index,
                **_message_excerpt_for_handoff_llm(message),
            }
            for index, message in selected
        ],
    }


def _parse_handoff_json(raw_output: str) -> dict | None:
    """Parse the first JSON object returned by the handoff LLM."""
    text = str(raw_output or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _llm_thread_handoff_compactor(
    *,
    provider,
    llm,
    model: str,
    provider_name: str,
    session_id: str,
):
    """Build the default model-backed durable handoff summarizer."""

    def compact(omitted_messages: list[dict], context: dict) -> dict:
        request_session_id = f"{session_id}:thread-handoff"
        schema = context.get("schema") if isinstance(context, dict) else {}
        prompt_payload = {
            "schema": schema,
            "phase": context.get("phase") if isinstance(context, dict) else None,
            "session_id": context.get("session_id") if isinstance(context, dict) else session_id,
            "recent_messages": _handoff_llm_message_payload(
                list(context.get("recent_messages") or []) if isinstance(context, dict) else [],
                max_messages=12,
            ),
            "messages_to_summarize": _handoff_llm_message_payload(omitted_messages),
        }
        system = [{
            "type": "text",
            "text": (
                "You write durable thread handoff summaries for long-running agent conversations. "
                "Summarize the provided transcript into compact structured JSON for the next run. "
                "Preserve exact user constraints, active objectives, decisions, files/objects touched, "
                "failed attempts, important tool results, open questions, verification status, and risks. "
                "For active_objective and recent_user_intent, prefer the latest unresolved user request "
                "from recent_messages or the tail of messages_to_summarize; do not default to the first "
                "question in the thread. "
                "Do not invent details. Mark uncertainty explicitly. Return JSON only."
            ),
        }]
        messages = [{
            "role": "user",
            "content": (
                "Create the next durable thread handoff checkpoint from this payload:\n"
                f"{json.dumps(prompt_payload, sort_keys=True, default=str)}"
            ),
        }]
        request = _build_api_request(
            model,
            messages,
            1_200,
            system,
            None,
            "low",
            llm.build_request_headers(session_id=request_session_id),
            provider_name,
            request_session_id,
            False,
            cache_tools=False,
            operation_type="memory_extraction",
        )
        response = provider.create(request)
        raw_output = _extract_text([{"role": "assistant", "content": _content_to_dicts(response.content)}]).strip()
        payload = _parse_handoff_json(raw_output)
        if payload is None:
            payload = {
                "active_objective": raw_output[:1_000],
                "risks_or_unknowns": ["Handoff LLM returned non-JSON output; raw text was retained as objective."],
            }
        metadata = dict(payload.get("metadata") or {})
        metadata.update({
            "summary_source": "llm_thread_handoff_compactor",
            "model": model,
            "provider": provider_name,
            "raw_output_excerpt": raw_output[:500],
        })
        payload["metadata"] = metadata
        return payload

    return compact


def _llm_context_checkpoint_compactor(
    *,
    provider,
    llm,
    model: str,
    provider_name: str,
    session_id: str,
):
    """Build the default model-backed in-run context checkpoint summarizer."""

    def compact(omitted_messages: list[dict], context: dict) -> dict:
        phase = context.get("phase") if isinstance(context, dict) else None
        request_session_id = f"{session_id}:context-checkpoint:{phase or 'runtime'}"
        schema = context.get("schema") if isinstance(context, dict) else {}
        prompt_payload = {
            "schema": schema,
            "phase": phase,
            "session_id": context.get("session_id") if isinstance(context, dict) else session_id,
            "recent_messages": _handoff_llm_message_payload(
                list(context.get("recent_messages") or []) if isinstance(context, dict) else [],
                max_messages=16,
            ),
            "messages_to_summarize": _handoff_llm_message_payload(omitted_messages),
        }
        system = [{
            "type": "text",
            "text": (
                "You write in-run context compaction checkpoints for a tool-using coding agent. "
                "The older transcript span will be replaced by your compact structured JSON while "
                "recent raw turns remain visible. Preserve exact user constraints, current objective, "
                "decisions, files/objects touched, important tool results, failed attempts, verification "
                "status, and risks. For active_objective and recent_user_intent, prefer the latest "
                "unresolved user request from recent_messages or the tail of messages_to_summarize; do "
                "not default to the first question in the thread. Do not invent details. Mark uncertainty "
                "explicitly. Return JSON only."
            ),
        }]
        messages = [{
            "role": "user",
            "content": (
                "Create a runtime context checkpoint from this payload:\n"
                f"{json.dumps(prompt_payload, sort_keys=True, default=str)}"
            ),
        }]
        request = _build_api_request(
            model,
            messages,
            1_200,
            system,
            None,
            "low",
            llm.build_request_headers(session_id=request_session_id),
            provider_name,
            request_session_id,
            False,
            cache_tools=False,
            operation_type="memory_extraction",
        )
        response = provider.create(request)
        raw_output = _extract_text([{"role": "assistant", "content": _content_to_dicts(response.content)}]).strip()
        payload = _parse_handoff_json(raw_output)
        if payload is None:
            payload = {
                "active_objective": raw_output[:1_000],
                "risks_or_unknowns": ["Context checkpoint LLM returned non-JSON output; raw text was retained as objective."],
            }
        metadata = dict(payload.get("metadata") or {})
        metadata.update({
            "summary_source": "llm_context_checkpoint_compactor",
            "model": model,
            "provider": provider_name,
            "raw_output_excerpt": raw_output[:500],
        })
        payload["metadata"] = metadata
        return payload

    return compact


def _thread_handoff_recent_message_limit(metadata: dict) -> int:
    configured = metadata.get("thread_handoff_recent_messages") if isinstance(metadata, dict) else None
    if configured is not None:
        try:
            return max(4, min(100, int(configured)))
        except (TypeError, ValueError):
            pass
    return max(4, min(100, _env_int("AGENT_THREAD_HANDOFF_RECENT_MESSAGES", 32)))


def _append_message_with_archive(
    messages: list[dict],
    message: dict,
    archive_messages: list[dict] | None = None,
) -> None:
    """Append to active context and, when present, to the raw session archive."""
    messages.append(copy.deepcopy(message))
    if archive_messages is not None:
        archive_messages.append(copy.deepcopy(message))


def _prepare_thread_startup_context(
    messages: list[dict],
    *,
    handoff: dict | None,
    session_id: str,
    recent_message_limit: int,
    semantic_compactor=None,
) -> tuple[list[dict], dict | None]:
    """Build the startup context from a durable handoff plus recent raw messages."""
    from brain.systems.context.thread_handoff import (
        build_thread_handoff,
        build_thread_handoff_context_messages,
    )

    effective_handoff = handoff
    if not effective_handoff and len(messages) > recent_message_limit:
        generated, fallback_error = build_thread_handoff(
            previous_handoff=None,
            messages_since=messages,
            total_message_count=len(messages),
            session_id=session_id,
            phase="startup_backfill_handoff",
            semantic_compactor=semantic_compactor,
        )
        effective_handoff = generated.to_payload()
        if fallback_error:
            logger.debug("Agent %s: startup handoff fallback used: %s", session_id, fallback_error)

    active_messages = build_thread_handoff_context_messages(
        messages,
        handoff=effective_handoff,
        max_recent_messages=recent_message_limit,
    )
    if effective_handoff and len(active_messages) < len(messages):
        logger.info(
            "Agent %s: loaded durable thread handoff plus %d/%d raw recent messages",
            session_id,
            max(0, len(active_messages) - 1),
            len(messages),
        )
    return active_messages, effective_handoff


def _update_thread_handoff_after_run(
    *,
    session_id: str,
    archive_messages: list[dict],
    previous_handoff: dict | None,
    semantic_compactor=None,
    run_id: int | None = None,
    user_id: str | None = None,
) -> dict | None:
    """Incrementally summarize the raw archive for the next persistent run."""
    from brain.systems.context.thread_handoff import ThreadHandoff, build_thread_handoff

    previous = ThreadHandoff.from_payload(previous_handoff)
    previous_count = min(previous.message_count if previous else 0, len(archive_messages))
    messages_since = archive_messages[previous_count:]
    if not messages_since and previous_handoff:
        return previous_handoff
    handoff, fallback_error = build_thread_handoff(
        previous_handoff=previous,
        messages_since=messages_since,
        total_message_count=len(archive_messages),
        session_id=session_id,
        semantic_compactor=semantic_compactor,
        run_id=run_id,
    )
    payload = handoff.to_payload()
    _save_session_handoff(session_id, payload, user_id=user_id)
    if fallback_error:
        logger.debug("Agent %s: post-run handoff fallback used: %s", session_id, fallback_error)
    logger.info(
        "Agent %s: updated durable thread handoff through %d raw messages (source=%s)",
        session_id,
        handoff.message_count,
        handoff.source,
    )
    return payload


def _record_context_compaction_event(
    *,
    run_id: int | None,
    session_id: str,
    model: str,
    provider_name: str | None,
    phase: str,
    budget: dict,
    report,
) -> None:
    """Emit best-effort audit metadata for long-context compaction."""
    payload = {
        "session_id": session_id,
        "model": model,
        "provider": provider_name,
        "phase": phase,
        "budget": budget,
        "report": report.to_payload() if hasattr(report, "to_payload") else {},
    }
    if not run_id:
        return
    try:
        from brain.systems.runs.event_log import record_run_event

        record_run_event(run_id, "run.context_compacted", payload)
    except Exception:
        logger.debug("Agent %s: failed to record context compaction event", session_id, exc_info=True)


def _maybe_compact_active_context(
    messages: list[dict],
    *,
    session_id: str,
    model: str,
    phase: str,
    system: list[dict] | str | None = None,
    tools: list[dict] | None = None,
    provider_name: str | None = None,
    reasoning_effort: str | None = None,
    max_output_tokens: int | None = None,
    run_id: int | None = None,
    semantic_compactor=None,
    force: bool = False,
    emergency: bool = False,
) -> tuple[list[dict], object | None]:
    """Compact active history when the estimated prompt crosses the runtime limit."""
    from brain.systems.context.budget import resolve_model_context_budget
    from brain.systems.context.compaction import (
        estimate_session_tokens,
    )
    from brain.systems.context.semantic_compaction import compact_session_messages_with_checkpoint

    budget = resolve_model_context_budget(
        model=model,
        provider=provider_name,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
        tools=tools,
    )
    estimated_tokens = estimate_session_tokens(messages, system=system, tools=tools)
    if not force and estimated_tokens < budget.auto_compact_threshold_tokens:
        return messages, None

    compacted, report = compact_session_messages_with_checkpoint(
        messages,
        token_limit=budget.auto_compact_threshold_tokens,
        target_tokens=budget.emergency_target_tokens if emergency else budget.target_tokens,
        session_id=session_id,
        phase=phase,
        system=system,
        tools=tools,
        max_messages=_MAX_PERSISTED_MESSAGES,
        min_messages=4,
        force=force,
        emergency=emergency,
        semantic_compactor=semantic_compactor,
    )
    final_tokens = report.provenance.get("final_estimated_tokens")
    if report.omitted_count <= 0:
        logger.warning(
            "Agent %s: %s context exceeded token limit (~%d >= %d) but no safe transcript "
            "messages were eligible for compaction",
            session_id,
            phase,
            estimated_tokens,
            budget.auto_compact_threshold_tokens,
        )
        return messages, None
    logger.info(
        "Agent %s: %s auto-compacted active context from ~%d to ~%s tokens "
        "(limit=%d, omitted=%d, kept=%d, strategy=%s)",
        session_id,
        phase,
        estimated_tokens,
        final_tokens if final_tokens is not None else "?",
        budget.auto_compact_threshold_tokens,
        report.omitted_count,
        len(compacted),
        report.strategy,
    )
    _record_context_compaction_event(
        run_id=run_id,
        session_id=session_id,
        model=model,
        provider_name=provider_name,
        phase=phase,
        budget=budget.to_payload(),
        report=report,
    )
    return compacted, report


def _mark_tools_cacheable(tools: list[dict]) -> list[dict]:
    """Add cache_control to the last tool so the full tool block gets cached."""
    return _runtime_mark_tools_cacheable(tools)


def _derive_openai_cache_key(
    session_id: str,
    system: list[dict] | str | None,
    tools: list[dict] | None,
    persist_session: bool,
    operation_type: str | None = None,
) -> str:
    """Build a stable OpenAI prompt cache key for similar repeated prefixes.

    OpenAI prompt caching benefits from a stable `prompt_cache_key`; we key it
    by coarse runtime role plus the static prompt/tool scaffold so repeated
    coordinator/worker/scout calls share cache routing across runs.
    """
    return _runtime_derive_openai_cache_key(
        session_id,
        system,
        tools,
        persist_session,
        operation_type=operation_type,
    )


def _derive_prompt_cache_key(
    session_id: str,
    system: list[dict] | str | None,
    tools: list[dict] | None,
    persist_session: bool,
    operation_type: str | None = None,
) -> str:
    """Build a stable provider-neutral prompt cache key."""
    return _runtime_derive_prompt_cache_key(
        session_id,
        system,
        tools,
        persist_session,
        operation_type=operation_type,
    )


def _get_openai_cache_retention(model: str) -> str | None:
    """Return a conservative retention hint for OpenAI models that support it."""
    return _runtime_get_openai_cache_retention(model)


def _get_extended_prompt_cache_retention(model: str) -> str | None:
    """Return an extended prompt-cache retention hint when supported."""
    return _runtime_get_extended_prompt_cache_retention(model)


def _build_api_request(
    model: str, messages: list, max_tokens: int, system: list | None,
    tools: list | None, reasoning_effort: str | None,
    extra_headers: dict | None, provider_name: str, session_id: str,
    persist_session: bool, cache_tools: bool = False,
    operation_type: str | None = None,
) -> LLMRequest:
    """Build a provider-neutral request for the runtime."""
    return _runtime_build_api_request(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        system=system,
        tools=tools,
        reasoning_effort=reasoning_effort,
        extra_headers=extra_headers,
        provider_name=provider_name,
        session_id=session_id,
        persist_session=persist_session,
        cache_tools=cache_tools,
        operation_type=operation_type,
    )


def _infer_provider_operation_type(
    *,
    session_id: str,
    tool_call_source: str,
    metadata: dict,
) -> str:
    return _runtime_infer_provider_operation_type(
        session_id=session_id,
        tool_call_source=tool_call_source,
        metadata=metadata,
    )


def _make_result(
    output: str, success: bool, session_id: str, tokens: _TokenAccumulator,
    start_time: float, tool_calls: list[str], error: str | None = None,
    worker_results: list | None = None,
) -> AgentResult:
    """Construct an AgentResult with common fields."""
    return _runtime_make_result(
        output,
        success,
        session_id,
        tokens,
        start_time,
        tool_calls,
        error=error,
        worker_results=worker_results,
    )


def _check_gate_violations(
    tool_name: str, block_id: str, gates: _GateState,
    tool_handlers: dict,
) -> dict | None:
    """Check if a tool call is blocked by gates. Returns error tool_result or None."""
    return _runtime_check_gate_violations(
        tool_name,
        block_id,
        gates,
        tool_handlers,
        gated_tool_names=_GATED_TOOL_NAMES,
    )


def _required_introspection_tool(message: str) -> tuple[str | None, str | None]:
    """Return the mandatory tool for certain introspection questions."""
    return resolve_required_introspection_tool(message)


def _response_has_text(response) -> bool:
    """Return True when the assistant response includes any non-empty text block."""
    return _runtime_response_has_text(response)


def _tool_is_available(tools: list[dict] | None, tool_name: str | None) -> bool:
    """Return True when the current invocation actually exposes the tool."""
    if not tool_name:
        return False
    for tool in tools or []:
        if isinstance(tool, dict) and tool.get("name") == tool_name:
            return True
    return False


# ── API Call ──────────────────────────────────────────────────

_API_RETRY_DELAYS = (2, 5, 10)


def _api_call_with_retry(
    provider, request: LLMRequest, llm, cancel_event, on_stream_activity, on_stream_delta,
    session_id: str, turn: int, tokens: _TokenAccumulator,
    start_time: float, tool_calls_made: list[str], _call_start: float,
):
    """Make API call with retry on 500s. Returns (response, extra_headers) or AgentResult on cancel."""
    return _runtime_api_call_with_retry(
        provider,
        request,
        llm,
        cancel_event,
        on_stream_activity,
        on_stream_delta,
        session_id=session_id,
        turn=turn,
        tokens=tokens,
        start_time=start_time,
        tool_calls_made=tool_calls_made,
        call_start=_call_start,
        retry_delays=_API_RETRY_DELAYS,
        streaming_call=_runtime_streaming_call,
        make_cancelled_result=_make_result,
        degrade_betas=_degrade_betas,
        is_cancelled_result=lambda response: isinstance(response, AgentResult),
    )


def _streaming_call(
    provider, request, cancel_event, on_stream_activity, on_stream_delta,
    session_id, tokens, start_time, tool_calls_made, _call_start,
):
    """Handle streaming API call with cancellation support. Returns response or AgentResult."""
    return _runtime_streaming_call(
        provider,
        request,
        cancel_event,
        on_stream_activity,
        on_stream_delta,
        session_id=session_id,
        tokens=tokens,
        start_time=start_time,
        tool_calls_made=tool_calls_made,
        call_start=_call_start,
        make_cancelled_result=_make_result,
    )


# ── Tool Execution ───────────────────────────────────────────


def _run_tool_awaitable(result):
    """Resolve sync or async tool handler outputs in the sync agent loop."""
    return _runtime_run_tool_awaitable(result)


def _invoke_tool_handler(handler: Callable, tool_input: dict, threadlocal_context: dict | None = None):
    """Execute a tool handler with optional propagated AgentRun context."""
    return _runtime_invoke_tool_handler(
        handler,
        tool_input,
        agent_context=_agent_context,
        threadlocal_context=threadlocal_context,
    )


def _resolve_tool_call(
    request: _PendingToolCall,
    *,
    threadlocal_context: dict | None = None,
) -> _ResolvedToolCall:
    """Execute one tool request and normalize success/error handling."""
    return _runtime_resolve_tool_call(
        request,
        agent_context=_agent_context,
        threadlocal_context=threadlocal_context,
    )


def _emit_resolved_tool_call(
    resolved: _ResolvedToolCall,
    tool_results: list[dict],
    on_tool_call,
    run_id,
    idea_id,
    tool_call_source: str,
) -> None:
    """Append tool_result content and record side effects in block order."""
    _runtime_emit_resolved_tool_call(
        resolved,
        tool_results,
        on_tool_call,
        run_id,
        idea_id,
        tool_call_source,
    )


def _execute_parallel_tool_batch(
    pending: list[_PendingToolCall],
    tool_results: list[dict],
    on_tool_call,
    run_id,
    idea_id,
    tool_call_source: str,
) -> None:
    """Run a batch of independent tool calls concurrently while preserving output order."""
    _runtime_execute_parallel_tool_batch(
        pending,
        tool_results,
        on_tool_call,
        run_id,
        idea_id,
        tool_call_source,
        agent_context=_agent_context,
        max_parallel_tool_calls=_MAX_PARALLEL_TOOL_CALLS,
    )


def _tool_supports_parallel_batch(tool_name: str) -> bool:
    """Return True when a tool can be safely co-scheduled with peer calls."""
    return tool_name in _PARALLEL_SAFE_TOOL_NAMES


def _execute_tool_calls(
    response, tool_handlers: dict, tool_calls_made: list[str],
    gates: _GateState,
    on_tool_call, run_id, idea_id, tool_call_source: str,
    *,
    max_parallel_tool_calls: int = _MAX_PARALLEL_TOOL_CALLS,
) -> list[dict]:
    """Execute all tool calls from a response. Returns tool_results list."""
    return _runtime_execute_tool_calls(
        response,
        tool_handlers,
        tool_calls_made,
        gates,
        on_tool_call,
        run_id,
        idea_id,
        tool_call_source,
        agent_context=_agent_context,
        brain_tool_names=_BRAIN_TOOL_NAMES,
        gated_tool_names=_GATED_TOOL_NAMES,
        research_tool_names=_RESEARCH_TOOL_NAMES,
        research_budget=_RESEARCH_BUDGET,
        parallel_safe_tool_names=_PARALLEL_SAFE_TOOL_NAMES,
        max_parallel_tool_calls=max(1, int(max_parallel_tool_calls)),
        check_gate_violations=_runtime_check_gate_violations,
    )


def _append_live_guidance(
    messages: list[dict],
    live_guidance_loader: Callable[[], list[str]] | None,
    *,
    session_id: str,
    on_stream_activity: Callable[[str], None] | None = None,
) -> int:
    """Append user guidance that arrived while the agent was already working."""
    if live_guidance_loader is None:
        return 0
    try:
        guidance_items = live_guidance_loader() or []
    except Exception:
        logger.debug("Agent %s: live guidance loader failed", session_id, exc_info=True)
        return 0
    clean_items = [str(item).strip() for item in guidance_items if str(item or "").strip()]
    if not clean_items:
        return 0
    content = (
        "[Live user guidance received while you were working]\n"
        "Use this to adjust the current run. Preserve useful progress; do not restart unless the user explicitly asks.\n\n"
        + "\n\n".join(clean_items)
    )
    messages.append({"role": "user", "content": content})
    if on_stream_activity:
        try:
            on_stream_activity("Received live user guidance")
        except Exception:
            pass
    logger.info("Agent %s: appended %d live guidance item(s)", session_id, len(clean_items))
    return len(clean_items)


# ── The Agent Loop ───────────────────────────────────────────

def run_agent(
    message: str,
    system_prompt: str = "",
    session_id: str | None = None,
    model: str | None = None,
    thinking: str | None = "medium",
    tools: list[dict] | None = None,
    tool_handlers: dict | None = None,
    max_turns: int = 200,
    timeout_sec: int | None = None,
    cache_system_prompt: bool = True,
    persist_session: bool = True,
    on_tool_call: Callable[[str, dict, str], None] | None = None,
    workspace_root: str | None = None,
    brain_context_preloaded: bool = False,
    run_id: int | None = None,
    idea_id: str | None = None,
    tool_call_source: str = "runner",
    cancel_event: "threading.Event | None" = None,
    on_stream_activity: "Callable[[str], None] | None" = None,
    on_stream_delta: "Callable[[str], None] | None" = None,
    live_guidance_loader: "Callable[[], list[str]] | None" = None,
    user_id: str | None = None,
    skip_harvest: bool = False,
    metadata: dict | None = None,
) -> AgentResult:
    """Run an agent loop with tool use.

    Args:
        message: The user message to send
        system_prompt: System prompt for the agent
        session_id: Session ID for persistence
        model: Model name (e.g., 'openai/gpt-5.4')
        thinking: Thinking level ('none', 'low', 'medium', 'high')
        tools: Tool definitions (default: BRAIN_TOOLS)
        tool_handlers: Map of tool name → handler function
        max_turns: Maximum tool-use turns before stopping
        timeout_sec: Accepted no-op arg; runtime timeout enforcement is owned by callers
        cache_system_prompt: Whether to cache the system prompt
        persist_session: Whether to save conversation to DB
        on_tool_call: Optional callback(tool_name, args, result_text)
        workspace_root: Override workspace root for file/exec tools
        brain_context_preloaded: If True, brain gate starts satisfied
    """
    import threading

    start_time = time.time()
    session_id = session_id or f"agent-{uuid.uuid4().hex[:12]}"
    metadata = dict(metadata or {})
    max_parallel_tool_calls = max(1, _metadata_int(metadata, "max_parallel_tool_calls", _MAX_PARALLEL_TOOL_CALLS))
    semantic_compactor = _semantic_compactor_from_metadata(metadata)
    thread_handoff_compactor = _thread_handoff_compactor_from_metadata(metadata)
    operation_type = _infer_provider_operation_type(
        session_id=session_id,
        tool_call_source=tool_call_source,
        metadata=metadata,
    )

    if tools is None:
        tools = BRAIN_TOOLS
    if tool_handlers is None:
        tool_handlers = _get_tool_handlers(workspace_root=workspace_root)

    state = AgentLoopState(
        gates=_GateState(
            brain=brain_context_preloaded,
            skills=brain_context_preloaded,
        ),
        operation_type=operation_type,
        metadata=metadata,
    )
    metadata_required_tool, metadata_required_msg = resolve_required_introspection_tool(
        explicit_tool=metadata.get("required_introspection_tool") if isinstance(metadata, dict) else None,
    )
    if metadata_required_tool:
        required_introspection_tool, required_introspection_msg = metadata_required_tool, metadata_required_msg
    else:
        required_introspection_tool, required_introspection_msg = _required_introspection_tool(message)

    _previous_execution_metadata = getattr(_agent_context, "execution_metadata", None)
    _previous_execution_artifacts = getattr(_agent_context, "execution_artifacts", None)
    _previous_final_reply_review = getattr(_agent_context, "final_reply_review", None)
    _session_sentinel = object()
    _previous_agent_session_id = getattr(_agent_context, "session_id", _session_sentinel)
    context_attrs = {
        "session_id": session_id,
        "final_reply_review": None,
    }
    if workspace_root:
        context_attrs["workspace_root"] = workspace_root
    context_idea_id = idea_id
    if not context_idea_id:
        target_ref = metadata.get("target_ref")
        if isinstance(target_ref, dict):
            candidate = target_ref.get("idea_id") or target_ref.get("thread_id")
            if isinstance(candidate, str) and candidate.strip():
                context_idea_id = candidate.strip()
    if context_idea_id:
        context_attrs["idea_id"] = context_idea_id
    if user_id:
        context_attrs["user_id"] = user_id
    if metadata.get("org_id"):
        context_attrs["org_id"] = metadata.get("org_id")
    chat_trigger = metadata.get("chat_trigger")
    if not isinstance(chat_trigger, dict) and isinstance(metadata.get("target_ref"), dict):
        chat_trigger = metadata["target_ref"].get("chat_trigger")
    if isinstance(chat_trigger, dict):
        context_attrs["chat_trigger"] = dict(chat_trigger)
    if run_id is not None:
        context_attrs["run_id"] = run_id
    _agent_agent_context = bind_agent_context(context_attrs)
    _agent_agent_context.__enter__()

    try:
        _agent_context.session_id = session_id
        _agent_context.final_reply_review = None
        execution_metadata = metadata.get("execution_provenance") if isinstance(metadata, dict) else None
        if execution_metadata:
            _agent_context.execution_metadata = execution_metadata
            if getattr(_agent_context, "execution_artifacts", None) is None:
                _agent_context.execution_artifacts = []

        if not model:
            model = get_default_model(
                include_provider_prefix=True,
                user_id=user_id,
                org_id=metadata.get("org_id"),
            )
        else:
            tier = normalize_model_tier(model, default=None)
            if tier in MODEL_TIERS:
                model = get_model_for_tier(
                    tier,
                    include_provider_prefix=True,
                    user_id=user_id,
                    org_id=metadata.get("org_id"),
                )
        model = _normalize_model(model)

        # Resolve LLM client
        llm, state.provider, _runtime_extra_headers = _init_llm(
            user_id,
            session_id,
            model,
            org_id=metadata.get("org_id"),
        )
        state.provider_name = llm.provider
        if semantic_compactor is None:
            semantic_compactor = _llm_context_checkpoint_compactor(
                provider=state.provider,
                llm=llm,
                model=model,
                provider_name=state.provider_name,
                session_id=session_id,
            )
        if thread_handoff_compactor is None:
            thread_handoff_compactor = _llm_thread_handoff_compactor(
                provider=state.provider,
                llm=llm,
                model=model,
                provider_name=state.provider_name,
                session_id=session_id,
            )

        # Load existing raw session archive, then use durable handoff + recent messages as active context.
        loaded_messages, stored_system = _load_session(session_id) if persist_session else ([], None)
        if stored_system and not system_prompt:
            system_prompt = stored_system
        raw_archive_messages = copy.deepcopy(loaded_messages) if persist_session else None
        thread_handoff = _load_session_handoff(session_id) if persist_session else None

        # Build system + reasoning config
        system = _build_system_blocks(llm, system_prompt, cache_system_prompt)
        system = _apply_provider_system_cache_policy(state.provider_name, system, cache_system_prompt)
        reasoning_effort, max_tokens = _build_reasoning_effort(thinking)

        if persist_session:
            state.messages, thread_handoff = _prepare_thread_startup_context(
                loaded_messages,
                handoff=thread_handoff,
                session_id=session_id,
                recent_message_limit=_thread_handoff_recent_message_limit(metadata),
                semantic_compactor=thread_handoff_compactor,
            )
        else:
            state.messages = loaded_messages

        _append_message_with_archive(
            state.messages,
            {"role": "user", "content": _initial_user_content(message, metadata)},
            raw_archive_messages,
        )

        # Agent loop
        turn = 0

        for turn in range(max_turns):
            if cancel_event and cancel_event.is_set():
                return _make_result(
                    "",
                    False,
                    session_id,
                    state.tokens,
                    start_time,
                    state.tool_calls_made,
                    error="Cancelled by runner",
                )

            before_guidance_len = len(state.messages)
            guidance_count = _append_live_guidance(
                state.messages,
                live_guidance_loader,
                session_id=session_id,
                on_stream_activity=on_stream_activity,
            )
            if guidance_count and raw_archive_messages is not None and len(state.messages) > before_guidance_len:
                raw_archive_messages.append(copy.deepcopy(state.messages[-1]))
            state.messages = _sanitize_tool_pairs(state.messages, session_id)
            state.messages, _ = _maybe_compact_active_context(
                state.messages,
                session_id=session_id,
                model=model,
                phase="pre_sampling",
                system=system,
                tools=tools,
                provider_name=state.provider_name,
                reasoning_effort=reasoning_effort,
                max_output_tokens=max_tokens,
                run_id=run_id,
                semantic_compactor=semantic_compactor,
            )
            if state.provider_name == "anthropic" and cache_system_prompt and len(state.messages) >= 2:
                _clear_message_cache_breakpoints(state.messages)
                _set_cache_breakpoint(state.messages[-1])

            request = _build_api_request(
                model, state.messages, max_tokens, system, tools,
                reasoning_effort, _runtime_extra_headers,
                state.provider_name, session_id, persist_session,
                cache_tools=cache_system_prompt,
                operation_type=state.operation_type,
            )

            # DEBUG: verify cache_control is in the API kwargs
            if _AGENT_CACHE_DEBUG and turn == 0:
                _tools_in_kwargs = request.tools or []
                _sys_in_kwargs = request.system or []
                _last_tool_cc = _tools_in_kwargs[-1].get("cache_control") if _tools_in_kwargs else None
                _last_sys_cc = _sys_in_kwargs[-1].get("cache_control") if _sys_in_kwargs else None
                logger.debug(
                    "CACHE-DEBUG %s: cache_system_prompt=%s, tools=%d, last_tool_cache_control=%s, "
                    "system_blocks=%d, last_sys_cache_control=%s, cache_key=%s, cache_retention=%s, extra_headers=%s",
                    session_id, cache_system_prompt, len(_tools_in_kwargs),
                    _last_tool_cc, len(_sys_in_kwargs), _last_sys_cc,
                    request.cache_key, request.cache_retention,
                    list((request.extra_headers or {}).keys()),
                )

            _call_start = time.time()
            logger.info(
                "Agent %s turn %d: model=%s, msgs=%d, tools=%d",
                session_id, turn, model, len(state.messages), len(tools) if tools else 0,
            )

            # API call with retry on transient 500s, plus one emergency context compaction retry.
            overflow_retry_used = False
            while True:
                try:
                    response = _api_call_with_retry(
                        state.provider, request, llm, cancel_event, on_stream_activity, on_stream_delta,
                        session_id, turn, state.tokens, start_time, state.tool_calls_made, _call_start,
                    )
                    break
                except Exception as exc:
                    if overflow_retry_used or not is_context_overflow_error(exc, provider_name=state.provider_name):
                        raise
                    overflow_retry_used = True
                    overflow_payload = context_overflow_payload(exc, provider_name=state.provider_name)
                    logger.warning(
                        "Agent %s turn %d: provider rejected context; compacting and retrying once (%s)",
                        session_id,
                        turn,
                        overflow_payload.get("message", "")[:240],
                    )
                    _record_api_call(
                        session_id=session_id, run_id=run_id, turn=turn,
                        model=model,
                        context_messages=len(state.messages),
                        system_prompt_chars=len(json.dumps(system)) if system else 0,
                        status="context_overflow",
                        error=overflow_payload.get("message"),
                        latency_ms=int((time.time() - _call_start) * 1000),
                    )
                    state.messages, recovery_report = _maybe_compact_active_context(
                        state.messages,
                        session_id=session_id,
                        model=model,
                        phase="context_overflow_retry",
                        system=system,
                        tools=tools,
                        provider_name=state.provider_name,
                        reasoning_effort=reasoning_effort,
                        max_output_tokens=max_tokens,
                        run_id=run_id,
                        semantic_compactor=semantic_compactor,
                        force=True,
                        emergency=True,
                    )
                    if recovery_report is None:
                        logger.warning(
                            "Agent %s turn %d: context overflow recovery had no eligible messages to compact",
                            session_id,
                            turn,
                        )
                        raise
                    if on_stream_activity:
                        try:
                            on_stream_activity("Compacted context after provider limit; retrying")
                        except Exception:
                            pass
                    if state.provider_name == "anthropic" and cache_system_prompt and len(state.messages) >= 2:
                        _clear_message_cache_breakpoints(state.messages)
                        _set_cache_breakpoint(state.messages[-1])
                    request = _build_api_request(
                        model, state.messages, max_tokens, system, tools,
                        reasoning_effort, _runtime_extra_headers,
                        state.provider_name, session_id, persist_session,
                        cache_tools=cache_system_prompt,
                        operation_type=state.operation_type,
                    )
                    _call_start = time.time()
            if isinstance(response, AgentResult):
                return response  # Cancellation during streaming

            _call_ms = int((time.time() - _call_start) * 1000)
            state.tokens.add_turn(response.usage)

            # DEBUG: log cache results from API response
            _cr = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            _cw = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            if _AGENT_CACHE_DEBUG and (turn <= 2 or _cr > 0 or _cw > 0):
                logger.debug(
                    "CACHE-RESULT %s turn %d: input=%d, cache_read=%d, cache_write=%d, output=%d",
                    session_id, turn,
                    getattr(response.usage, "input_tokens", 0),
                    _cr, _cw,
                    getattr(response.usage, "output_tokens", 0),
                )

            # Per-call telemetry (fire-and-forget)
            _record_api_call(
                session_id=session_id, run_id=run_id, turn=turn,
                model=model,
                tokens_input=getattr(response.usage, "input_tokens", 0),
                tokens_output=getattr(response.usage, "output_tokens", 0),
                cache_read=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                cache_write=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                context_messages=len(state.messages),
                system_prompt_chars=len(json.dumps(system)) if system else 0,
                status="tool_use" if response.stop_reason == StopReason.TOOL_USE else "success",
                stop_reason=str(response.stop_reason), latency_ms=_call_ms,
            )

            content_dicts = _content_to_dicts(response.content)
            if (
                response.stop_reason == StopReason.END_TURN
                and getattr(response.usage, "output_tokens", 0) > 0
                and not content_dicts
            ):
                logger.warning(
                    "Agent %s: provider returned end_turn with output tokens but no parsed assistant content "
                    "(run_id=%s, provider=%s, model=%s, output_tokens=%s, response_content_types=%s)",
                    session_id,
                    run_id,
                    getattr(llm, "provider", "unknown"),
                    model,
                    getattr(response.usage, "output_tokens", 0),
                    [getattr(block, "type", None) for block in getattr(response, "content", []) or []],
                )
            _append_message_with_archive(
                state.messages,
                {"role": "assistant", "content": content_dicts},
                raw_archive_messages,
            )

            if response.stop_reason == StopReason.END_TURN:
                before_guidance_len = len(state.messages)
                guidance_count = _append_live_guidance(
                    state.messages,
                    live_guidance_loader,
                    session_id=session_id,
                    on_stream_activity=on_stream_activity,
                )
                if guidance_count and raw_archive_messages is not None and len(state.messages) > before_guidance_len:
                    raw_archive_messages.append(copy.deepcopy(state.messages[-1]))
                if guidance_count:
                    continue
                output = _extract_text(state.messages)
                if (
                    required_introspection_tool
                    and _tool_is_available(tools, required_introspection_tool)
                    and required_introspection_tool in tool_handlers
                    and required_introspection_tool not in state.tool_calls_made
                ):
                    _append_message_with_archive(
                        state.messages,
                        {
                            "role": "user",
                            "content": f"[System: {required_introspection_msg}]",
                        },
                        raw_archive_messages,
                    )
                    continue
                break

            if response.stop_reason == StopReason.TOOL_USE:
                # Stuck detection
                _turn_fingerprints = [
                    f"{b.name}:{json.dumps(b.input, sort_keys=True)}"
                    for b in response.content
                    if hasattr(b, "type") and b.type == ContentBlockType.TOOL_USE
                ]
                state.recent_calls.extend(_turn_fingerprints)

                if _detect_stuck_loop(state.recent_calls, session_id, state.messages):
                    break

                # Execute tool calls
                tool_results = _execute_tool_calls(
                    response, tool_handlers, state.tool_calls_made, state.gates,
                    on_tool_call, run_id, idea_id,
                    tool_call_source,
                    max_parallel_tool_calls=max_parallel_tool_calls,
                )

                # Inject system nudges
                _inject_nudges(tool_results, state.recent_calls, response, session_id, agent_context=_agent_context)

                _append_message_with_archive(
                    state.messages,
                    {"role": "user", "content": tool_results},
                    raw_archive_messages,
                )
                state.messages, _ = _maybe_compact_active_context(
                    state.messages,
                    session_id=session_id,
                    model=model,
                    phase="mid_turn",
                    system=system,
                    tools=tools,
                    provider_name=state.provider_name,
                    reasoning_effort=reasoning_effort,
                    max_output_tokens=max_tokens,
                    run_id=run_id,
                    semantic_compactor=semantic_compactor,
                )
            else:
                logger.warning("Unknown stop_reason: %s", response.stop_reason)
                break

        # Post-loop: harvest, auto-encode, save, return
        output = _extract_text(state.messages)
        raw_persist_source = raw_archive_messages if raw_archive_messages is not None else state.messages
        persistable_messages = _messages_without_inline_attachment_binary(
            _sanitize_tool_pairs(copy.deepcopy(raw_persist_source), session_id)
        )
        _runtime_apply_agent_session_side_effects(
            session_id=session_id,
            messages=persistable_messages,
            output=output,
            system_prompt=system_prompt,
            tokens=state.tokens,
            tool_calls_made=state.tool_calls_made,
            user_id=user_id,
            metadata=state.metadata,
            agent_context=_agent_context,
            idea_id=idea_id,
            run_id=run_id,
            skip_harvest=skip_harvest,
            persist_session=persist_session,
            memory_org_for_user=_memory_org_for_user,
            auto_encode_if_needed=_auto_encode_if_needed,
            harvest_session=_harvest_session,
            save_session=_save_session,
        )
        if persist_session and raw_archive_messages is not None:
            _update_thread_handoff_after_run(
                session_id=session_id,
                archive_messages=persistable_messages,
                previous_handoff=thread_handoff,
                semantic_compactor=thread_handoff_compactor,
                run_id=run_id,
                user_id=None,
            )

        logger.info(
            "Agent %s: completed in %ds (turns=%d, input=%d, output=%d, tools=%d)",
            session_id, int(time.time() - start_time), turn + 1,
            state.tokens.input, state.tokens.output, len(state.tool_calls_made),
        )
        if not output.strip():
            logger.warning(
                "Agent %s: completed successfully but final extracted output is empty "
                "(run_id=%s, provider=%s, model=%s, turns=%d, tools=%d, message_count=%d)",
                session_id,
                run_id,
                getattr(llm, "provider", "unknown"),
                model,
                turn + 1,
                len(state.tool_calls_made),
                len(state.messages),
            )
        return _make_result(output, True, session_id, state.tokens, start_time, state.tool_calls_made)

    except Exception as e:
        if state.provider and state.provider.is_api_error(e):
            _resp = getattr(e, 'response', None)
            _body = getattr(e, 'body', None) or (getattr(_resp, 'text', '') if _resp else '')
            _hdrs = dict(getattr(_resp, 'headers', {}) or {}) if _resp else {}
            logger.error(
                "Agent %s: API error (status=%s): %s | body=%s | request_id=%s",
                session_id, getattr(e, 'status_code', '?'), e,
                str(_body)[:500], _hdrs.get('x-request-id', 'unknown'),
            )
            _record_api_call(
                session_id=session_id, run_id=run_id, turn=0,
                model=model, status="error", error=f"{e} | body={str(_body)[:200]}",
            )
            return _make_result(
                "",
                False,
                session_id,
                state.tokens,
                start_time,
                state.tool_calls_made,
                error=f"API error: {e}",
            )
        logger.error("Agent %s: error: %s", session_id, e)
        return _make_result("", False, session_id, state.tokens, start_time, state.tool_calls_made, error=str(e))

    finally:
        try:
            if _previous_execution_metadata is None:
                if hasattr(_agent_context, "execution_metadata"):
                    delattr(_agent_context, "execution_metadata")
            else:
                _agent_context.execution_metadata = _previous_execution_metadata
            if _previous_agent_session_id is _session_sentinel:
                if hasattr(_agent_context, "session_id"):
                    delattr(_agent_context, "session_id")
            else:
                _agent_context.session_id = _previous_agent_session_id
            if _previous_execution_artifacts is None:
                if hasattr(_agent_context, "execution_artifacts") and not getattr(getattr(_agent_context, "run", None), "run_id", None):
                    delattr(_agent_context, "execution_artifacts")
            else:
                _agent_context.execution_artifacts = _previous_execution_artifacts
            if _previous_final_reply_review is None:
                if hasattr(_agent_context, "final_reply_review"):
                    delattr(_agent_context, "final_reply_review")
            else:
                _agent_context.final_reply_review = _previous_final_reply_review
        finally:
            _agent_agent_context.__exit__(None, None, None)



# ── Convenience Functions ────────────────────────────────────

def call_llm(prompt: str, model: str | None = None, thinking: str = "medium") -> dict | None:
    """Simple LLM call for nightly pipeline JSON tasks.

    Returns parsed JSON from the response, or None on failure.
    Used by: nightly_reflect.py, nightly_dream.py, curiosity.py, brain_export.py
    """
    from brain.systems.runs.invocation import build_direct_agent_invocation, invoke_direct_agent

    result = invoke_direct_agent(build_direct_agent_invocation(
        message=prompt,
        model=model,
        thinking=thinking,
        tools=[],
        persist_session=False,
        max_turns=1,
        tool_call_source="call_llm",
    ))

    if not result.success or not result.output:
        return None

    # Try to parse JSON from output
    text = result.output.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON in the text
    json_start = text.find("{")
    json_end = text.rfind("}") + 1
    if json_start >= 0 and json_end > json_start:
        try:
            return json.loads(text[json_start:json_end])
        except json.JSONDecodeError:
            pass

    return None
