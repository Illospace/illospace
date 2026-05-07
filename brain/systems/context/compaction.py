"""Transcript compaction primitives for context runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from brain.systems.sessions import _sanitize_tool_pairs, _summarize_trimmed_messages


def _block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _is_tool_use_assistant(msg: dict) -> bool:
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content", "")
    return isinstance(content, list) and any(_block_type(block) == "tool_use" for block in content)


def _is_tool_result_user(msg: dict) -> bool:
    if msg.get("role") != "user":
        return False
    content = msg.get("content", "")
    return isinstance(content, list) and any(_block_type(block) == "tool_result" for block in content)


def estimate_context_tokens(value: Any) -> int:
    """Approximate model-visible tokens using the runtime's 4 chars/token heuristic."""
    if value is None:
        return 0
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, sort_keys=True, default=str)
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def estimate_session_tokens(
    messages: list[dict],
    *,
    system: Any = None,
    tools: Any = None,
) -> int:
    """Estimate active prompt tokens for an agent turn."""
    return (
        estimate_context_tokens(system)
        + estimate_context_tokens(tools)
        + estimate_context_tokens(messages)
    )


@dataclass(frozen=True)
class CompactionReport:
    """Audit record for omitted transcript content."""

    original_count: int
    kept_count: int
    omitted_count: int
    summary: str
    strategy: str = "preserve_tool_pairs"
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "original_count": self.original_count,
            "kept_count": self.kept_count,
            "omitted_count": self.omitted_count,
            "summary": self.summary,
            "strategy": self.strategy,
            "provenance": dict(self.provenance or {}),
        }


@dataclass(frozen=True)
class CompactionWindow:
    """Transcript slices used to replace an omitted middle span safely."""

    kept_early: list[dict]
    omitted_messages: list[dict]
    kept_recent: list[dict]
    omitted_range: tuple[int, int]


def split_session_messages_for_compaction(
    messages: list[dict],
    *,
    max_messages: int,
    keep_early: int = 2,
) -> CompactionWindow | None:
    """Choose early/recent transcript slices without splitting tool pairs."""
    original_count = len(messages)
    if original_count <= max_messages:
        return None

    early_end = min(keep_early, original_count)
    while early_end < original_count and early_end < max(keep_early + 6, keep_early):
        previous = messages[early_end - 1]
        if _is_tool_use_assistant(previous) or _is_tool_result_user(previous):
            early_end += 1
        else:
            break
    while early_end > 1 and (
        _is_tool_use_assistant(messages[early_end - 1])
        or _is_tool_result_user(messages[early_end - 1])
    ):
        early_end -= 1

    recent_budget = max(1, max_messages - early_end - 1)
    candidate_recent = messages[-recent_budget:]
    safe_start = 0
    for index, msg in enumerate(candidate_recent):
        if _is_tool_result_user(msg):
            safe_start = index + 1
            continue
        if _is_tool_use_assistant(msg):
            if index + 1 < len(candidate_recent) and _is_tool_result_user(candidate_recent[index + 1]):
                break
            safe_start = index + 1
            continue
        break

    kept_early = list(messages[:early_end])
    kept_recent = list(candidate_recent[safe_start:])
    omitted_start = early_end
    omitted_end = original_count - len(candidate_recent) + safe_start
    omitted_messages = list(messages[omitted_start:omitted_end])
    return CompactionWindow(
        kept_early=kept_early,
        omitted_messages=omitted_messages,
        kept_recent=kept_recent,
        omitted_range=(omitted_start, omitted_end),
    )


def compact_session_messages(
    messages: list[dict],
    *,
    max_messages: int,
    session_id: str = "",
    keep_early: int = 2,
) -> tuple[list[dict], CompactionReport]:
    """Trim a transcript while preserving tool_use/tool_result boundaries."""
    original_count = len(messages)
    if original_count <= max_messages:
        return list(messages), CompactionReport(
            original_count=original_count,
            kept_count=original_count,
            omitted_count=0,
            summary="No compaction required.",
            provenance={
                "session_id": session_id,
                "max_messages": max_messages,
                "summary_source": "none",
            },
        )

    window = split_session_messages_for_compaction(
        messages,
        max_messages=max_messages,
        keep_early=keep_early,
    )
    if window is None:
        return list(messages), CompactionReport(
            original_count=original_count,
            kept_count=original_count,
            omitted_count=0,
            summary="No compaction required.",
            provenance={
                "session_id": session_id,
                "max_messages": max_messages,
                "summary_source": "none",
            },
        )

    omitted_start, omitted_end = window.omitted_range
    omitted_messages = window.omitted_messages
    summary = _summarize_trimmed_messages(omitted_messages)
    summary_msg = {
        "role": "user",
        "content": (
            f"[System: {len(omitted_messages)} earlier messages compacted by ContextRuntime. "
            f"Summary provenance=session:{session_id or 'unknown'}.\n{summary}]"
        ),
    }
    compacted = _sanitize_tool_pairs(window.kept_early + [summary_msg] + window.kept_recent, session_id)
    report = CompactionReport(
        original_count=original_count,
        kept_count=len(compacted),
        omitted_count=len(omitted_messages),
        summary=summary,
        provenance={
            "session_id": session_id,
            "max_messages": max_messages,
            "summary_source": "brain.systems.sessions._summarize_trimmed_messages",
            "omitted_range": [omitted_start, omitted_end],
            "tool_pair_safe": True,
        },
    )
    return compacted, report


def compact_session_messages_to_token_budget(
    messages: list[dict],
    *,
    token_limit: int,
    target_tokens: int | None = None,
    session_id: str = "",
    system: Any = None,
    tools: Any = None,
    keep_early: int = 2,
    max_messages: int = 40,
    min_messages: int = 8,
) -> tuple[list[dict], CompactionReport]:
    """Compact a transcript until it falls under a token budget.

    This is intentionally layered on top of ``compact_session_messages`` so the
    tool-use/tool-result boundary guarantees remain in one implementation.
    """
    original_tokens = estimate_session_tokens(messages, system=system, tools=tools)
    if token_limit <= 0 or original_tokens <= token_limit:
        return list(messages), CompactionReport(
            original_count=len(messages),
            kept_count=len(messages),
            omitted_count=0,
            summary="No compaction required.",
            strategy="token_budget_preserve_tool_pairs",
            provenance={
                "session_id": session_id,
                "token_limit": token_limit,
                "target_tokens": target_tokens,
                "original_estimated_tokens": original_tokens,
                "final_estimated_tokens": original_tokens,
                "summary_source": "none",
            },
        )

    target = int(target_tokens or max(min_messages, token_limit * 7 // 10))
    candidate_limits = []
    start = min(max_messages, max(min_messages, len(messages) - 1))
    current = start
    while current >= min_messages:
        candidate_limits.append(current)
        current -= 4
    if min_messages not in candidate_limits:
        candidate_limits.append(min_messages)

    best_messages = list(messages)
    best_report: CompactionReport | None = None
    best_tokens = original_tokens

    for message_limit in candidate_limits:
        compacted, report = compact_session_messages(
            messages,
            max_messages=message_limit,
            session_id=session_id,
            keep_early=keep_early,
        )
        estimated = estimate_session_tokens(compacted, system=system, tools=tools)
        best_messages, best_report, best_tokens = compacted, report, estimated
        if estimated <= target:
            break

    report = best_report or CompactionReport(
        original_count=len(messages),
        kept_count=len(best_messages),
        omitted_count=max(0, len(messages) - len(best_messages)),
        summary="Token-budget compaction produced no detailed report.",
    )
    return best_messages, CompactionReport(
        original_count=report.original_count,
        kept_count=len(best_messages),
        omitted_count=report.omitted_count,
        summary=report.summary,
        strategy="token_budget_preserve_tool_pairs",
        provenance={
            **dict(report.provenance or {}),
            "session_id": session_id,
            "token_limit": token_limit,
            "target_tokens": target,
            "original_estimated_tokens": original_tokens,
            "final_estimated_tokens": best_tokens,
            "message_limit": len(best_messages),
        },
    )
