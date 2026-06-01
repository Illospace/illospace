"""Canonical Thread URL helpers and parsers."""
from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

THREAD_ROUTE_PREFIX = "/threads"
LEGACY_CORTEX_ROUTE = "/cortex"
LOCAL_APP_BASE_URL = "http://localhost:8080"

_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_URL_CANDIDATE_RE = re.compile(
    r"https?://[^\s<>'\"\]\)]+|/(?:threads/|cortex\?)[^\s<>'\"\]\)]+",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ".,;:!?"


def public_app_base_url() -> str:
    raw = os.getenv("ILLO_PUBLIC_URL") or os.getenv("ILLO_DASHBOARD_URL") or LOCAL_APP_BASE_URL
    parts = urlsplit(str(raw).strip() or LOCAL_APP_BASE_URL)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return LOCAL_APP_BASE_URL
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")


def thread_route_for_id(thread_id: Any) -> str:
    return f"{THREAD_ROUTE_PREFIX}/{quote(str(thread_id), safe='')}"


def legacy_thread_route_for_id(thread_id: Any) -> str:
    return f"{LEGACY_CORTEX_ROUTE}?idea={quote(str(thread_id), safe='')}"


def thread_url_for_route(route: str) -> str:
    value = str(route or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    if not value.startswith("/"):
        value = f"/{value}"
    return f"{public_app_base_url()}{value}"


def thread_url_for_id(thread_id: Any) -> str:
    return thread_url_for_route(thread_route_for_id(thread_id))


def thread_link_payload(thread_id: Any) -> dict[str, str]:
    route = thread_route_for_id(thread_id)
    url = thread_url_for_route(route)
    return {
        "thread_id": str(thread_id),
        "thread_route": route,
        "thread_url": url,
        "url": url,
    }


def _clean_thread_id(value: Any) -> str | None:
    text = unquote(str(value or "")).strip().rstrip(_TRAILING_PUNCTUATION)
    if not text:
        return None
    return text if _THREAD_ID_RE.match(text) else None


def _strip_candidate(value: Any) -> str:
    cleaned = str(value or "").strip()
    while cleaned and cleaned[-1] in _TRAILING_PUNCTUATION:
        cleaned = cleaned[:-1]
    return cleaned


def thread_id_from_reference(value: Any, *, allow_raw_id: bool = False) -> str | None:
    text = _strip_candidate(value)
    if not text:
        return None
    if allow_raw_id and not any(part in text for part in ("/", "?", "#", "://")):
        return _clean_thread_id(text)

    parts = urlsplit(text)
    path = parts.path or text
    if path.startswith(f"{THREAD_ROUTE_PREFIX}/"):
        tail = path[len(THREAD_ROUTE_PREFIX) + 1:]
        return _clean_thread_id(tail.split("/", 1)[0])
    if path == LEGACY_CORTEX_ROUTE:
        return _clean_thread_id(parse_qs(parts.query).get("idea", [None])[0])
    return None


def canonicalize_thread_reference(value: Any, *, allow_raw_id: bool = False) -> dict[str, str] | None:
    thread_id = thread_id_from_reference(value, allow_raw_id=allow_raw_id)
    if not thread_id:
        return None
    payload = thread_link_payload(thread_id)
    payload["original_ref"] = _strip_candidate(value)
    return payload


def extract_thread_reference_values(text: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for match in _URL_CANDIDATE_RE.finditer(str(text or "")):
        candidate = _strip_candidate(match.group(0))
        if not candidate or candidate in seen:
            continue
        if thread_id_from_reference(candidate):
            values.append(candidate)
            seen.add(candidate)
    return values


__all__ = [
    "LEGACY_CORTEX_ROUTE",
    "THREAD_ROUTE_PREFIX",
    "canonicalize_thread_reference",
    "extract_thread_reference_values",
    "legacy_thread_route_for_id",
    "public_app_base_url",
    "thread_id_from_reference",
    "thread_link_payload",
    "thread_route_for_id",
    "thread_url_for_id",
    "thread_url_for_route",
]
