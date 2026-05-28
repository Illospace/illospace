from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.thread_attachments import (
    attachment_content_type,
    attachment_display_name,
    is_audio_attachment,
    upload_path_from_attachment,
)
from brain.systems.runtime_settings.audio_transcription import (
    AudioTranscriptionError,
    async_transcribe_audio_path,
)
from brain.systems.runs.tool_catalog.handlers.common import _agent_context


async def _handle_transcribe_audio_attachment(
    attachment_id: str | None = None,
    attachment_url: str | None = None,
    path: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Transcribe one current-thread audio attachment through OpenAI Realtime."""

    selected = _resolve_audio_attachment(
        attachment_id=attachment_id,
        attachment_url=attachment_url,
        path=path,
    )
    if "error" in selected:
        return selected

    audio_path = Path(str(selected["path"]))
    try:
        async with UnitOfWork() as uow:
            result = await async_transcribe_audio_path(
                uow.session,
                audio_path,
                language=language,
                filename=str(selected.get("filename") or audio_path.name),
                safety_identifier=_safety_identifier(),
            )
    except AudioTranscriptionError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Audio transcription failed: {exc}"}

    payload = result.to_payload()
    payload["source"] = {
        key: selected[key]
        for key in ("id", "filename", "url", "mime", "size")
        if selected.get(key) not in (None, "", {}, [])
    }
    return payload


def _resolve_audio_attachment(
    *,
    attachment_id: str | None,
    attachment_url: str | None,
    path: str | None,
) -> dict[str, Any]:
    context_items = _current_audio_context_items()
    explicit = {
        "id": str(attachment_id or "").strip(),
        "url": str(attachment_url or "").strip(),
        "path": str(path or "").strip(),
    }
    matches = [item for item in context_items if _matches_explicit(item, explicit)]
    if matches:
        return dict(matches[0])
    if any(explicit.values()):
        direct = _resolve_direct_audio(explicit)
        if direct:
            return direct
        return {"error": "Audio attachment not found in the current thread context."}
    if len(context_items) == 1:
        return dict(context_items[0])
    if len(context_items) > 1:
        return {"error": "Multiple audio attachments are available. Provide attachment_id."}
    return {"error": "No audio attachment is available in the current thread context."}


def _current_audio_context_items() -> list[dict[str, Any]]:
    context = _current_thread_attachment_context()
    items = context.get("items") if isinstance(context, dict) else None
    audio_items: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        path_value = str(item.get("path") or "").strip()
        if not path_value:
            continue
        candidate = Path(path_value)
        if item.get("kind") == "audio" or is_audio_attachment(item, candidate):
            audio_items.append(dict(item))
    return audio_items


def _current_thread_attachment_context() -> dict[str, Any]:
    metadata = getattr(_agent_context, "execution_metadata", None)
    context = _context_from_container(metadata)
    if context:
        return context
    target_ref = getattr(_agent_context, "target_ref", None)
    context = _context_from_container(target_ref)
    if context:
        return context
    workspace_ref = getattr(_agent_context, "workspace_ref", None)
    context = _context_from_container(workspace_ref)
    if context:
        return context
    return {}


def _context_from_container(container: object) -> dict[str, Any]:
    if not isinstance(container, Mapping):
        return {}
    direct = container.get("thread_attachment_context")
    if isinstance(direct, dict):
        return direct
    for key in ("target_ref", "workspace_ref"):
        nested = container.get(key)
        if isinstance(nested, Mapping) and isinstance(nested.get("thread_attachment_context"), dict):
            return dict(nested["thread_attachment_context"])
    return {}


def _matches_explicit(item: dict[str, Any], explicit: dict[str, str]) -> bool:
    if explicit["id"] and explicit["id"] == str(item.get("id") or "").strip():
        return True
    if explicit["url"] and explicit["url"] == str(item.get("url") or "").strip():
        return True
    if explicit["path"] and explicit["path"] == str(item.get("path") or "").strip():
        return True
    return False


def _resolve_direct_audio(explicit: dict[str, str]) -> dict[str, Any] | None:
    attachment: dict[str, Any] = {}
    if explicit["url"]:
        attachment["url"] = explicit["url"]
    if explicit["path"]:
        attachment["storage_path"] = explicit["path"]
    resolved = upload_path_from_attachment(attachment)
    if resolved is None or not is_audio_attachment(attachment, resolved):
        return None
    return {
        "id": f"attachment-{hashlib.sha1(str(resolved).encode('utf-8', errors='ignore')).hexdigest()[:16]}",
        "kind": "audio",
        "filename": attachment_display_name(attachment, resolved),
        "path": str(resolved),
        "url": explicit["url"] or None,
        "mime": attachment_content_type(attachment),
        "size": resolved.stat().st_size,
    }


def _safety_identifier() -> str | None:
    metadata = getattr(_agent_context, "execution_metadata", None)
    user_id = getattr(_agent_context, "user_id", None)
    org_id = getattr(_agent_context, "org_id", None)
    if isinstance(metadata, Mapping):
        user_id = user_id or metadata.get("user_id")
        org_id = org_id or metadata.get("org_id")
    if not user_id:
        return None
    digest = hashlib.sha256(f"{org_id or ''}:{user_id}".encode("utf-8", errors="ignore")).hexdigest()[:32]
    return f"illo-user-{digest}"
