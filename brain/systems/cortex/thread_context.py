"""Agent-visible Cortex thread context projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunRow
from brain.platform.db.models.idea import IdeaThread

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
    attachments: tuple[dict[str, Any], ...] = ()

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
        if self.attachments:
            payload["attachments_count"] = len(self.attachments)
            payload["attachments"] = [_attachment_payload(attachment) for attachment in self.attachments]
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

    payload: dict[str, Any] = {
        "source": "cortex_visible_thread",
        "idea_id": str(idea_id),
        "omits_current_message": True,
        "message_count": len(kept_entries),
        "formatted": formatted,
        "messages": [entry.to_payload() for entry in kept_entries],
    }
    attachment_context = _thread_attachment_context_from_entries(kept_entries)
    if attachment_context:
        payload["thread_attachment_context"] = attachment_context
    return payload


async def _a_thread_message_entries(session: Any, idea_id: str, *, max_rows: int) -> list[ThreadContextEntry]:
    result = await session.execute(
        select(
            IdeaThread.id,
            IdeaThread.role,
            IdeaThread.content,
            IdeaThread.created_at,
            IdeaThread.attachments,
            IdeaThread.metadata_,
        )
        .where(IdeaThread.idea_id == idea_id)
        .order_by(IdeaThread.created_at.desc(), IdeaThread.id.desc())
        .limit(max(1, int(max_rows)))
    )
    rows = result.all()
    entries: list[ThreadContextEntry] = []
    for row in rows:
        content = _clean_content(row.content)
        if not content:
            continue
        role = _normalize_role(row.role)
        if role not in {"user", "illo", "assistant"}:
            continue
        metadata = row.metadata_
        metadata = metadata if isinstance(metadata, dict) else {}
        entries.append(
            ThreadContextEntry(
                role="illo" if role == "assistant" else role,
                content=content,
                created_at=row.created_at,
                source="thread",
                thread_message_id=_coerce_int(row.id),
                run_id=_coerce_int(metadata.get("run_id")),
                attachments=_normalize_attachments(row.attachments),
            )
        )
    return entries


async def _a_final_answer_entries(session: Any, idea_id: str, *, max_rows: int) -> list[ThreadContextEntry]:
    result = await session.execute(
        select(AgentRunArtifactRow, AgentRunRow)
        .join(AgentRunRow, AgentRunRow.id == AgentRunArtifactRow.run_id)
        .where(
            AgentRunRow.thread_id == str(idea_id),
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
        attachment_summary = _format_attachment_summary(entry.attachments)
        if attachment_summary:
            content = f"{content} {attachment_summary}".strip()
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


def _normalize_attachments(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    normalized: list[dict[str, Any]] = []
    for attachment in value:
        if isinstance(attachment, dict):
            normalized.append(dict(attachment))
    return tuple(normalized)


def _attachment_filename(attachment: dict[str, Any]) -> str:
    for key in ("filename", "name", "label"):
        value = attachment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raw_url = attachment.get("url") or attachment.get("uri") or attachment.get("storage_path")
    if isinstance(raw_url, str) and raw_url.strip():
        return PurePosixPath(raw_url.split("?", 1)[0].rstrip("/")).name or "attachment"
    return "attachment"


def _attachment_mime(attachment: dict[str, Any]) -> str:
    value = (
        attachment.get("content_type")
        or attachment.get("contentType")
        or attachment.get("mime_type")
        or attachment.get("mime")
        or attachment.get("type")
        or ""
    )
    return str(value).split(";", 1)[0].strip().lower()


def _attachment_kind(attachment: dict[str, Any]) -> str:
    mime = _attachment_mime(attachment)
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("text/") or mime in {"application/json", "application/xml", "application/x-yaml"}:
        return "text"
    suffix = PurePosixPath(_attachment_filename(attachment)).suffix.lower().lstrip(".")
    if suffix in {"avif", "gif", "jpeg", "jpg", "png", "webp"}:
        return "image"
    if suffix in {"aac", "aif", "aiff", "flac", "m4a", "mp3", "mp4", "mpeg", "mpga", "oga", "ogg", "opus", "wav", "weba", "webm"}:
        return "audio"
    if suffix in {"csv", "json", "md", "txt", "xml", "yaml", "yml"}:
        return "text"
    return "file"


def _attachment_payload(attachment: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "filename": _attachment_filename(attachment),
        "kind": _attachment_kind(attachment),
    }
    mime = _attachment_mime(attachment)
    if mime:
        payload["mime"] = mime
    url = attachment.get("url") or attachment.get("uri")
    if isinstance(url, str) and url.strip():
        payload["url"] = url.strip()
    return payload


def _format_attachment_summary(attachments: tuple[dict[str, Any], ...]) -> str:
    if not attachments:
        return ""
    parts = [
        f"{_attachment_filename(attachment)} ({_attachment_kind(attachment)})"
        for attachment in attachments[:3]
    ]
    if len(attachments) > 3:
        parts.append(f"+{len(attachments) - 3} more")
    return "[Attachments: " + ", ".join(parts) + "]"


def _thread_attachment_context_from_entries(entries: list[ThreadContextEntry]) -> dict[str, Any] | None:
    attachments: list[dict[str, Any]] = []
    for entry in entries:
        if entry.source != "thread":
            continue
        attachments.extend(dict(attachment) for attachment in entry.attachments)
    if not attachments:
        return None
    try:
        from brain.systems.cortex.thread_attachments import build_thread_attachment_context

        context = build_thread_attachment_context(attachments)
    except Exception:
        logger.debug("visible_thread_attachment_context_build_failed", exc_info=True)
        return None
    if not isinstance(context, dict):
        return None
    context = dict(context)
    context["source"] = "cortex-visible-thread-attachments"
    context["prompt"] = _earlier_attachment_context_prompt(list(context.get("items") or []))
    return context


def _earlier_attachment_context_prompt(items: list[dict[str, Any]]) -> str:
    lines = [
        "## Earlier Thread Attachments",
        "These files were attached to earlier messages in this same thread.",
    ]
    for index, item in enumerate(items, start=1):
        filename = item.get("filename") or f"Attachment {index}"
        kind = item.get("kind") or "file"
        lines.append(f"\n### {index}. {filename} ({kind})")
        if kind == "text":
            text = str(item.get("text") or "").strip()
            if text:
                suffix = "\n\n[Excerpt truncated.]" if item.get("truncated") else ""
                lines.append(f"```text\n{text}{suffix}\n```")
            elif item.get("unavailable"):
                lines.append("Text extraction failed for this file.")
        elif kind == "image":
            lines.append("Image attached earlier in the thread. Inspect the image input when vision is available.")
        elif kind == "audio":
            attachment_id = item.get("id") or f"attachment {index}"
            lines.append(
                f"Audio attached earlier in the thread. Use transcribe_audio_attachment with "
                f"attachment_id={attachment_id} if the spoken content is needed."
            )
    return "\n".join(lines).strip()


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
