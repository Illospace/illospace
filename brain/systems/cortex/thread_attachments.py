"""Thread attachment context for Cortex runs.

Thread attachments are immediate message context. They are deliberately
separate from durable Project Context profiles: a user should be able to drop a
file into any thread and have Illo see it, even when no project is attached.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from brain.app.api.routers.cortex._helpers import UPLOAD_DIR

READABLE_TEXT_EXTENSIONS = {"csv", "json", "md", "txt", "xml", "yaml", "yml"}
READABLE_TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "text/csv",
    "text/markdown",
    "text/plain",
    "text/xml",
    "text/yaml",
}
IMAGE_EXTENSIONS = {"avif", "gif", "jpeg", "jpg", "png", "webp"}
MAX_TEXT_ATTACHMENT_CHARS = 18_000
MAX_TOTAL_TEXT_ATTACHMENT_CHARS = 36_000
MAX_IMAGE_BLOCK_BYTES = 8_000_000
MAX_CONTEXT_ATTACHMENTS = 20


def attachment_content_type(attachment: dict[str, Any]) -> str:
    value = (
        attachment.get("content_type")
        or attachment.get("contentType")
        or attachment.get("mime_type")
        or attachment.get("mime")
        or attachment.get("type")
        or ""
    )
    return str(value).split(";", 1)[0].strip().lower()


def attachment_display_name(attachment: dict[str, Any], path: Path | None = None) -> str:
    for key in ("filename", "name", "label"):
        value = attachment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return path.name if path is not None else "attachment"


def upload_path_from_attachment(attachment: dict[str, Any]) -> Path | None:
    """Resolve a Cortex upload attachment to a backend-readable local file."""

    root = UPLOAD_DIR.resolve()
    raw_path = attachment.get("storage_path")
    if isinstance(raw_path, str) and raw_path.strip():
        try:
            candidate = Path(raw_path).expanduser().resolve()
            if candidate.is_file() and (candidate == root or candidate.is_relative_to(root)):
                return candidate
        except OSError:
            return None

    raw_url = attachment.get("url") or attachment.get("uri")
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None
    parsed = urlsplit(raw_url.strip())
    if parsed.scheme or parsed.netloc:
        return None
    prefix = "/static/uploads/"
    if not parsed.path.startswith(prefix):
        return None
    relative = unquote(parsed.path[len(prefix):]).strip("/")
    if not relative:
        return None
    try:
        candidate = (UPLOAD_DIR / relative).resolve()
        if candidate.is_file() and (candidate == root or candidate.is_relative_to(root)):
            return candidate
    except OSError:
        return None
    return None


def is_readable_text_attachment(attachment: dict[str, Any], path: Path) -> bool:
    ext = path.suffix.lower().lstrip(".")
    if ext in READABLE_TEXT_EXTENSIONS:
        return True
    content_type = attachment_content_type(attachment)
    return content_type.startswith("text/") or content_type in READABLE_TEXT_MIME_TYPES


def is_image_attachment(attachment: dict[str, Any], path: Path) -> bool:
    ext = path.suffix.lower().lstrip(".")
    if ext in IMAGE_EXTENSIONS:
        return True
    return attachment_content_type(attachment).startswith("image/")


def _read_text_excerpt(path: Path, *, limit: int = MAX_TEXT_ATTACHMENT_CHARS) -> tuple[str, bool]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


def _size_from_attachment(attachment: dict[str, Any], path: Path) -> int | None:
    try:
        return int(attachment.get("size") or path.stat().st_size)
    except (OSError, TypeError, ValueError):
        return None


def _base_item(attachment: dict[str, Any], path: Path, *, kind: str) -> dict[str, Any]:
    name = attachment_display_name(attachment, path)
    stable_id = hashlib.sha1(f"{path}:{name}".encode("utf-8", errors="ignore")).hexdigest()[:16]
    item: dict[str, Any] = {
        "id": f"attachment-{stable_id}",
        "kind": kind,
        "filename": name,
        "path": str(path),
        "url": attachment.get("url") or attachment.get("uri"),
        "mime": attachment_content_type(attachment),
        "size": _size_from_attachment(attachment, path),
    }
    return {key: value for key, value in item.items() if value not in (None, "", {}, [])}


def build_thread_attachment_context(
    attachments: list[dict[str, Any]] | None,
    *,
    include_text: bool = True,
) -> dict[str, Any] | None:
    """Build run-visible context for attachments on one thread message."""

    items: list[dict[str, Any]] = []
    remaining_text_chars = MAX_TOTAL_TEXT_ATTACHMENT_CHARS
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        path = upload_path_from_attachment(attachment)
        if path is None:
            continue
        if is_readable_text_attachment(attachment, path):
            item = _base_item(attachment, path, kind="text")
            if include_text:
                try:
                    limit = max(0, min(MAX_TEXT_ATTACHMENT_CHARS, remaining_text_chars))
                    text, truncated = _read_text_excerpt(path, limit=limit) if limit else ("", True)
                    remaining_text_chars -= len(text)
                    item["text"] = text
                    item["truncated"] = truncated
                except OSError:
                    item["unavailable"] = True
            items.append(item)
        elif is_image_attachment(attachment, path):
            items.append(_base_item(attachment, path, kind="image"))
        if len(items) >= MAX_CONTEXT_ATTACHMENTS:
            break

    if not items:
        return None
    return {
        "version": 1,
        "source": "cortex-thread-attachments",
        "items": items,
        "attachment_count": len(items),
        "prompt": attachment_context_prompt(items),
    }


def project_context_from_text_attachments(attachments: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Return Project Context resources for readable text files.

    This supports current run materialization while keeping Thread Attachment
    Context as the primary immediate-message module.
    """

    resources: list[dict[str, Any]] = []
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            continue
        path = upload_path_from_attachment(attachment)
        if path is None or not is_readable_text_attachment(attachment, path):
            continue
        name = attachment_display_name(attachment, path)
        resource: dict[str, Any] = {
            "id": f"attachment-{len(resources) + 1}",
            "kind": "file",
            "type": "file",
            "label": name,
            "name": name,
            "path": str(path),
            "source": "thread_attachment",
        }
        url = attachment.get("url") or attachment.get("uri")
        if isinstance(url, str) and url.strip():
            resource["uri"] = url.strip()
        content_type = attachment_content_type(attachment)
        if content_type:
            resource["mime"] = content_type
        size = _size_from_attachment(attachment, path)
        if size is not None:
            resource["size"] = size
        resources.append(resource)
        if len(resources) >= MAX_CONTEXT_ATTACHMENTS:
            break
    if not resources:
        return None
    return {
        "version": 1,
        "source": "cortex-thread-attachments",
        "selected_profile_name": "Thread attachments",
        "validation_status": "client_validated",
        "resources": resources,
    }


def attachment_context_prompt(items: list[dict[str, Any]]) -> str:
    lines = ["## Thread Attachments", "The user attached these files to this message."]
    for index, item in enumerate(items, start=1):
        filename = item.get("filename") or f"Attachment {index}"
        kind = item.get("kind") or "file"
        lines.append(f"\n### {index}. {filename} ({kind})")
        if item.get("kind") == "text":
            text = str(item.get("text") or "").strip()
            if text:
                suffix = "\n\n[Excerpt truncated.]" if item.get("truncated") else ""
                lines.append(f"```text\n{text}{suffix}\n```")
            elif item.get("unavailable"):
                lines.append("Text extraction failed for this file.")
        elif item.get("kind") == "image":
            lines.append("Image attached. Inspect the image input when vision is available.")
    return "\n".join(lines).strip()


def image_content_blocks_from_attachment_context(
    context: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build Anthropic-style image blocks from attachment context."""

    blocks: list[dict[str, Any]] = []
    skipped: list[str] = []
    if not isinstance(context, dict):
        return blocks, skipped
    for item in context.get("items") or []:
        if not isinstance(item, dict) or item.get("kind") != "image":
            continue
        path_value = item.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            continue
        path = Path(path_value)
        try:
            data = path.read_bytes()
        except OSError:
            skipped.append(str(item.get("filename") or path.name))
            continue
        if len(data) > MAX_IMAGE_BLOCK_BYTES:
            skipped.append(f"{item.get('filename') or path.name} (too large for vision input)")
            continue
        media_type = str(item.get("mime") or "").strip() or "image/png"
        if not media_type.startswith("image/"):
            media_type = "image/png"
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(data).decode("ascii"),
            },
        })
    return blocks, skipped


def initial_user_content_blocks(message: str, context: dict[str, Any] | None) -> list[dict[str, Any]] | str:
    """Return text+image content blocks for the initial user message."""

    if not isinstance(context, dict):
        return message
    prompt = str(context.get("prompt") or "").strip()
    text = str(message or "")
    if prompt:
        text = f"{text}\n\n{prompt}".strip()
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    image_blocks, skipped = image_content_blocks_from_attachment_context(context)
    blocks.extend(image_blocks)
    if skipped:
        blocks[0]["text"] += "\n\nImages not included as vision input: " + ", ".join(skipped)
    return blocks


__all__ = [
    "attachment_content_type",
    "attachment_context_prompt",
    "attachment_display_name",
    "build_thread_attachment_context",
    "image_content_blocks_from_attachment_context",
    "initial_user_content_blocks",
    "is_image_attachment",
    "is_readable_text_attachment",
    "project_context_from_text_attachments",
    "upload_path_from_attachment",
]
