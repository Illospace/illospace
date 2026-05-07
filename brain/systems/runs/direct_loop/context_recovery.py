"""Context-overflow classification and recovery helpers."""

from __future__ import annotations

import re
from typing import Any

_CONTEXT_OVERFLOW_PATTERNS = (
    re.compile(r"context[_ ]?length[_ ]?exceeded", re.IGNORECASE),
    re.compile(r"maximum context length", re.IGNORECASE),
    re.compile(r"context window", re.IGNORECASE),
    re.compile(r"too many tokens", re.IGNORECASE),
    re.compile(r"input is too long", re.IGNORECASE),
    re.compile(r"request too large", re.IGNORECASE),
    re.compile(r"prompt is too long", re.IGNORECASE),
    re.compile(r"token limit", re.IGNORECASE),
    re.compile(r"exceeds? the model", re.IGNORECASE),
)


def _stringify_error_part(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return repr(value)


def context_overflow_error_text(exc: Exception) -> str:
    parts = [
        exc.__class__.__name__,
        _stringify_error_part(exc),
    ]
    for attr in ("code", "type", "param", "body"):
        value = getattr(exc, attr, None)
        if value is not None:
            parts.append(_stringify_error_part(value))
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("text", "content", "reason"):
            value = getattr(response, attr, None)
            if value is not None:
                parts.append(_stringify_error_part(value))
        try:
            headers = getattr(response, "headers", None)
            if headers:
                parts.append(_stringify_error_part(dict(headers)))
        except Exception:
            pass
    return " | ".join(part for part in parts if part)


def is_context_overflow_error(exc: Exception, *, provider_name: str | None = None) -> bool:
    """Return True when an API exception looks like a context-window rejection."""
    text = context_overflow_error_text(exc)
    if not text:
        return False
    return any(pattern.search(text) for pattern in _CONTEXT_OVERFLOW_PATTERNS)


def context_overflow_payload(exc: Exception, *, provider_name: str | None = None) -> dict[str, Any]:
    text = context_overflow_error_text(exc)
    response = getattr(exc, "response", None)
    headers = {}
    if response is not None:
        try:
            headers = dict(getattr(response, "headers", {}) or {})
        except Exception:
            headers = {}
    return {
        "provider": provider_name,
        "error_type": exc.__class__.__name__,
        "status_code": getattr(exc, "status_code", None),
        "request_id": headers.get("x-request-id") or headers.get("request-id"),
        "message": text[:1_000],
    }

