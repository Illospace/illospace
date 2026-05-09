"""Backend-owned Project Context upload policy and path helpers."""
from __future__ import annotations

import re
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from typing import Any


PROJECT_CONTEXT_UPLOAD_MAX_FILES = 200
PROJECT_CONTEXT_UPLOAD_MAX_FILE_SIZE = 10_000_000
PROJECT_CONTEXT_UPLOAD_MAX_TOTAL_SIZE = 20_000_000


def safe_upload_relative_path(value: str, fallback: str) -> str:
    raw = (value or fallback or "file").replace("\\", "/").strip().strip("/")
    if not raw:
        raw = fallback or "file"
    parts: list[str] = []
    for part in PurePosixPath(raw).parts:
        if part in {"", ".", ".."}:
            continue
        cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", part).strip(" .")
        if cleaned:
            parts.append(cleaned[:120])
    if not parts:
        parts = [re.sub(r"[^A-Za-z0-9._ -]+", "_", fallback or "file").strip(" .") or "file"]
    return str(PurePosixPath(*parts))


def unique_upload_relative_path(relative_path: str, used_paths: set[str]) -> str:
    if relative_path not in used_paths:
        used_paths.add(relative_path)
        return relative_path
    path = PurePosixPath(relative_path)
    stem = path.stem or "file"
    suffix = path.suffix
    parent = path.parent if str(path.parent) != "." else PurePosixPath()
    counter = 2
    while True:
        candidate = str(parent / f"{stem}-{counter}{suffix}")
        if candidate not in used_paths:
            used_paths.add(candidate)
            return candidate
        counter += 1


def static_upload_url(upload_id: str, relative_path: str) -> str:
    return "/static/uploads/" + "/".join(
        quote(part)
        for part in ("project-context", upload_id, *PurePosixPath(relative_path).parts)
    )


def skip_upload(skipped: list[dict[str, str]], filename: str, reason: str) -> None:
    skipped.append({"filename": filename or "file", "reason": reason})


class ProjectContextUploadError(Exception):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


async def save_project_context_uploads(
    files: list[Any],
    relative_paths: list[str],
    *,
    upload_dir: Path,
) -> dict[str, Any]:
    """Store browser-selected resources as backend-readable Project Context files."""

    if not files:
        raise ProjectContextUploadError(400, "At least one file is required")

    upload_id = uuid.uuid4().hex
    upload_root = upload_dir / "project-context" / upload_id
    saved: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    used_relative_paths: set[str] = set()
    total_size = 0

    for index, upload in enumerate(files):
        original_name = upload.filename or f"file-{index + 1}"
        if len(saved) >= PROJECT_CONTEXT_UPLOAD_MAX_FILES:
            skip_upload(skipped, original_name, f"Only the first {PROJECT_CONTEXT_UPLOAD_MAX_FILES} files are attached")
            continue
        relative_hint = relative_paths[index] if index < len(relative_paths) else original_name
        relative_path = safe_upload_relative_path(relative_hint, original_name)
        data = await upload.read(PROJECT_CONTEXT_UPLOAD_MAX_FILE_SIZE + 1)
        if len(data) > PROJECT_CONTEXT_UPLOAD_MAX_FILE_SIZE:
            skip_upload(
                skipped,
                original_name,
                f"File is larger than {PROJECT_CONTEXT_UPLOAD_MAX_FILE_SIZE // 1_000_000} MB",
            )
            continue
        if total_size + len(data) > PROJECT_CONTEXT_UPLOAD_MAX_TOTAL_SIZE:
            skip_upload(
                skipped,
                original_name,
                f"Project Context upload is capped at {PROJECT_CONTEXT_UPLOAD_MAX_TOTAL_SIZE // 1_000_000} MB",
            )
            continue

        relative_path = unique_upload_relative_path(relative_path, used_relative_paths)
        target = upload_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        total_size += len(data)
        saved.append({
            "upload_id": upload_id,
            "filename": Path(relative_path).name,
            "relative_path": relative_path,
            "storage_path": str(target),
            "uri": static_upload_url(upload_id, relative_path),
            "mime": upload.content_type,
            "size": len(data),
        })

    if not saved:
        raise ProjectContextUploadError(
            422,
            {
                "error": "No Project Context files could be uploaded",
                "skipped_files": skipped[:20],
            },
        )

    return {
        "upload_id": upload_id,
        "files": saved,
        "skipped_files": skipped[:100],
        "total_size": total_size,
    }
