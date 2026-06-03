"""Slack upload payload normalization."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import re


MAX_SLACK_IMAGE_BYTES = 10 * 1024 * 1024
_IMAGE_DATA_URL_RE = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.+)$", re.IGNORECASE | re.DOTALL)
_IMAGE_MIME_EXTENSIONS = {
    "image/avif": "avif",
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/svg+xml": "svg",
    "image/webp": "webp",
}


@dataclass(frozen=True)
class SlackImageUpload:
    file_bytes: bytes
    content_type: str
    filename: str
    title: str
    alt_txt: str | None


def _decode_image_data_url(value: str) -> tuple[bytes, str]:
    match = _IMAGE_DATA_URL_RE.match(value)
    if not match:
        raise ValueError("image_data must be a base64 data:image URL")
    mime = match.group(1).lower()
    if mime not in _IMAGE_MIME_EXTENSIONS:
        raise ValueError("image_data must use png, jpg, gif, webp, avif, or svg")
    try:
        data = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image_data contains invalid base64") from exc
    if not data:
        raise ValueError("image_data is empty")
    if len(data) > MAX_SLACK_IMAGE_BYTES:
        raise ValueError(f"image_data is too large for Slack upload ({len(data)} bytes)")
    return data, mime


def _slack_image_filename(filename: str | None, content_type: str) -> str:
    extension = _IMAGE_MIME_EXTENSIONS.get(content_type, "png")
    raw = re.sub(r"[^A-Za-z0-9._-]+", "-", str(filename or "").strip()).strip(".-")
    if not raw:
        raw = f"illo-graph.{extension}"
    if "." not in raw.rsplit("/", 1)[-1]:
        raw = f"{raw}.{extension}"
    return raw[:120]


def slack_image_upload_from_data_url(
    image_data: str | None,
    *,
    filename: str | None = None,
    title: str | None = None,
    alt_txt: str | None = None,
) -> SlackImageUpload | None:
    raw = str(image_data or "").strip()
    if not raw:
        return None

    file_bytes, content_type = _decode_image_data_url(raw)
    normalized_filename = _slack_image_filename(filename or title, content_type)
    normalized_title = str(title or "").strip() or normalized_filename
    normalized_alt = str(alt_txt or title or "").strip() or None
    return SlackImageUpload(
        file_bytes=file_bytes,
        content_type=content_type,
        filename=normalized_filename,
        title=normalized_title,
        alt_txt=normalized_alt,
    )
