"""OpenAI Responses transport formatting and parsing."""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from brain.platform.effort import render_reasoning_effort
from brain.platform.integrations.openai_cache import normalize_openai_request_kwargs
from brain.platform.integrations.transports.base import (
    ContentBlock,
    ContentBlockType,
    LLMRequest,
    LLMResponse,
    MessageRole,
    Provider,
    StopReason,
    TextContentBlock,
    ToolUseContentBlock,
    Usage,
    validate_provider_messages,
    validate_system_blocks,
    validate_tool_definitions,
)
from brain.platform.model_catalog import model_accepts_effort, normalize_model_effort

logger = logging.getLogger("brain.platform.integrations.providers")


def _anthropic_tools_to_openai(tools: list[dict] | None) -> list[dict] | None:
    """Convert Anthropic tool format to OpenAI Responses tool format."""
    validate_tool_definitions(tools)
    if not tools:
        return None
    openai_tools = []
    for tool in tools:
        openai_tools.append({
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {}),
        })
    return openai_tools


def _block_get(block: Any, key: str, default: Any = None) -> Any:
    """Read from dicts or SDK objects uniformly."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _coerce_text(value: Any) -> str:
    """Normalize SDK/dict content into plain text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def _append_text_message(items: list[dict], role: str, text_parts: list[str]) -> None:
    text = "\n".join(part for part in text_parts if part).strip()
    if text:
        items.append({"role": role, "content": text})
    text_parts.clear()


def _openai_image_part(block: ContentBlock) -> dict[str, Any] | None:
    source = block.source if isinstance(block.source, dict) else {}
    image_url = source.get("url")
    if isinstance(image_url, str) and image_url.strip():
        return {"type": "input_image", "image_url": image_url.strip()}
    if source.get("type") == "base64":
        data = source.get("data")
        media_type = source.get("media_type") or source.get("mime_type") or "image/png"
        if isinstance(data, str) and data.strip():
            return {
                "type": "input_image",
                "image_url": f"data:{media_type};base64,{data.strip()}",
            }
    return None


def _openai_tool_result_content_parts(content: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(content, list):
        return _coerce_text(content), []

    text_parts: list[str] = []
    image_parts: list[dict[str, Any]] = []
    for item in content:
        try:
            block = ContentBlock.from_legacy(item, strict=False)
        except Exception:
            text_parts.append(_coerce_text(item))
            continue
        if block.type == ContentBlockType.TEXT:
            if block.text:
                text_parts.append(block.text)
            continue
        if block.type == ContentBlockType.IMAGE:
            part = _openai_image_part(block)
            if part:
                image_parts.append(part)
            continue
        text_parts.append(_coerce_text(block.model_dump(exclude_none=True)))
    return "\n".join(part for part in text_parts if part).strip(), image_parts


def _append_user_content_message(
    items: list[dict],
    text_parts: list[str],
    image_parts: list[dict[str, Any]],
) -> None:
    text = "\n".join(part for part in text_parts if part).strip()
    if image_parts:
        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "input_text", "text": text})
        content.extend(image_parts)
        items.append({"role": MessageRole.USER.value, "content": content})
    elif text:
        items.append({"role": MessageRole.USER.value, "content": text})
    text_parts.clear()
    image_parts.clear()


def _anthropic_messages_to_openai_input(messages: list[dict]) -> list[dict]:
    """Convert Anthropic-style history into Responses API input items."""
    items: list[dict] = []
    for msg in validate_provider_messages(messages, provider=Provider.OPENAI):
        role = str(msg.role)
        content = msg.content

        if isinstance(content, str):
            items.append({"role": role, "content": content})
            continue

        text_parts: list[str] = []
        image_parts: list[dict[str, Any]] = []
        for block in content:
            block_type = block.type

            if block_type == ContentBlockType.TEXT:
                text = block.text
                if text:
                    text_parts.append(text)
                continue

            if role == MessageRole.USER and block_type == ContentBlockType.IMAGE:
                part = _openai_image_part(block)
                if part:
                    image_parts.append(part)
                continue

            if block_type == ContentBlockType.THINKING:
                continue

            if role == MessageRole.ASSISTANT and block_type == ContentBlockType.TOOL_USE:
                _append_text_message(items, MessageRole.ASSISTANT.value, text_parts)
                items.append({
                    "type": "function_call",
                    "call_id": block.id or f"call_{uuid.uuid4().hex[:24]}",
                    "name": block.name or "",
                    "arguments": json.dumps(block.input or {}),
                })
                continue

            if role == MessageRole.USER and block_type == ContentBlockType.TOOL_RESULT:
                _append_user_content_message(items, text_parts, image_parts)
                output, result_image_parts = _openai_tool_result_content_parts(block.content)
                items.append({
                    "type": "function_call_output",
                    "call_id": block.tool_use_id or "",
                    "output": output,
                })
                if result_image_parts:
                    _append_user_content_message(items, [], result_image_parts)
                continue

        if role == MessageRole.USER:
            _append_user_content_message(items, text_parts, image_parts)
        else:
            _append_text_message(items, role, text_parts)

    return items


def _system_blocks_to_instructions(system: list[dict] | str | None) -> str | None:
    """Collapse Anthropic system blocks into Responses instructions text."""
    if not system:
        return None
    if isinstance(system, str):
        return system
    typed_blocks = validate_system_blocks(system)
    text_parts = []
    for block in typed_blocks or []:
        if isinstance(block, ContentBlock) and block.type == ContentBlockType.TEXT and block.text:
            text_parts.append(block.text)
    text = "\n\n".join(text_parts).strip()
    return text or None


def _usage_from_openai(response: Any) -> Usage:
    """Extract usage from a Responses API object."""
    usage = _block_get(response, "usage", None)
    input_tokens = _block_get(usage, "input_tokens", 0) or _block_get(usage, "prompt_tokens", 0) or 0
    output_tokens = _block_get(usage, "output_tokens", 0) or _block_get(usage, "completion_tokens", 0) or 0
    input_details = _block_get(usage, "input_tokens_details", {}) or {}
    cache_read_tokens = _block_get(input_details, "cached_tokens", 0) or 0
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_tokens,
    )


def _summarize_openai_output_shape(response: Any) -> list[dict[str, Any]]:
    """Return a compact structural summary of the Responses API output."""
    summary: list[dict[str, Any]] = []
    for item in _block_get(response, "output", []) or []:
        entry: dict[str, Any] = {"type": _block_get(item, "type")}
        role = _block_get(item, "role")
        if role:
            entry["role"] = role
        content = _block_get(item, "content", []) or []
        if content:
            entry["content_types"] = [_block_get(part, "type") for part in content]
        if _block_get(item, "name"):
            entry["name"] = _block_get(item, "name")
        summary.append(entry)
    return summary


def _truncate_for_log(value: Any, limit: int = 1500) -> str:
    """Serialize a compact debug excerpt that is safe to emit to logs."""
    try:
        rendered = json.dumps(value, ensure_ascii=True, default=str)
    except Exception:
        rendered = str(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit] + "...<truncated>"


def _openai_output_excerpt(response: Any) -> str:
    """Return a compact excerpt of output items for parser debugging."""
    excerpt: list[dict[str, Any]] = []
    for item in _block_get(response, "output", []) or []:
        entry: dict[str, Any] = {
            "type": _block_get(item, "type"),
            "role": _block_get(item, "role"),
            "name": _block_get(item, "name"),
        }
        content_excerpt: list[dict[str, Any]] = []
        for part in _block_get(item, "content", []) or []:
            content_excerpt.append({
                "type": _block_get(part, "type"),
                "text": (_block_get(part, "text", "") or "")[:200],
            })
        if content_excerpt:
            entry["content"] = content_excerpt
        arguments = _block_get(item, "arguments")
        if arguments:
            entry["arguments"] = str(arguments)[:200]
        excerpt.append(entry)
    return _truncate_for_log(excerpt)


def _openai_response_debug_dump(response: Any) -> str:
    """Best-effort serialized dump of the raw OpenAI response for debugging."""
    raw: Any = None
    if isinstance(response, dict):
        raw = response
    else:
        for attr in ("model_dump", "to_dict", "dict"):
            fn = getattr(response, attr, None)
            if callable(fn):
                try:
                    candidate = fn()
                    if isinstance(candidate, dict):
                        raw = candidate
                        break
                except Exception:
                    pass

    if raw is None:
        raw = {}
        for key in (
            "id",
            "object",
            "model",
            "status",
            "created_at",
            "output",
            "output_text",
            "text",
            "content",
            "incomplete_details",
            "usage",
            "parallel_tool_calls",
        ):
            value = getattr(response, key, None)
            if value is not None:
                raw[key] = value

    if isinstance(raw, dict):
        focused = {
            "id": raw.get("id"),
            "object": raw.get("object"),
            "model": raw.get("model"),
            "status": raw.get("status"),
            "created_at": raw.get("created_at"),
            "completed_at": raw.get("completed_at"),
            "output_text": raw.get("output_text"),
            "output": raw.get("output"),
            "text": raw.get("text"),
            "content": raw.get("content"),
            "incomplete_details": raw.get("incomplete_details"),
            "usage": raw.get("usage"),
            "parallel_tool_calls": raw.get("parallel_tool_calls"),
            "top_level_keys": sorted(raw.keys()),
        }
        return _truncate_for_log(focused, limit=12000)

    return _truncate_for_log(raw, limit=12000)


def _openai_request_debug_summary(kwargs: dict[str, Any]) -> str:
    """Compact request summary for debugging empty Responses API outputs."""
    input_items = kwargs.get("input", []) or []
    summarized_input: list[dict[str, Any]] = []
    for item in input_items[:4]:
        entry = {
            "role": _block_get(item, "role"),
            "type": _block_get(item, "type"),
            "content": _truncate_for_log(_block_get(item, "content"), limit=300),
        }
        if _block_get(item, "name"):
            entry["name"] = _block_get(item, "name")
        summarized_input.append(entry)

    tools = kwargs.get("tools", []) or []
    summary = {
        "model": kwargs.get("model"),
        "max_output_tokens": kwargs.get("max_output_tokens"),
        "store": kwargs.get("store"),
        "has_instructions": bool(kwargs.get("instructions")),
        "instructions_excerpt": _truncate_for_log(kwargs.get("instructions"), limit=400) if kwargs.get("instructions") else None,
        "reasoning": kwargs.get("reasoning"),
        "text": kwargs.get("text"),
        "tool_choice": kwargs.get("tool_choice"),
        "parallel_tool_calls": kwargs.get("parallel_tool_calls"),
        "tool_names": [tool.get("name") for tool in tools if isinstance(tool, dict)],
        "input_count": len(input_items),
        "input_excerpt": summarized_input,
        "cache_key": kwargs.get("prompt_cache_key"),
        "cache_retention": kwargs.get("prompt_cache_retention"),
        "extra_header_keys": sorted((kwargs.get("extra_headers") or {}).keys()),
    }
    return _truncate_for_log(summary, limit=12000)


def _extract_openai_text_blocks(response: Any) -> list[ContentBlock]:
    """Extract assistant text blocks from Responses API payload variants."""
    content_blocks: list[ContentBlock] = []

    for item in _block_get(response, "output", []) or []:
        if _block_get(item, "type") != "message" or _block_get(item, "role") != "assistant":
            continue
        for part in _block_get(item, "content", []) or []:
            part_type = _block_get(part, "type")
            if part_type not in {"output_text", "text"}:
                continue
            text = _block_get(part, "text")
            if text:
                content_blocks.append(TextContentBlock(text))

    if content_blocks:
        return content_blocks

    fallback_text = _block_get(response, "output_text")
    if isinstance(fallback_text, str) and fallback_text.strip():
        return [TextContentBlock(fallback_text)]
    if isinstance(fallback_text, list):
        for part in fallback_text:
            text = _block_get(part, "text")
            if text:
                content_blocks.append(TextContentBlock(text))

    if content_blocks:
        return content_blocks

    top_level_text = _block_get(response, "text")
    if isinstance(top_level_text, str) and top_level_text.strip():
        return [TextContentBlock(top_level_text)]
    if isinstance(top_level_text, dict):
        text_value = _block_get(top_level_text, "text")
        if text_value:
            return [TextContentBlock(text_value)]

    return content_blocks


def _extract_openai_text_from_event(event: Any) -> str:
    """Extract assistant text from a streaming event payload."""
    event_type = _block_get(event, "type", "")

    if event_type in {"response.output_text.delta", "response.text.delta"}:
        return _block_get(event, "delta", "") or ""

    if event_type in {"response.content_part.added", "response.content_part.done"}:
        part = _block_get(event, "part", None)
        part_type = _block_get(part, "type")
        if part_type in {"output_text", "text"}:
            return _block_get(part, "text", "") or ""
        return ""

    if event_type in {"response.output_item.added", "response.output_item.done"}:
        item = _block_get(event, "item", None)
        if _block_get(item, "type") == "message" and _block_get(item, "role") == "assistant":
            text_blocks = _extract_openai_text_blocks({"output": [item]})
            return "".join(block.text or "" for block in text_blocks if block.type == "text")
        return ""

    return ""


def _extract_openai_reasoning_text_from_event(event: Any) -> str:
    """Extract raw reasoning text only for private token/count telemetry."""
    event_type = _block_get(event, "type", "")
    if event_type == "response.reasoning_text.delta":
        return _block_get(event, "delta", "") or ""
    if event_type == "response.reasoning_text.done":
        return _block_get(event, "text", "") or ""
    return ""


def _extract_openai_reasoning_summary_from_event(event: Any) -> str:
    """Extract public reasoning-summary text from streaming events."""
    event_type = _block_get(event, "type", "")
    if event_type == "response.reasoning_summary_text.delta":
        return _block_get(event, "delta", "") or ""
    if event_type == "response.reasoning_summary_text.done":
        for key in ("text", "summary", "delta"):
            value = _block_get(event, key, "")
            if value:
                return _coerce_text(value)
    if event_type in {"response.reasoning_summary_part.added", "response.reasoning_summary_part.done"}:
        part = _block_get(event, "part", None)
        if _block_get(part, "type") == "summary_text":
            return _coerce_text(_block_get(part, "text", "") or "")
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        item = _block_get(event, "item", None)
        if _block_get(item, "type") == "reasoning":
            summary = _block_get(item, "summary", []) or []
            return " ".join(
                _coerce_text(_block_get(part, "text", ""))
                for part in summary
                if _block_get(part, "type") == "summary_text" and _block_get(part, "text", "")
            )
    return ""


def _merge_streamed_output_into_response(response: Any, streamed_output_items: dict[int, Any]) -> Any:
    """Inject streamed output items into a final response object/dict when output is empty."""
    merged_output = [item for _, item in sorted(streamed_output_items.items(), key=lambda pair: pair[0])]
    if not merged_output:
        return response

    if isinstance(response, dict):
        merged = dict(response)
        merged["output"] = merged_output
        return merged

    raw = None
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(response, attr, None)
        if callable(fn):
            try:
                candidate = fn()
                if isinstance(candidate, dict):
                    raw = candidate
                    break
            except Exception:
                pass

    if raw is None:
        raw = {}
        for key in (
            "id",
            "object",
            "model",
            "status",
            "created_at",
            "completed_at",
            "output_text",
            "output",
            "text",
            "content",
            "incomplete_details",
            "usage",
            "parallel_tool_calls",
        ):
            value = getattr(response, key, None)
            if value is not None:
                raw[key] = value

    raw["output"] = merged_output
    return raw


def _openai_response_to_unified(response, model: str = "") -> LLMResponse:
    """Convert an OpenAI Responses API object to our unified LLMResponse."""
    content_blocks = _extract_openai_text_blocks(response)

    for item in _block_get(response, "output", []) or []:
        item_type = _block_get(item, "type")
        if item_type == "function_call":
            raw_arguments = _block_get(item, "arguments")
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments or {}
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            content_blocks.append(ToolUseContentBlock(
                id=_block_get(item, "call_id") or _block_get(item, "id"),
                name=_block_get(item, "name"),
                input=arguments,
            ))

    stop_reason = (
        StopReason.TOOL_USE
        if any(block.type == ContentBlockType.TOOL_USE for block in content_blocks)
        else StopReason.END_TURN
    )
    incomplete_details = _block_get(response, "incomplete_details", None)
    if _block_get(incomplete_details, "reason") == "max_output_tokens":
        stop_reason = StopReason.MAX_TOKENS

    usage = _usage_from_openai(response)
    if usage.output_tokens > 0 and not content_blocks:
        output_excerpt = _openai_output_excerpt(response)
        response_dump = _openai_response_debug_dump(response)
        logger.warning(
            "OpenAI response parsed to empty content: model=%s response_id=%s stop_reason=%s output_shape=%s incomplete=%s output_excerpt=%s response_dump=%s",
            model or _block_get(response, "model", ""),
            _block_get(response, "id", ""),
            stop_reason,
            _summarize_openai_output_shape(response),
            _block_get(incomplete_details, "reason", None),
            output_excerpt,
            response_dump,
        )

    return LLMResponse(
        content=content_blocks,
        stop_reason=stop_reason,
        usage=usage,
        model=model or _block_get(response, "model", ""),
    )


class OpenAIResponsesTransport:
    """Build OpenAI Responses SDK kwargs from an LLMRequest."""

    def _reasoning_summary_setting(self) -> str | None:
        value = os.environ.get("ILLO_OPENAI_REASONING_SUMMARY", "auto").strip().lower()
        if value in {"", "0", "false", "no", "none", "off"}:
            return None
        if value not in {"auto", "concise", "detailed"}:
            return "auto"
        return value

    def build_kwargs(self, request: LLMRequest) -> dict[str, Any]:
        validate_provider_messages(request.messages, provider=Provider.OPENAI)
        validate_system_blocks(request.system)
        validate_tool_definitions(request.tools)

        openai_kwargs: dict[str, Any] = {
            "model": request.normalized_model,
            "input": _anthropic_messages_to_openai_input(request.messages),
            "store": False,
        }
        if request.max_output_tokens is not None:
            openai_kwargs["max_output_tokens"] = request.max_output_tokens

        instructions = _system_blocks_to_instructions(request.system)
        if instructions:
            openai_kwargs["instructions"] = instructions

        tools = _anthropic_tools_to_openai(request.tools)
        if tools:
            openai_kwargs["tools"] = tools

        model_effort = normalize_model_effort(request.model, request.reasoning_effort)
        effort = (
            render_reasoning_effort("openai", model_effort)
            if model_accepts_effort(request.model, model_effort)
            else None
        )
        if effort:
            reasoning = {"effort": effort}
            summary = self._reasoning_summary_setting()
            if summary:
                reasoning["summary"] = summary
            openai_kwargs["reasoning"] = reasoning
            openai_kwargs["include"] = ["reasoning.encrypted_content"]

        if request.response_format:
            openai_kwargs["text"] = {"format": dict(request.response_format)}

        if request.cache_key:
            openai_kwargs["prompt_cache_key"] = request.cache_key
        if request.cache_retention:
            openai_kwargs["prompt_cache_retention"] = request.cache_retention

        if request.extra_headers:
            openai_kwargs["extra_headers"] = dict(request.extra_headers)

        return normalize_openai_request_kwargs(openai_kwargs)
