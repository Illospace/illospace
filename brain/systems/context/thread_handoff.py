"""Durable thread handoff summaries for persistent agent sessions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import copy
import hashlib
import json
from typing import Any

from brain.systems.context.compaction import split_session_messages_for_compaction
from brain.systems.context.semantic_compaction import (
    CompactionCheckpoint,
    SemanticCompactor,
    build_compaction_checkpoint,
)


DEFAULT_HANDOFF_RECENT_MESSAGES = 32
THREAD_HANDOFF_SCHEMA_VERSION = 1


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return str(value)


def _payload_digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(_jsonable(dict(payload)), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _message_summary(message: dict, *, limit: int = 280) -> str:
    role = str(message.get("role") or "message")
    content = message.get("content")
    if isinstance(content, str):
        text = " ".join(content.split())
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "text":
                parts.append(str(block.get("text") or ""))
            elif block_type == "tool_use":
                parts.append(f"tool_use:{block.get('name') or 'tool'}")
            elif block_type == "tool_result":
                parts.append(f"tool_result:{str(block.get('content') or '')[:120]}")
        text = " ".join(" ".join(parts).split())
    else:
        text = ""
    return f"{role}: {text[:limit]}" if text else f"{role}: <non-text content>"


@dataclass(frozen=True)
class ThreadHandoff:
    """Compact durable summary of a persistent thread up to a message count."""

    checkpoint: CompactionCheckpoint
    message_count: int
    source: str
    schema_version: int = THREAD_HANDOFF_SCHEMA_VERSION
    previous_message_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "ThreadHandoff | None":
        if not isinstance(payload, Mapping):
            return None
        checkpoint_payload = payload.get("checkpoint")
        checkpoint = CompactionCheckpoint.from_payload(
            checkpoint_payload if isinstance(checkpoint_payload, Mapping) else payload,
            source=str(payload.get("source") or "stored_handoff"),
        )
        try:
            message_count = max(0, int(payload.get("message_count") or 0))
        except (TypeError, ValueError):
            message_count = 0
        try:
            previous_message_count = max(0, int(payload.get("previous_message_count") or 0))
        except (TypeError, ValueError):
            previous_message_count = 0
        return cls(
            checkpoint=checkpoint,
            message_count=message_count,
            source=str(payload.get("source") or checkpoint.source or "stored_handoff"),
            schema_version=int(payload.get("schema_version") or THREAD_HANDOFF_SCHEMA_VERSION),
            previous_message_count=previous_message_count,
            metadata=dict(payload.get("metadata") or {}),
        )

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "source": self.source,
            "message_count": self.message_count,
            "previous_message_count": self.previous_message_count,
            "checkpoint": self.checkpoint.to_payload(),
            "metadata": _jsonable(dict(self.metadata or {})),
        }
        payload["digest"] = _payload_digest(payload)
        return payload

    @property
    def digest(self) -> str:
        return str(self.to_payload()["digest"])


def build_thread_handoff(
    *,
    previous_handoff: Mapping[str, Any] | ThreadHandoff | None,
    messages_since: list[dict],
    total_message_count: int,
    session_id: str = "",
    phase: str = "post_run_handoff",
    semantic_compactor: SemanticCompactor | None = None,
    run_id: int | None = None,
) -> tuple[ThreadHandoff, str | None]:
    """Update a durable handoff from the prior handoff plus new raw messages."""
    previous = (
        previous_handoff
        if isinstance(previous_handoff, ThreadHandoff)
        else ThreadHandoff.from_payload(previous_handoff)
    )
    previous_payload = previous.to_payload() if previous else None
    synthetic_previous: list[dict] = []
    if previous_payload:
        synthetic_previous.append({
            "role": "user",
            "content": "[Previous durable thread handoff]\n"
            + json.dumps(previous_payload, sort_keys=True, default=str),
        })
    source_messages = synthetic_previous + list(messages_since or [])
    checkpoint, fallback_error = build_compaction_checkpoint(
        source_messages,
        recent_messages=list(messages_since[-8:] if messages_since else []),
        session_id=session_id,
        phase=phase,
        semantic_compactor=semantic_compactor,
    )
    summary_source = str((checkpoint.metadata or {}).get("summary_source") or "")
    if summary_source == "llm_thread_handoff_compactor":
        source = "llm_thread_handoff_compactor"
    elif checkpoint.source == "semantic_compactor":
        source = "semantic_compactor"
    else:
        source = "deterministic_fallback"
    handoff = ThreadHandoff(
        checkpoint=checkpoint,
        message_count=max(0, int(total_message_count)),
        previous_message_count=previous.message_count if previous else 0,
        source=source,
        metadata={
            "session_id": session_id,
            "phase": phase,
            "run_id": run_id,
            "messages_summarized_this_update": len(messages_since or []),
            "previous_handoff_digest": previous.digest if previous else None,
        },
    )
    return handoff, fallback_error


def recent_raw_messages_for_handoff(
    messages: list[dict],
    *,
    max_recent_messages: int = DEFAULT_HANDOFF_RECENT_MESSAGES,
) -> list[dict]:
    """Return a safe recent transcript suffix without splitting tool pairs."""
    if len(messages) <= max_recent_messages:
        return copy.deepcopy(messages)
    window = split_session_messages_for_compaction(
        messages,
        max_messages=max_recent_messages + 3,
        keep_early=2,
    )
    if window is None:
        return copy.deepcopy(messages[-max_recent_messages:])
    return copy.deepcopy(window.kept_recent)


def thread_handoff_message(
    handoff: Mapping[str, Any] | ThreadHandoff,
    *,
    raw_message_count: int,
    recent_message_count: int,
) -> dict:
    """Render a model-visible handoff message for the next run."""
    resolved = handoff if isinstance(handoff, ThreadHandoff) else ThreadHandoff.from_payload(handoff)
    payload = resolved.to_payload() if resolved else _jsonable(dict(handoff or {}))
    payload["raw_message_count"] = raw_message_count
    payload["recent_raw_message_count"] = recent_message_count
    body = json.dumps(payload, sort_keys=True, indent=2, default=str)
    return {
        "role": "user",
        "content": (
            "[System: Durable thread handoff summary from previous runs. "
            "Use this as compact prior context, prefer the raw recent messages below for fresh details, "
            "and call read_thread_messages if an older exact detail matters.\n"
            f"{body}]"
        ),
    }


def build_thread_handoff_context_messages(
    messages: list[dict],
    *,
    handoff: Mapping[str, Any] | ThreadHandoff | None,
    max_recent_messages: int = DEFAULT_HANDOFF_RECENT_MESSAGES,
) -> list[dict]:
    """Build startup context as durable handoff plus a raw recent suffix."""
    resolved = handoff if isinstance(handoff, ThreadHandoff) else ThreadHandoff.from_payload(handoff)
    recent = recent_raw_messages_for_handoff(messages, max_recent_messages=max_recent_messages)
    if not resolved:
        return recent
    return [
        thread_handoff_message(
            resolved,
            raw_message_count=len(messages),
            recent_message_count=len(recent),
        ),
        *recent,
    ]


__all__ = [
    "DEFAULT_HANDOFF_RECENT_MESSAGES",
    "THREAD_HANDOFF_SCHEMA_VERSION",
    "ThreadHandoff",
    "build_thread_handoff",
    "build_thread_handoff_context_messages",
    "recent_raw_messages_for_handoff",
    "thread_handoff_message",
]
