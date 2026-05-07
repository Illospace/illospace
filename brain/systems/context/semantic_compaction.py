"""Structured transcript checkpointing for long-running agent context."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
import hashlib
import json
import re
from typing import Any

from brain.systems.context.compaction import (
    CompactionReport,
    estimate_session_tokens,
    split_session_messages_for_compaction,
)
from brain.systems.sessions import _sanitize_tool_pairs, _summarize_trimmed_messages

SemanticCompactor = Callable[[list[dict], dict[str, Any]], Mapping[str, Any] | str | None]

_CONSTRAINT_TERMS = (
    "must",
    "never",
    "do not",
    "don't",
    "avoid",
    "required",
    "requirement",
    "constraint",
    "acceptance",
    "important",
)
_PATH_RE = re.compile(r"(?<![\w/.-])[\w./-]+\.(?:py|ts|tsx|svelte|js|jsx|json|md|toml|ya?ml|sql|sh|rs|go)(?![\w/.-])")


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


def _tuple_of_text(value: Any, *, limit: int = 20) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, Mapping):
        items = [json.dumps(_jsonable(value), sort_keys=True, default=str)]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value if str(item or "").strip()]
    else:
        items = [str(value)]
    cleaned = []
    seen = set()
    for item in items:
        text = " ".join(str(item or "").split())
        if not text or text in seen:
            continue
        cleaned.append(text)
        seen.add(text)
        if len(cleaned) >= limit:
            break
    return tuple(cleaned)


def _content_text(content: Any, *, limit: int = 1_000) -> str:
    if isinstance(content, str):
        return " ".join(content.split())[:limit]
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(str(block.get("text") or ""))
        elif block_type == "tool_result":
            tool_content = block.get("content")
            if isinstance(tool_content, str):
                parts.append(tool_content)
            else:
                parts.append(json.dumps(_jsonable(tool_content), sort_keys=True, default=str))
        elif block_type == "tool_use":
            name = block.get("name") or "tool"
            args = block.get("input") or {}
            parts.append(f"tool_use {name}: {json.dumps(_jsonable(args), sort_keys=True, default=str)}")
    return " ".join(" ".join(parts).split())[:limit]


def _latest_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        text = _content_text(msg.get("content"))
        if text:
            return text
    return ""


def _first_user_text(messages: list[dict]) -> str:
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = _content_text(msg.get("content"))
        if text:
            return text
    return ""


def _extract_constraint_candidates(messages: list[dict]) -> tuple[str, ...]:
    candidates: list[str] = []
    seen = set()
    for msg in messages:
        text = _content_text(msg.get("content"), limit=1_500)
        if not text:
            continue
        lowered = text.lower()
        if not any(term in lowered for term in _CONSTRAINT_TERMS):
            continue
        excerpt = text[:320].strip()
        if excerpt and excerpt not in seen:
            candidates.append(excerpt)
            seen.add(excerpt)
        if len(candidates) >= 12:
            break
    return tuple(candidates)


def _extract_paths(messages: list[dict]) -> tuple[str, ...]:
    paths: list[str] = []
    seen = set()
    for msg in messages:
        text = _content_text(msg.get("content"), limit=2_000)
        for path in _PATH_RE.findall(text):
            if path in seen:
                continue
            paths.append(path)
            seen.add(path)
            if len(paths) >= 20:
                return tuple(paths)
    return tuple(paths)


@dataclass(frozen=True)
class CompactionCheckpoint:
    """Model-visible state ledger that replaces an omitted transcript span."""

    active_objective: str = ""
    user_constraints: tuple[str, ...] = ()
    completed_work: tuple[str, ...] = ()
    current_plan: tuple[str, ...] = ()
    files_or_objects_touched: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    failed_attempts: tuple[str, ...] = ()
    important_tool_results: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    verification_status: str = ""
    recent_user_intent: str = ""
    risks_or_unknowns: tuple[str, ...] = ()
    source: str = "deterministic_fallback"
    schema_version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | str | None, *, source: str) -> "CompactionCheckpoint":
        if payload is None:
            return cls(source=source)
        if isinstance(payload, str):
            return cls(active_objective=" ".join(payload.split()), source=source)
        return cls(
            active_objective=str(payload.get("active_objective") or "").strip(),
            user_constraints=_tuple_of_text(payload.get("user_constraints")),
            completed_work=_tuple_of_text(payload.get("completed_work")),
            current_plan=_tuple_of_text(payload.get("current_plan")),
            files_or_objects_touched=_tuple_of_text(
                payload.get("files_or_objects_touched") or payload.get("files_touched")
            ),
            decisions=_tuple_of_text(payload.get("decisions")),
            failed_attempts=_tuple_of_text(payload.get("failed_attempts")),
            important_tool_results=_tuple_of_text(payload.get("important_tool_results")),
            open_questions=_tuple_of_text(payload.get("open_questions")),
            verification_status=str(payload.get("verification_status") or "").strip(),
            recent_user_intent=str(payload.get("recent_user_intent") or "").strip(),
            risks_or_unknowns=_tuple_of_text(payload.get("risks_or_unknowns")),
            source=source,
            schema_version=int(payload.get("schema_version") or 1),
            metadata=dict(payload.get("metadata") or {}),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "active_objective": self.active_objective,
            "user_constraints": list(self.user_constraints),
            "completed_work": list(self.completed_work),
            "current_plan": list(self.current_plan),
            "files_or_objects_touched": list(self.files_or_objects_touched),
            "decisions": list(self.decisions),
            "failed_attempts": list(self.failed_attempts),
            "important_tool_results": list(self.important_tool_results),
            "open_questions": list(self.open_questions),
            "verification_status": self.verification_status,
            "recent_user_intent": self.recent_user_intent,
            "risks_or_unknowns": list(self.risks_or_unknowns),
            "metadata": _jsonable(dict(self.metadata or {})),
        }

    @property
    def digest(self) -> str:
        raw = json.dumps(self.to_payload(), sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def deterministic_checkpoint_from_messages(
    omitted_messages: list[dict],
    *,
    recent_messages: list[dict] | None = None,
    session_id: str = "",
    phase: str = "",
    fallback_error: str | None = None,
) -> CompactionCheckpoint:
    """Build a structured checkpoint without a model call."""
    summary = _summarize_trimmed_messages(omitted_messages)
    recent = list(recent_messages or [])
    constraints = _extract_constraint_candidates(omitted_messages)
    paths = _extract_paths(omitted_messages)
    risks = ["Semantic compactor unavailable; checkpoint was built from deterministic transcript excerpts."]
    if fallback_error:
        risks.append(f"Semantic compactor failed: {fallback_error[:240]}")
    return CompactionCheckpoint(
        active_objective=_first_user_text(omitted_messages)[:500],
        user_constraints=constraints,
        completed_work=(summary,),
        current_plan=(),
        files_or_objects_touched=paths,
        decisions=(),
        failed_attempts=(),
        important_tool_results=(),
        open_questions=(),
        verification_status="unknown",
        recent_user_intent=_latest_user_text(recent)[:500],
        risks_or_unknowns=tuple(risks),
        source="deterministic_fallback",
        metadata={
            "session_id": session_id,
            "phase": phase,
            "omitted_message_count": len(omitted_messages),
        },
    )


def build_compaction_checkpoint(
    omitted_messages: list[dict],
    *,
    recent_messages: list[dict] | None = None,
    session_id: str = "",
    phase: str = "",
    semantic_compactor: SemanticCompactor | None = None,
) -> tuple[CompactionCheckpoint, str | None]:
    """Use an injected semantic compactor when available, otherwise fallback."""
    if semantic_compactor is None:
        return deterministic_checkpoint_from_messages(
            omitted_messages,
            recent_messages=recent_messages,
            session_id=session_id,
            phase=phase,
        ), None
    context = {
        "session_id": session_id,
        "phase": phase,
        "recent_messages": list(recent_messages or []),
        "schema": {
            "active_objective": "string",
            "user_constraints": ["string"],
            "completed_work": ["string"],
            "current_plan": ["string"],
            "files_or_objects_touched": ["string"],
            "decisions": ["string"],
            "failed_attempts": ["string"],
            "important_tool_results": ["string"],
            "open_questions": ["string"],
            "verification_status": "string",
            "recent_user_intent": "string",
            "risks_or_unknowns": ["string"],
        },
    }
    try:
        payload = semantic_compactor(list(omitted_messages), context)
        checkpoint = CompactionCheckpoint.from_payload(payload, source="semantic_compactor")
        checkpoint = replace(
            checkpoint,
            recent_user_intent=checkpoint.recent_user_intent or _latest_user_text(list(recent_messages or []))[:500],
            metadata={
                **dict(checkpoint.metadata or {}),
                "session_id": session_id,
                "phase": phase,
                "omitted_message_count": len(omitted_messages),
            },
        )
        return checkpoint, None
    except Exception as exc:
        fallback_error = f"{type(exc).__name__}: {exc}"
        return deterministic_checkpoint_from_messages(
            omitted_messages,
            recent_messages=recent_messages,
            session_id=session_id,
            phase=phase,
            fallback_error=fallback_error,
        ), fallback_error


def checkpoint_message(checkpoint: CompactionCheckpoint, *, omitted_count: int) -> dict:
    payload = checkpoint.to_payload()
    payload["checkpoint_digest"] = checkpoint.digest
    body = json.dumps(payload, sort_keys=True, indent=2, default=str)
    return {
        "role": "user",
        "content": (
            "[System: Context compaction checkpoint. "
            f"{omitted_count} older messages were summarized to preserve the current task state. "
            "Continue from this checkpoint plus the raw recent turns. Do not claim certainty about omitted details.\n"
            f"{body}]"
        ),
    }


def compact_session_messages_with_checkpoint(
    messages: list[dict],
    *,
    token_limit: int,
    target_tokens: int | None = None,
    session_id: str = "",
    phase: str = "",
    system: Any = None,
    tools: Any = None,
    keep_early: int = 2,
    max_messages: int = 40,
    min_messages: int = 8,
    force: bool = False,
    emergency: bool = False,
    semantic_compactor: SemanticCompactor | None = None,
) -> tuple[list[dict], CompactionReport]:
    """Compact a transcript using a structured state checkpoint."""
    original_tokens = estimate_session_tokens(messages, system=system, tools=tools)
    if not force and (token_limit <= 0 or original_tokens <= token_limit):
        return list(messages), CompactionReport(
            original_count=len(messages),
            kept_count=len(messages),
            omitted_count=0,
            summary="No compaction required.",
            strategy="semantic_checkpoint",
            provenance={
                "session_id": session_id,
                "phase": phase,
                "token_limit": token_limit,
                "target_tokens": target_tokens,
                "original_estimated_tokens": original_tokens,
                "final_estimated_tokens": original_tokens,
                "summary_source": "none",
            },
        )

    target = int(target_tokens or max(1, token_limit * 7 // 10))
    start = min(max_messages, max(min_messages, len(messages) - 1))
    candidate_limits: list[int] = []
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
        window = split_session_messages_for_compaction(
            messages,
            max_messages=message_limit,
            keep_early=keep_early,
        )
        if window is None or not window.omitted_messages:
            continue
        checkpoint, fallback_error = build_compaction_checkpoint(
            window.omitted_messages,
            recent_messages=window.kept_recent,
            session_id=session_id,
            phase=phase,
            semantic_compactor=semantic_compactor,
        )
        summary_msg = checkpoint_message(checkpoint, omitted_count=len(window.omitted_messages))
        compacted = _sanitize_tool_pairs(window.kept_early + [summary_msg] + window.kept_recent, session_id)
        estimated = estimate_session_tokens(compacted, system=system, tools=tools)
        strategy = "semantic_checkpoint" if checkpoint.source == "semantic_compactor" else "structured_checkpoint_fallback"
        if emergency:
            strategy = f"emergency_{strategy}"
        report = CompactionReport(
            original_count=len(messages),
            kept_count=len(compacted),
            omitted_count=len(window.omitted_messages),
            summary=checkpoint.active_objective or checkpoint.recent_user_intent or "Context checkpoint created.",
            strategy=strategy,
            provenance={
                "session_id": session_id,
                "phase": phase,
                "token_limit": token_limit,
                "target_tokens": target,
                "original_estimated_tokens": original_tokens,
                "final_estimated_tokens": estimated,
                "message_limit": message_limit,
                "omitted_range": list(window.omitted_range),
                "tool_pair_safe": True,
                "checkpoint_source": checkpoint.source,
                "checkpoint_schema_version": checkpoint.schema_version,
                "checkpoint_digest": checkpoint.digest,
                "semantic_fallback_error": fallback_error,
                "force": force,
                "emergency": emergency,
            },
        )
        best_messages, best_report, best_tokens = compacted, report, estimated
        if estimated <= target:
            break

    if best_report is None:
        return list(messages), CompactionReport(
            original_count=len(messages),
            kept_count=len(messages),
            omitted_count=0,
            summary="No safe transcript messages were eligible for checkpoint compaction.",
            strategy="emergency_checkpoint_unavailable" if emergency else "checkpoint_unavailable",
            provenance={
                "session_id": session_id,
                "phase": phase,
                "token_limit": token_limit,
                "target_tokens": target,
                "original_estimated_tokens": original_tokens,
                "final_estimated_tokens": original_tokens,
                "force": force,
                "emergency": emergency,
            },
        )

    return best_messages, CompactionReport(
        original_count=best_report.original_count,
        kept_count=best_report.kept_count,
        omitted_count=best_report.omitted_count,
        summary=best_report.summary,
        strategy=best_report.strategy,
        provenance={
            **dict(best_report.provenance or {}),
            "final_estimated_tokens": best_tokens,
        },
    )

