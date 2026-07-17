"""Publish generated run artifacts as previewable Cortex thread assets."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlencode

from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.cortex.upload_preview import (
    normalize_static_upload_url,
    public_static_upload_url,
    resolve_static_upload_path,
    static_upload_url_for,
)

UPLOAD_DIR = Path(__file__).resolve().parents[2] / "uploads"
MAX_THREAD_ASSET_SIZE = 10 * 1024 * 1024
IMAGE_EXTENSIONS = {"avif", "gif", "jpeg", "jpg", "png", "svg", "webp"}
VIEWER_EXTENSIONS = {
    *IMAGE_EXTENSIONS,
    "csv",
    "json",
    "log",
    "markdown",
    "md",
    "pdf",
    "text",
    "tsv",
    "txt",
    "xml",
    "yaml",
    "yml",
}

CONTENT_TYPE_BY_EXTENSION = {
    "avif": "image/avif",
    "csv": "text/csv",
    "gif": "image/gif",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "json": "application/json",
    "log": "text/plain",
    "markdown": "text/markdown",
    "md": "text/markdown",
    "pdf": "application/pdf",
    "png": "image/png",
    "svg": "image/svg+xml",
    "text": "text/plain",
    "tsv": "text/tab-separated-values",
    "txt": "text/plain",
    "webp": "image/webp",
    "xml": "application/xml",
    "yaml": "application/x-yaml",
    "yml": "application/x-yaml",
}

DEFAULT_SOURCE_ROOTS = ("/workspaces/artifacts",)
THREAD_ASSET_PREFIX = "thread-assets"
STATIC_UPLOAD_LINK_PATTERN = re.compile(r"(?:https?://[^\s<>\"')]+)?/static/uploads/[^\s<>\"')]+")


def _resolved_roots(source_roots: Iterable[str | Path] | None = None) -> list[Path]:
    values = list(source_roots or [])
    if not values:
        configured = os.environ.get("ILLO_THREAD_ASSET_SOURCE_ROOTS", "").strip()
        values = configured.split(os.pathsep) if configured else list(DEFAULT_SOURCE_ROOTS)
    roots: list[Path] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        try:
            roots.append(Path(text).expanduser().resolve())
        except OSError:
            continue
    return roots


def _is_within(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except AttributeError:
        return path == root or str(path).startswith(str(root) + os.sep)


def _source_path(file_path: str, *, source_roots: Iterable[str | Path] | None = None) -> Path:
    raw = str(file_path or "").strip()
    if not raw:
        raise ValueError("file_path is required")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Thread asset source not found: {file_path}")

    roots = _resolved_roots(source_roots)
    if not any(_is_within(path, root) for root in roots):
        allowed = ", ".join(str(root) for root in roots) or "(none configured)"
        raise PermissionError(f"Thread assets can only be published from configured artifact roots: {allowed}")
    return path


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("._-")
    return (cleaned or fallback)[:96]


def _content_type(path: Path, extension: str) -> str:
    return mimetypes.guess_type(path.name)[0] or CONTENT_TYPE_BY_EXTENSION.get(extension, "application/octet-stream")


def _asset_attachment_from_upload(
    value: str,
    *,
    title: str | None = None,
    upload_dir: Path | None = None,
) -> tuple[Path, str, dict[str, Any]]:
    target_upload_dir = upload_dir or UPLOAD_DIR
    path = resolve_static_upload_path(value, upload_dir=target_upload_dir)
    extension = path.suffix.lower().lstrip(".")
    if extension not in VIEWER_EXTENSIONS:
        raise ValueError(f"Thread asset type .{extension or '(none)'} is not previewable")

    url = normalize_static_upload_url(value)
    size = path.stat().st_size
    content_type = _content_type(path, extension)
    kind = "image" if content_type.startswith("image/") or extension in IMAGE_EXTENSIONS else "file"
    label = str(title or path.name).strip() or path.name
    attachment = {
        "kind": kind,
        "url": url,
        "download_url": public_static_upload_url(url),
        "filename": path.name,
        "label": label,
        "content_type": content_type,
        "mime_type": content_type,
        "size": size,
    }
    return path, url, attachment


def _asset_markdown(*, label: str, url: str, kind: str) -> str:
    clean_label = label.replace("[", "").replace("]", "").strip() or "Thread asset"
    if kind == "image":
        return f"![{clean_label}]({url})"
    return f"[{clean_label}]({url})"


def _viewer_url(*, url: str, label: str) -> str:
    """Absolute /doc link teammates can open without a session."""

    query = urlencode({"src": url, "title": label}, quote_via=quote, safe="/")
    return f"{public_app_base_url()}/doc?{query}"


_PUBLISH_INSTRUCTION = (
    "To show this in a Thread, write the returned markdown or /static/uploads route "
    "in post_thread_discussion_reply or post_ai_timeline_message. Valid upload routes "
    "are promoted to visible attachments automatically. When sharing outside the app "
    "(e.g. a Slack message), link viewer_url — it opens as a readable page with no sign-in."
)


def publish_thread_asset(
    file_path: str,
    *,
    thread_id: str | None = None,
    title: str | None = None,
    upload_dir: Path | None = None,
    source_roots: Iterable[str | Path] | None = None,
    max_bytes: int = MAX_THREAD_ASSET_SIZE,
) -> dict[str, Any]:
    """Copy a generated artifact into static uploads and return a chat attachment."""

    target_upload_dir = upload_dir or UPLOAD_DIR
    raw_file_path = str(file_path or "").strip()
    if raw_file_path:
        try:
            published_path, url, attachment = _asset_attachment_from_upload(
                raw_file_path,
                title=title,
                upload_dir=target_upload_dir,
            )
            return {
                "ok": True,
                "source_path": str(published_path),
                "published_path": str(published_path),
                "url": url,
                "public_url": attachment["download_url"],
                "viewer_url": _viewer_url(
                    url=url,
                    label=attachment["label"],
                ),
                "markdown": _asset_markdown(label=attachment["label"], url=url, kind=attachment["kind"]),
                "attachment": attachment,
                "already_published": True,
                "instruction": _PUBLISH_INSTRUCTION,
            }
        except ValueError:
            pass
        except FileNotFoundError:
            pass

    source = _source_path(file_path, source_roots=source_roots)
    extension = source.suffix.lower().lstrip(".")
    if extension not in VIEWER_EXTENSIONS:
        raise ValueError(f"Thread asset type .{extension or '(none)'} is not previewable")

    data = source.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f"Thread asset is too large ({len(data)} bytes, max {max_bytes})")

    digest = hashlib.sha256(data).hexdigest()[:12]
    stem = _safe_segment(source.stem, "asset")
    thread_segment = _safe_segment(thread_id or "shared", "shared")
    filename = f"{stem}-{digest}.{extension}"
    destination_dir = (target_upload_dir / THREAD_ASSET_PREFIX / thread_segment).resolve()
    upload_root = target_upload_dir.resolve()
    if not _is_within(destination_dir, upload_root):
        raise ValueError("Thread asset destination escapes upload root")
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / filename
    if not destination.exists():
        destination.write_bytes(data)

    url = static_upload_url_for(THREAD_ASSET_PREFIX, thread_segment, filename)
    content_type = _content_type(source, extension)
    kind = "image" if content_type.startswith("image/") or extension in IMAGE_EXTENSIONS else "file"
    label = str(title or source.name).strip() or source.name
    attachment = {
        "kind": kind,
        "url": url,
        "download_url": public_static_upload_url(url),
        "filename": source.name,
        "label": label,
        "content_type": content_type,
        "mime_type": content_type,
        "size": len(data),
    }
    return {
        "ok": True,
        "source_path": str(source),
        "published_path": str(destination),
        "url": url,
        "public_url": attachment["download_url"],
        "viewer_url": _viewer_url(
            url=url,
            label=label,
        ),
        "markdown": _asset_markdown(label=label, url=url, kind=kind),
        "attachment": attachment,
        "instruction": _PUBLISH_INSTRUCTION,
    }


def attachment_for_published_thread_asset(
    value: str,
    *,
    title: str | None = None,
    upload_dir: Path | None = None,
) -> dict[str, Any]:
    """Return a visible attachment for an existing /static/uploads artifact."""

    _, _, attachment = _asset_attachment_from_upload(value, title=title, upload_dir=upload_dir)
    return attachment


def infer_thread_asset_attachments_from_body(
    body: str | None,
    *,
    existing_attachments: list[dict[str, Any]] | None = None,
    upload_dir: Path | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Promote existing /static/uploads links in message text into visible attachments."""

    attachments = list(existing_attachments or [])
    existing_urls = set()
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        existing_url = str(attachment.get("url") or attachment.get("href") or "").strip()
        if not existing_url:
            continue
        try:
            existing_urls.add(normalize_static_upload_url(existing_url))
        except ValueError:
            existing_urls.add(existing_url)
    text = str(body or "")
    if not text:
        return attachments

    for match in STATIC_UPLOAD_LINK_PATTERN.finditer(text):
        if len(attachments) >= limit:
            break
        raw_url = match.group(0).rstrip("),.;!?")
        try:
            url = normalize_static_upload_url(raw_url)
        except ValueError:
            continue
        if not url or url in existing_urls:
            continue
        try:
            attachment = attachment_for_published_thread_asset(url, upload_dir=upload_dir)
        except (FileNotFoundError, ValueError):
            continue
        attachments.append(attachment)
        existing_urls.add(url)
    return attachments


__all__ = [
    "attachment_for_published_thread_asset",
    "infer_thread_asset_attachments_from_body",
    "publish_thread_asset",
]
