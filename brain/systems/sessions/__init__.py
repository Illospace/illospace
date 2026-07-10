"""Session persistence and message management for the Illo agent loop.

Contains load/save, sanitization, cache management, and content
normalization functions.
"""

from __future__ import annotations

import inspect
import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger("agent")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _session_execute(session: Any, *args: Any, **kwargs: Any) -> Any:
    return await _maybe_await(session.execute(*args, **kwargs))


def _run_session_sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        with asyncio.Runner() as runner:
            return runner.run(awaitable)
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()
    raise RuntimeError("Session sync facade cannot run inside an active event loop; await the async session API")


# ── Tool Pair Sanitization ────────────────────────────────────


def _sanitize_tool_pairs(messages: list[dict], session_id: str = "") -> list[dict]:
    """Strip invalid tool_use/tool_result blocks from a message list.

    Anthropic requires every assistant `tool_use` block to be followed
    immediately by a user message containing the matching `tool_result`
    blocks. Matching IDs elsewhere in the transcript are still invalid.
    """

    def _block_type(block):
        if isinstance(block, dict):
            return block.get("type")
        return getattr(block, "type", None)

    def _tool_use_id(block):
        if isinstance(block, dict):
            return block.get("id")
        return getattr(block, "id", None)

    def _tool_result_id(block):
        if isinstance(block, dict):
            return block.get("tool_use_id")
        return getattr(block, "tool_use_id", None)

    def _drop_blocks(content, drop_predicate):
        if not isinstance(content, list):
            return content, 0
        kept = []
        removed = 0
        for block in content:
            if drop_predicate(block):
                removed += 1
                continue
            kept.append(block)
        return kept, removed

    cleaned: list[dict] = []
    pending_tool_ids: set[str] = set()
    pending_assistant_idx: int | None = None
    removed_tool_use = 0
    removed_tool_result = 0

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")

        if pending_tool_ids:
            matching_result_ids = set()
            if role == "user" and isinstance(content, list):
                matching_result_ids = {
                    _tool_result_id(block)
                    for block in content
                    if _block_type(block) == "tool_result" and _tool_result_id(block) in pending_tool_ids
                }

            if role == "user" and matching_result_ids == pending_tool_ids:
                sanitized_content, removed = _drop_blocks(
                    content,
                    lambda block: _block_type(block) == "tool_result"
                    and _tool_result_id(block) not in pending_tool_ids,
                )
                removed_tool_result += removed
                if sanitized_content:
                    cleaned.append({**msg, "content": sanitized_content})
                else:
                    cleaned.append({**msg, "content": []})
                pending_tool_ids = set()
                pending_assistant_idx = None
                continue

            if pending_assistant_idx is not None:
                assistant_msg = cleaned[pending_assistant_idx]
                sanitized_assistant, removed = _drop_blocks(
                    assistant_msg.get("content", []),
                    lambda block: _block_type(block) == "tool_use"
                    and _tool_use_id(block) in pending_tool_ids,
                )
                removed_tool_use += removed
                if sanitized_assistant:
                    cleaned[pending_assistant_idx] = {**assistant_msg, "content": sanitized_assistant}
                else:
                    cleaned.pop(pending_assistant_idx)
                    pending_assistant_idx = None

            pending_tool_ids = set()

            if role == "user" and isinstance(content, list):
                sanitized_content, removed = _drop_blocks(
                    content,
                    lambda block: _block_type(block) == "tool_result",
                )
                removed_tool_result += removed
                content = sanitized_content
                if not content:
                    continue
                msg = {**msg, "content": content}

        if isinstance(content, list):
            tool_use_ids = {
                _tool_use_id(block)
                for block in content
                if _block_type(block) == "tool_use" and _tool_use_id(block)
            }
            if role == "user":
                sanitized_content, removed = _drop_blocks(
                    content,
                    lambda block: _block_type(block) == "tool_result",
                )
                removed_tool_result += removed
                content = sanitized_content
                msg = {**msg, "content": content}
                if not content:
                    continue
            cleaned.append(msg)
            if role == "assistant" and tool_use_ids:
                pending_tool_ids = tool_use_ids
                pending_assistant_idx = len(cleaned) - 1
            continue

        cleaned.append(msg)

    if pending_tool_ids and pending_assistant_idx is not None:
        assistant_msg = cleaned[pending_assistant_idx]
        sanitized_assistant, removed = _drop_blocks(
            assistant_msg.get("content", []),
            lambda block: _block_type(block) == "tool_use"
            and _tool_use_id(block) in pending_tool_ids,
        )
        removed_tool_use += removed
        if sanitized_assistant:
            cleaned[pending_assistant_idx] = {**assistant_msg, "content": sanitized_assistant}
        else:
            cleaned.pop(pending_assistant_idx)

    if removed_tool_use or removed_tool_result:
        logger.warning(
            f"Session {session_id}: sanitizing {removed_tool_use} invalid tool_use, "
            f"{removed_tool_result} invalid tool_result blocks"
        )

    return cleaned


# ── Session Load/Save ─────────────────────────────────────────


async def async_load_session(session_id: str, user_id: str | None = None) -> tuple[list[dict], str | None]:
    """Load conversation messages from DB. Returns (messages, system_prompt).
    If user_id is provided, validates ownership — returns empty if mismatch.
    """
    try:
        from sqlalchemy import text as sa_text
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        async with UnitOfWork() as uow:
            if user_id:
                row_result = await _session_execute(uow.session, sa_text(
                    "SELECT messages, system_prompt FROM agent_sessions "
                    "WHERE session_id = :sid AND user_id = :uid"
                ), {"sid": session_id, "uid": user_id})
            else:
                row_result = await _session_execute(uow.session, sa_text(
                    "SELECT messages, system_prompt FROM agent_sessions WHERE session_id = :sid"
                ), {"sid": session_id})
            row = row_result.mappings().first()
            if row:
                messages = row["messages"] if isinstance(row["messages"], list) else json.loads(row["messages"])
                # Sanitize: strip orphaned tool pairs from corrupted sessions
                messages = _sanitize_tool_pairs(messages, session_id)
                # Strip SDK-internal fields (e.g. parsed_output) that may
                # have been persisted by older code and cause API 400 errors
                _sanitize_content_blocks(messages)
                return messages, row.get("system_prompt")
    except Exception as e:
        logger.debug(f"Session load failed for {session_id}: {e}")
    return [], None


def _load_session(session_id: str, user_id: str | None = None) -> tuple[list[dict], str | None]:
    """Sync agent-loop compatibility wrapper around async session load."""
    return _run_session_sync(async_load_session(session_id, user_id=user_id))


async def async_save_session(
    session_id: str,
    messages: list[dict],
    system_prompt: str | None,
    tokens_input: int,
    tokens_output: int,
    cache_read: int,
    cache_creation: int,
    user_id: str | None = None,
):
    """Save conversation messages to DB (upsert)."""
    try:
        from sqlalchemy import text as sa_text
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        # NOTE: Do NOT strip thinking blocks before persisting.
        # The API requires thinking/redacted_thinking in all assistant turns
        # when thinking is enabled. Stripping them causes 500 on session reload.
        clean_messages = messages

        async with UnitOfWork() as uow:
            await _session_execute(uow.session, sa_text("""
                INSERT INTO agent_sessions (session_id, messages, system_prompt,
                    total_input_tokens, total_output_tokens,
                    total_cache_read, total_cache_creation, user_id)
                VALUES (:sid, :msgs, :sys_prompt, :ti, :to, :cr, :cc, :uid)
                ON CONFLICT (session_id) DO UPDATE SET
                    messages = EXCLUDED.messages,
                    system_prompt = EXCLUDED.system_prompt,
                    total_input_tokens = agent_sessions.total_input_tokens + EXCLUDED.total_input_tokens,
                    total_output_tokens = agent_sessions.total_output_tokens + EXCLUDED.total_output_tokens,
                    total_cache_read = agent_sessions.total_cache_read + EXCLUDED.total_cache_read,
                    total_cache_creation = agent_sessions.total_cache_creation + EXCLUDED.total_cache_creation,
                    updated_at = NOW()
            """), {
                "sid": session_id,
                "msgs": json.dumps(clean_messages, default=str),
                "sys_prompt": system_prompt,
                "ti": tokens_input, "to": tokens_output,
                "cr": cache_read, "cc": cache_creation,
                "uid": user_id,
            })
    except Exception as e:
        logger.warning(f"Session save failed for {session_id}: {e}")


def _save_session(
    session_id: str,
    messages: list[dict],
    system_prompt: str | None,
    tokens_input: int,
    tokens_output: int,
    cache_read: int,
    cache_creation: int,
    user_id: str | None = None,
):
    """Sync agent-loop compatibility wrapper around async session save."""
    return _run_session_sync(
        async_save_session(
            session_id,
            messages,
            system_prompt,
            tokens_input,
            tokens_output,
            cache_read,
            cache_creation,
            user_id=user_id,
        )
    )


async def async_load_session_handoff(session_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    """Load the durable handoff summary for a persistent agent session."""
    try:
        from sqlalchemy import text as sa_text
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        async with UnitOfWork() as uow:
            if user_id:
                row_result = await _session_execute(uow.session, sa_text(
                    "SELECT handoff_summary FROM agent_sessions "
                    "WHERE session_id = :sid AND user_id = :uid"
                ), {"sid": session_id, "uid": user_id})
            else:
                row_result = await _session_execute(uow.session, sa_text(
                    "SELECT handoff_summary FROM agent_sessions WHERE session_id = :sid"
                ), {"sid": session_id})
            row = row_result.mappings().first()
            if not row:
                return None
            value = row.get("handoff_summary")
            if isinstance(value, dict):
                return value
            if isinstance(value, str) and value.strip():
                loaded = json.loads(value)
                return loaded if isinstance(loaded, dict) else None
    except Exception as e:
        logger.debug(f"Session handoff load failed for {session_id}: {e}")
    return None


def _load_session_handoff(session_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    """Sync agent-loop compatibility wrapper around async handoff load."""
    return _run_session_sync(async_load_session_handoff(session_id, user_id=user_id))


async def async_save_session_handoff(
    session_id: str,
    handoff_summary: dict[str, Any] | None,
    *,
    user_id: str | None = None,
) -> None:
    """Persist the durable handoff summary for a session if the schema supports it."""
    if not handoff_summary:
        return
    try:
        from sqlalchemy import text as sa_text
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        async with UnitOfWork() as uow:
            if user_id:
                await _session_execute(uow.session, sa_text("""
                    UPDATE agent_sessions
                    SET handoff_summary = :handoff,
                        handoff_message_count = :message_count,
                        handoff_updated_at = NOW(),
                        updated_at = NOW()
                    WHERE session_id = :sid AND user_id = :uid
                """), {
                    "sid": session_id,
                    "uid": user_id,
                    "handoff": json.dumps(handoff_summary, default=str),
                    "message_count": int(handoff_summary.get("message_count") or 0),
                })
            else:
                await _session_execute(uow.session, sa_text("""
                    UPDATE agent_sessions
                    SET handoff_summary = :handoff,
                        handoff_message_count = :message_count,
                        handoff_updated_at = NOW(),
                        updated_at = NOW()
                    WHERE session_id = :sid
                """), {
                    "sid": session_id,
                    "handoff": json.dumps(handoff_summary, default=str),
                    "message_count": int(handoff_summary.get("message_count") or 0),
                })
    except Exception as e:
        logger.debug(f"Session handoff save failed for {session_id}: {e}")


def _save_session_handoff(
    session_id: str,
    handoff_summary: dict[str, Any] | None,
    *,
    user_id: str | None = None,
) -> None:
    """Sync agent-loop compatibility wrapper around async handoff save."""
    return _run_session_sync(
        async_save_session_handoff(
            session_id,
            handoff_summary,
            user_id=user_id,
        )
    )


def _compact_message_for_read(index: int, message: dict, *, max_chars: int) -> dict[str, Any]:
    """Return a bounded JSON-safe view of one stored thread message."""
    content = message.get("content")
    if isinstance(content, str):
        text = content
    else:
        text = json.dumps(content, sort_keys=True, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (message content truncated)"
    return {
        "index": index,
        "role": message.get("role"),
        "content": text,
    }


async def async_read_thread_messages(
    session_id: str,
    *,
    user_id: str | None = None,
    mode: str = "recent",
    start_index: int | None = None,
    end_index: int | None = None,
    query: str | None = None,
    limit: int = 20,
    max_chars: int = 8_000,
) -> dict[str, Any]:
    """Read/search stored raw messages for a persistent thread."""
    messages, _ = await async_load_session(session_id, user_id=user_id)
    total = len(messages)
    limit = max(1, min(int(limit or 20), 100))
    per_message_chars = max(200, min(int(max_chars or 8_000), 20_000))
    mode = str(mode or "recent").strip().lower()

    indexed = list(enumerate(messages))
    if mode == "search":
        needle = str(query or "").strip().lower()
        if not needle:
            selected: list[tuple[int, dict]] = []
        else:
            selected = [
                (index, message)
                for index, message in indexed
                if needle in json.dumps(message, sort_keys=True, default=str).lower()
            ][:limit]
    elif mode == "range":
        start = max(0, int(start_index or 0))
        end = total if end_index is None else min(total, max(start, int(end_index)))
        selected = indexed[start:end][:limit]
    else:
        selected = indexed[-limit:]

    return {
        "session_id": session_id,
        "mode": mode,
        "message_count": total,
        "returned_count": len(selected),
        "messages": [
            _compact_message_for_read(index, message, max_chars=per_message_chars)
            for index, message in selected
        ],
    }


# ── Message Processing ────────────────────────────────────────


def _strip_thinking_from_messages(messages: list[dict]) -> list[dict]:
    """Remove thinking blocks from messages before DB storage.

    Thinking blocks can be large and contain redacted content.
    They're needed within a single run but not for persistence.
    """
    cleaned = []
    for msg in messages:
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            filtered_content = [
                block for block in msg["content"]
                if not (isinstance(block, dict) and block.get("type") in ("thinking", "redacted_thinking"))
            ]
            if filtered_content:
                cleaned.append({**msg, "content": filtered_content})
        else:
            cleaned.append(msg)
    return cleaned


def _summarize_trimmed_messages(messages: list[dict]) -> str:
    """Build a brief summary of messages being trimmed from a session.

    Extracts key content from assistant text blocks and user messages,
    producing a condensed overview of what was discussed.
    """
    summary_parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if isinstance(content, str) and content.strip():
            # Plain text message — extract first 150 chars
            text = content.strip()[:150]
            if text:
                summary_parts.append(f"{role}: {text}")
        elif isinstance(content, list):
            # Structured content — extract text blocks only
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()[:150]
                    if text:
                        summary_parts.append(f"{role}: {text}")
                        break  # Only first text block per message

    # Keep to ~2000 chars total
    result = "\n".join(summary_parts)
    if len(result) > 2000:
        result = result[:2000] + "\n... (further content omitted)"
    return result or "No significant content in trimmed messages."


# ── Cache Management ──────────────────────────────────────────


def _clear_message_cache_breakpoints(messages: list[dict]) -> None:
    """Remove cache_control from all message blocks.

    Anthropic allows max 4 breakpoints per request. We use 1 on the system
    prompt and 1 on the latest message, so we need to clear old ones each turn.
    """
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)


def _set_cache_breakpoint(message: dict) -> None:
    """Add a cache_control breakpoint to the last content block of a message.

    Anthropic caches everything up to the breakpoint at 10% cost on subsequent
    requests. By placing a breakpoint on the last message before each API call,
    the entire conversation history becomes cached on the next loop iteration.

    Works with both string content and list-of-blocks content.
    """
    content = message.get("content")
    if isinstance(content, str):
        # Convert to block format so we can attach cache_control
        message["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list) and content:
        # Find the last block and attach cache_control
        last_block = content[-1]
        if isinstance(last_block, dict):
            last_block["cache_control"] = {"type": "ephemeral"}


# ── Content Block Sanitization ────────────────────────────────

# Fields the Anthropic API accepts per content block type.
# Anything else (e.g. parsed_output from streaming SDK) must be stripped
# before sending back as conversation history.
_ALLOWED_BLOCK_FIELDS = {
    "text": {"type", "text", "citations", "cache_control"},
    "tool_use": {"type", "id", "name", "input", "cache_control"},
    "tool_result": {"type", "tool_use_id", "content", "is_error", "cache_control"},
    "thinking": {"type", "thinking", "signature"},
    "redacted_thinking": {"type", "data"},
    "image": {"type", "source", "cache_control"},
    "document": {"type", "source", "cache_control"},
}


def _content_to_dicts(content_blocks) -> list[dict]:
    """Convert SDK content blocks to clean API-safe dicts.

    The SDK can produce objects with extra fields (parsed_output from
    streaming, nested objects instead of strings). This function
    normalizes everything to exactly what the API accepts — no more,
    no less. This prevents corrupted data from ever being persisted.
    """
    result = []
    for block in content_blocks:
        if hasattr(block, "model_dump"):
            d = block.model_dump(exclude_none=True)
        elif isinstance(block, dict):
            d = {k: v for k, v in block.items() if v is not None}
        else:
            d = {"type": "text", "text": str(block)}

        block_type = d.get("type", "text")

        # Normalize text blocks: ensure "text" is a string, not an object
        if block_type == "text" and isinstance(d.get("text"), dict):
            text_obj = d["text"]
            d["text"] = text_obj.get("text", str(text_obj))

        # Normalize thinking blocks: ensure "thinking" is a string
        if block_type == "thinking" and isinstance(d.get("thinking"), dict):
            think_obj = d["thinking"]
            d["thinking"] = think_obj.get("thinking", str(think_obj))

        # Strip to only API-accepted fields
        allowed = _ALLOWED_BLOCK_FIELDS.get(block_type)
        if allowed:
            d = {k: v for k, v in d.items() if k in allowed}

        result.append(d)
    return result


def _sanitize_content_blocks(messages: list[dict]):
    """Strip SDK-internal fields from persisted message content blocks.

    Older sessions may have been saved with fields like parsed_output
    that the API rejects (400 invalid_request_error). This mutates
    content blocks in-place to remove anything not in _ALLOWED_BLOCK_FIELDS.
    """
    stripped = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            # Always strip parsed_output regardless of block type — can be
            # top-level OR nested inside the "text" field (when text is an
            # object instead of a string, e.g. from SDK streaming response)
            if "parsed_output" in block:
                del block["parsed_output"]
                stripped += 1
            # Fix text blocks where "text" is an object instead of a string
            # (SDK streaming can produce {type:"text", text:{parsed_output:..., text:"actual"}})
            if block.get("type") == "text" and isinstance(block.get("text"), dict):
                text_obj = block["text"]
                block["text"] = text_obj.get("text", str(text_obj))
                stripped += 1
            # Strip any other unknown fields based on block type
            block_type = block.get("type", "text")
            allowed = _ALLOWED_BLOCK_FIELDS.get(block_type)
            if allowed:
                extra_keys = set(block.keys()) - allowed
                for k in extra_keys:
                    del block[k]
                    stripped += 1
    if stripped:
        logger.info(f"Sanitized {stripped} invalid fields from session content blocks")
