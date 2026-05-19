"""Static upload URL and lightweight preview helpers for Cortex attachments."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree


STATIC_UPLOAD_PREFIX = "/static/uploads/"
MAX_PREVIEW_CHARS = 24_000
MAX_TEXT_FILE_BYTES = 1_000_000
MAX_SHEET_ROWS = 40
MAX_SHEET_COLUMNS = 16
MAX_SHEETS = 4

_TEXT_EXTENSIONS = {"csv", "json", "log", "md", "text", "tsv", "txt", "xml", "yaml", "yml"}
_HTML_EXTENSIONS = {"html", "htm"}
_IMAGE_EXTENSIONS = {"avif", "gif", "jpeg", "jpg", "png", "svg", "webp"}
_VIDEO_EXTENSIONS = {"m4v", "mov", "mp4", "webm"}
_DOCUMENT_EXTENSIONS = {"doc", "docx", "key", "numbers", "odt", "pages", "ppt", "pptx", "rtf", "xls", "xlsx"}
_ARCHIVE_EXTENSIONS = {"7z", "rar", "tar", "zip"}


def static_upload_url_for(*parts: str) -> str:
    """Build a quoted /static/uploads URL from path parts."""

    clean_parts = []
    for raw_part in parts:
        for part in PurePosixPath(str(raw_part or "").replace("\\", "/")).parts:
            if part in {"", ".", ".."}:
                continue
            clean_parts.append(quote(part))
    return STATIC_UPLOAD_PREFIX + "/".join(clean_parts)


def normalize_static_upload_url(value: str) -> str:
    """Return a canonical /static/uploads path from a relative or absolute URL."""

    raw_value = (value or "").strip()
    if not raw_value:
        raise ValueError("upload URL is required")

    parsed = urlsplit(raw_value)
    path = parsed.path if parsed.scheme or parsed.netloc else raw_value.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith(STATIC_UPLOAD_PREFIX):
        raise ValueError("Only Cortex uploads under /static/uploads are supported")

    relative = unquote(path[len(STATIC_UPLOAD_PREFIX):]).strip("/")
    if not relative:
        raise ValueError("upload URL is missing a file path")

    return static_upload_url_for(*PurePosixPath(relative).parts)


def resolve_static_upload_path(value: str, *, upload_dir: Path) -> Path:
    """Resolve a /static/uploads URL to a local file under upload_dir."""

    normalized = normalize_static_upload_url(value)
    relative = unquote(normalized[len(STATIC_UPLOAD_PREFIX):]).strip("/")
    candidate = (upload_dir / relative).resolve()
    root = upload_dir.resolve()
    try:
        within_root = candidate == root or candidate.is_relative_to(root)
    except AttributeError:
        within_root = str(candidate).startswith(str(root) + os.sep) or candidate == root
    if not within_root:
        raise ValueError("upload path escapes upload root")
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError(f"Upload not found: {value}")
    return candidate


def public_base_url(request_base_url: str | None = None) -> str:
    """Resolve the public app origin, preferring explicit deployment config."""

    raw = (
        os.environ.get("ILLO_PUBLIC_URL")
        or os.environ.get("ILLO_DASHBOARD_URL")
        or request_base_url
        or ""
    ).strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def public_static_upload_url(value: str, *, request_base_url: str | None = None) -> str:
    """Return an absolute public URL when a public base URL is available."""

    normalized = normalize_static_upload_url(value)
    base = public_base_url(request_base_url)
    return f"{base}{normalized}" if base else normalized


def build_upload_preview(
    value: str,
    *,
    upload_dir: Path,
    request_base_url: str | None = None,
) -> dict[str, Any]:
    """Build a lightweight preview payload for an uploaded file."""

    path = resolve_static_upload_path(value, upload_dir=upload_dir)
    relative_url = normalize_static_upload_url(value)
    extension = path.suffix.lower().lstrip(".")
    content_type = mimetypes.guess_type(path.name)[0] or _content_type_for_extension(extension)
    size = path.stat().st_size
    kind = _preview_kind(extension, content_type)
    payload: dict[str, Any] = {
        "url": relative_url,
        "download_url": public_static_upload_url(relative_url, request_base_url=request_base_url),
        "filename": path.name,
        "extension": extension,
        "content_type": content_type,
        "size": size,
        "kind": kind,
        "preview_mode": "unsupported",
        "text": "",
        "truncated": False,
    }

    if kind in {"image", "video", "pdf"}:
        payload["preview_mode"] = "embed"
        return payload

    if extension in _HTML_EXTENSIONS:
        text, truncated = _read_text_preview(path)
        payload.update({"preview_mode": "html", "text": text, "truncated": truncated})
        return payload

    if extension in _TEXT_EXTENSIONS:
        text, truncated = _read_text_preview(path)
        payload.update({"preview_mode": "text", "text": text, "truncated": truncated})
        return payload

    if extension == "rtf":
        text, truncated = _truncate_text(_strip_rtf(path.read_text(encoding="utf-8", errors="replace")))
        payload.update({"preview_mode": "text", "text": text, "truncated": truncated})
        return payload

    if extension == "docx":
        text, truncated = _extract_docx_text(path)
        payload.update({"preview_mode": "text", "text": text, "truncated": truncated})
        return payload

    if extension == "pptx":
        slides = _extract_pptx_slides(path)
        text, truncated = _truncate_text("\n\n".join(slide["text"] for slide in slides if slide.get("text")))
        payload.update({
            "preview_mode": "slides",
            "text": text,
            "slides": slides,
            "truncated": truncated,
        })
        return payload

    if extension == "xlsx":
        sheets = _extract_xlsx_sheets(path)
        text, truncated = _truncate_text(_sheets_to_text(sheets))
        payload.update({
            "preview_mode": "sheet",
            "text": text,
            "sheets": sheets,
            "truncated": truncated,
        })
        return payload

    if extension in _ARCHIVE_EXTENSIONS:
        payload["kind"] = "archive"
    return payload


def _content_type_for_extension(extension: str) -> str:
    return {
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "htm": "text/html",
        "html": "text/html",
        "pdf": "application/pdf",
        "ppt": "application/vnd.ms-powerpoint",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "rtf": "application/rtf",
        "xls": "application/vnd.ms-excel",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(extension, "application/octet-stream")


def _preview_kind(extension: str, content_type: str) -> str:
    if content_type.startswith("image/") or extension in _IMAGE_EXTENSIONS:
        return "image"
    if content_type.startswith("video/") or extension in _VIDEO_EXTENSIONS:
        return "video"
    if content_type == "application/pdf" or extension == "pdf":
        return "pdf"
    if content_type == "text/html" or extension in _HTML_EXTENSIONS:
        return "html"
    if extension in _TEXT_EXTENSIONS:
        return "text"
    if extension in _DOCUMENT_EXTENSIONS:
        return "document"
    if extension in _ARCHIVE_EXTENSIONS:
        return "archive"
    return "file"


def _read_text_preview(path: Path) -> tuple[str, bool]:
    if path.stat().st_size > MAX_TEXT_FILE_BYTES:
        data = path.read_bytes()[:MAX_TEXT_FILE_BYTES]
        text = data.decode("utf-8", errors="replace")
        preview, _ = _truncate_text(text)
        return preview, True
    return _truncate_text(path.read_text(encoding="utf-8", errors="replace"))


def _truncate_text(text: str, limit: int = MAX_PREVIEW_CHARS) -> tuple[str, bool]:
    clean = (text or "").replace("\x00", "").strip()
    if len(clean) <= limit:
        return clean, False
    return clean[:limit].rstrip(), True


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text_nodes(root: ElementTree.Element) -> list[str]:
    return [node.text or "" for node in root.iter() if _local_name(node.tag) == "t" and node.text]


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        name = _local_name(node.tag)
        if name == "t" and node.text:
            parts.append(node.text)
        elif name == "tab":
            parts.append("\t")
        elif name in {"br", "cr"}:
            parts.append("\n")
    return "".join(parts).strip()


def _extract_docx_text(path: Path) -> tuple[str, bool]:
    paragraphs: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = [
            "word/document.xml",
            *sorted(name for name in archive.namelist() if re.match(r"word/(header|footer)\d+\.xml$", name)),
        ]
        for name in names:
            try:
                root = ElementTree.fromstring(archive.read(name))
            except (KeyError, ElementTree.ParseError):
                continue
            for paragraph in root.iter():
                if _local_name(paragraph.tag) != "p":
                    continue
                text = _paragraph_text(paragraph)
                if text:
                    paragraphs.append(text)
    return _truncate_text("\n\n".join(paragraphs))


def _extract_pptx_slides(path: Path) -> list[dict[str, Any]]:
    slides: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        slide_names = sorted(
            (name for name in archive.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", name)),
            key=lambda name: int(re.search(r"slide(\d+)\.xml$", name).group(1) or 0),
        )
        for index, name in enumerate(slide_names, 1):
            try:
                root = ElementTree.fromstring(archive.read(name))
            except ElementTree.ParseError:
                continue
            text = "\n".join(chunk.strip() for chunk in _xml_text_nodes(root) if chunk.strip())
            slides.append({"index": index, "title": _first_line(text) or f"Slide {index}", "text": text})
    return slides


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        clean = line.strip()
        if clean:
            return clean[:120]
    return ""


def _extract_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except (KeyError, ElementTree.ParseError):
        return []
    strings: list[str] = []
    for item in root:
        if _local_name(item.tag) != "si":
            continue
        strings.append("".join(_xml_text_nodes(item)))
    return strings


def _extract_xlsx_sheets(path: Path) -> list[dict[str, Any]]:
    sheets: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        shared_strings = _extract_shared_strings(archive)
        sheet_names = sorted(
            (name for name in archive.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", name)),
            key=lambda name: int(re.search(r"sheet(\d+)\.xml$", name).group(1) or 0),
        )
        for sheet_index, name in enumerate(sheet_names[:MAX_SHEETS], 1):
            try:
                root = ElementTree.fromstring(archive.read(name))
            except ElementTree.ParseError:
                continue
            rows: list[list[str]] = []
            for row in (node for node in root.iter() if _local_name(node.tag) == "row"):
                values: list[str] = []
                for cell in [node for node in row if _local_name(node.tag) == "c"][:MAX_SHEET_COLUMNS]:
                    values.append(_xlsx_cell_value(cell, shared_strings))
                if any(value.strip() for value in values):
                    rows.append(values)
                if len(rows) >= MAX_SHEET_ROWS:
                    break
            sheets.append({"index": sheet_index, "name": f"Sheet {sheet_index}", "rows": rows})
    return sheets


def _xlsx_cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(_xml_text_nodes(cell)).strip()
    value_node = next((node for node in cell if _local_name(node.tag) == "v"), None)
    raw_value = (value_node.text if value_node is not None else "") or ""
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)].strip()
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "TRUE" if raw_value == "1" else "FALSE"
    return raw_value.strip()


def _sheets_to_text(sheets: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for sheet in sheets:
        lines = ["\t".join(str(value) for value in row).rstrip() for row in sheet.get("rows", [])]
        if lines:
            sections.append(f"{sheet.get('name') or 'Sheet'}\n" + "\n".join(lines))
    return "\n\n".join(sections)


def _strip_rtf(text: str) -> str:
    text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    text = re.sub(r"\\par[d]?", "\n", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "").replace("\\", "")
    return re.sub(r"\n{3,}", "\n\n", text)


def preview_payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
