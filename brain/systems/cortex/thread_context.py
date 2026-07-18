"""Agent-visible Cortex thread context projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunRow
from brain.platform.db.models.idea import IdeaThread
from brain.systems.runs.cortex.read_models import (
    public_failures_for_run_ids,
    public_run_linked_message,
    run_id_from_public_message_metadata,
)

DEFAULT_THREAD_CONTEXT_LIMIT = 16
DEFAULT_THREAD_CONTEXT_CHAR_LIMIT = 8000
MAX_ENTRY_CHARS = 1600
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThreadContextEntry:
    role: str
    content: str
    created_at: datetime | None = None
    source: str = "thread"
    thread_message_id: int | None = None
    run_id: int | None = None
    artifact_id: int | None = None

    def timestamp(self) -> datetime:
        value = self.created_at
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        return datetime.min.replace(tzinfo=timezone.utc)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
            "source": self.source,
        }
        if self.created_at is not None:
            payload["created_at"] = self.created_at.isoformat()
        if self.thread_message_id is not None:
            payload["thread_message_id"] = self.thread_message_id
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.artifact_id is not None:
            payload["artifact_id"] = self.artifact_id
        return payload


async def async_build_agent_visible_thread_context(
    session: Any,
    idea_id: str,
    *,
    current_thread_message_id: int | None = None,
    current_message: str | None = None,
    limit: int = DEFAULT_THREAD_CONTEXT_LIMIT,
    char_limit: int = DEFAULT_THREAD_CONTEXT_CHAR_LIMIT,
) -> dict[str, Any] | None:
    """Async equivalent of ``build_agent_visible_thread_context``."""

    if not idea_id or not hasattr(session, "execute"):
        return None

    try:
        entries = [
            *(await _a_thread_message_entries(
                session,
                idea_id,
                max_rows=max(int(limit) * 3, int(limit) + 8),
            )),
            *(await _a_final_answer_entries(
                session,
                idea_id,
                max_rows=max(int(limit) * 2, int(limit) + 4),
            )),
        ]
    except Exception:
        logger.debug("agent_visible_thread_context_load_failed", exc_info=True)
        return None

    entries = _drop_current_message(
        entries,
        current_thread_message_id=current_thread_message_id,
        current_message=current_message,
    )
    entries = _drop_duplicate_final_answers(entries)
    if not entries:
        return None

    entries.sort(key=lambda entry: (entry.timestamp(), entry.source, entry.thread_message_id or entry.artifact_id or 0))
    entries = entries[-max(1, int(limit)) :]
    formatted, kept_entries = _format_entries(entries, char_limit=max(1, int(char_limit)))
    if not kept_entries or not formatted:
        return None

    return {
        "source": "cortex_visible_thread",
        "idea_id": str(idea_id),
        "omits_current_message": True,
        "message_count": len(kept_entries),
        "formatted": formatted,
        "messages": [entry.to_payload() for entry in kept_entries],
    }


async def _a_thread_message_entries(session: Any, idea_id: str, *, max_rows: int) -> list[ThreadContextEntry]:
    result = await session.execute(
        select(
            IdeaThread.id,
            IdeaThread.role,
            IdeaThread.content,
            IdeaThread.created_at,
            IdeaThread.metadata_,
        )
        .where(IdeaThread.idea_id == idea_id)
        .order_by(IdeaThread.created_at.desc(), IdeaThread.id.desc())
        .limit(max(1, int(max_rows)))
    )
    rows = list(result.all())
    run_ids = {
        run_id
        for run_id in (
            (
                run_id_from_public_message_metadata(row.metadata_)
                if _normalize_role(row.role) in {"illo", "assistant"}
                else None
            )
            for row in rows
        )
        if run_id is not None
    }
    failures = await public_failures_for_run_ids(session, run_ids, thread_id=idea_id)
    entries: list[ThreadContextEntry] = []
    for row in rows:
        metadata = row.metadata_
        metadata = metadata if isinstance(metadata, dict) else {}
        role = _normalize_role(row.role)
        run_id = (
            run_id_from_public_message_metadata(metadata)
            if role in {"illo", "assistant"}
            else None
        )
        projected_content, _metadata = public_run_linked_message(
            row.content,
            metadata,
            failures.get(run_id),
        )
        content = _clean_content(projected_content)
        if not content:
            continue
        if role not in {"user", "illo", "assistant"}:
            continue
        entries.append(
            ThreadContextEntry(
                role="illo" if role == "assistant" else role,
                content=content,
                created_at=row.created_at,
                source="thread",
                thread_message_id=_coerce_int(row.id),
                run_id=run_id,
            )
        )
    return entries


async def _a_final_answer_entries(session: Any, idea_id: str, *, max_rows: int) -> list[ThreadContextEntry]:
    result = await session.execute(
        select(AgentRunArtifactRow, AgentRunRow)
        .join(AgentRunRow, AgentRunRow.id == AgentRunArtifactRow.run_id)
        .where(
            AgentRunRow.thread_id == str(idea_id),
            AgentRunRow.status == "completed",
            AgentRunArtifactRow.artifact_type == "final_answer",
        )
        .order_by(AgentRunArtifactRow.created_at.desc(), AgentRunArtifactRow.id.desc())
        .limit(max(1, int(max_rows)))
    )
    rows = result.all()
    entries: list[ThreadContextEntry] = []
    for artifact, run in rows:
        content = _clean_content(getattr(artifact, "text", None))
        if not content:
            continue
        entries.append(
            ThreadContextEntry(
                role="illo",
                content=content,
                created_at=getattr(artifact, "created_at", None) or getattr(run, "completed_at", None),
                source="run_final_answer",
                run_id=_coerce_int(getattr(run, "id", None)),
                artifact_id=_coerce_int(getattr(artifact, "id", None)),
            )
        )
    return entries


def _drop_current_message(
    entries: list[ThreadContextEntry],
    *,
    current_thread_message_id: int | None,
    current_message: str | None,
) -> list[ThreadContextEntry]:
    if current_thread_message_id is not None:
        return [
            entry for entry in entries
            if entry.thread_message_id != int(current_thread_message_id)
        ]

    normalized_current = _normalize_for_match(current_message)
    if not normalized_current:
        return entries

    latest_index: int | None = None
    latest_ts = datetime.min.replace(tzinfo=timezone.utc)
    for index, entry in enumerate(entries):
        if entry.source != "thread" or entry.role != "user":
            continue
        if _normalize_for_match(entry.content) != normalized_current:
            continue
        timestamp = entry.timestamp()
        if latest_index is None or timestamp >= latest_ts:
            latest_index = index
            latest_ts = timestamp
    if latest_index is None:
        return entries
    return [entry for index, entry in enumerate(entries) if index != latest_index]


def _drop_duplicate_final_answers(entries: list[ThreadContextEntry]) -> list[ThreadContextEntry]:
    thread_reply_run_ids = {
        int(entry.run_id)
        for entry in entries
        if entry.source == "thread" and entry.role == "illo" and entry.run_id is not None
    }
    if not thread_reply_run_ids:
        return entries
    return [
        entry for entry in entries
        if not (entry.source == "run_final_answer" and entry.run_id in thread_reply_run_ids)
    ]


def _format_entries(
    entries: list[ThreadContextEntry],
    *,
    char_limit: int,
) -> tuple[str, list[ThreadContextEntry]]:
    selected: list[tuple[str, ThreadContextEntry]] = []
    used = 0
    for entry in reversed(entries):
        label = "User" if entry.role == "user" else "Illo"
        content = _truncate(entry.content, MAX_ENTRY_CHARS)
        line = f"{label}: {content}"
        next_used = used + len(line) + (1 if selected else 0)
        if selected and next_used > char_limit:
            continue
        if not selected and len(line) > char_limit:
            line = _truncate(line, char_limit)
            next_used = len(line)
        selected.append((line, entry))
        used = next_used
    selected.reverse()
    lines = [line for line, _entry in selected]
    kept = [entry for _line, entry in selected]
    return "\n".join(lines).strip(), kept


def _normalize_role(value: Any) -> str:
    return str(value or "").strip().lower()


def _clean_content(value: Any) -> str:
    return " ".join(str(value or "").replace("\r\n", "\n").replace("\r", "\n").split())


def _normalize_for_match(value: Any) -> str:
    return _clean_content(value).casefold()


def _truncate(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_THREAD_CONTEXT_CHAR_LIMIT",
    "DEFAULT_THREAD_CONTEXT_LIMIT",
    "ThreadContextEntry",
    "async_build_agent_visible_thread_context",
]
