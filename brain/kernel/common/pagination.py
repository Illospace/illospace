"""Opaque pagination tokens shared by bounded readers."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any


class InvalidPageToken(ValueError):
    """Raised when a pagination token is malformed or belongs to another reader."""


def encode_page_token(kind: str, position: Mapping[str, Any]) -> str:
    payload = {
        "v": 1,
        "kind": str(kind),
        "position": dict(position),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_page_token(token: str | None, *, kind: str) -> dict[str, Any]:
    if not token:
        return {}
    try:
        encoded = str(token).strip()
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidPageToken("Invalid pagination cursor") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or payload.get("kind") != kind
        or not isinstance(payload.get("position"), dict)
    ):
        raise InvalidPageToken("Invalid pagination cursor")
    return dict(payload["position"])


def page_offset(token: str | None, *, kind: str) -> int:
    position = decode_page_token(token, kind=kind)
    try:
        offset = int(position.get("offset", 0))
    except (TypeError, ValueError) as exc:
        raise InvalidPageToken("Invalid pagination cursor") from exc
    if offset < 0:
        raise InvalidPageToken("Invalid pagination cursor")
    return offset


def next_offset_token(*, kind: str, offset: int, returned: int) -> str:
    return encode_page_token(kind, {"offset": max(0, int(offset)) + max(0, int(returned))})
