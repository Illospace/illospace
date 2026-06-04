"""Publish generated run artifacts as previewable Cortex thread assets."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Iterable

from brain.systems.cortex.upload_preview import public_static_upload_url, static_upload_url_for

UPLOAD_DIR = Path(__file__).resolve().parents[2] / "uploads"
MAX_THREAD_ASSET_SIZE = 10 * 1024 * 1024
IMAGE_EXTENSIONS = {"avif", "gif", "jpeg", "jpg", "png", "svg", "webp"}
VIEWER_EXTENSIONS = {
    *IMAGE_EXTENSIONS,
    "csv",
    "json",
    "log",
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


def _asset_markdown(*, label: str, url: str, kind: str) -> str:
    clean_label = label.replace("[", "").replace("]", "").strip() or "Thread asset"
    if kind == "image":
        return f"![{clean_label}]({url})"
    return f"[{clean_label}]({url})"


def publish_thread_asset(
    file_path: str,
    *,
    thread_id: str | None = None,
    title: str | None = None,
    upload_dir: Path = UPLOAD_DIR,
    source_roots: Iterable[str | Path] | None = None,
    max_bytes: int = MAX_THREAD_ASSET_SIZE,
) -> dict[str, Any]:
    """Copy a generated artifact into static uploads and return a chat attachment."""

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
    destination_dir = (upload_dir / THREAD_ASSET_PREFIX / thread_segment).resolve()
    upload_root = upload_dir.resolve()
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
        "markdown": _asset_markdown(label=label, url=url, kind=kind),
        "attachment": attachment,
        "instruction": (
            "To show this in Thread Discussion, call post_thread_discussion_reply with the returned "
            "markdown in body and include the returned attachment in attachments."
        ),
    }


__all__ = ["publish_thread_asset"]
