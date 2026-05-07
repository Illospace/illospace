"""Shared OpenAI request-safety helpers."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Any

OPENAI_PROMPT_CACHE_KEY_MAX_LEN = 64
OPENAI_PROMPT_CACHE_KEY_DIGEST_LEN = 24


def normalize_openai_prompt_cache_key(cache_key: str) -> str:
    """Clamp prompt_cache_key to OpenAI's 64-character limit deterministically."""
    if len(cache_key) <= OPENAI_PROMPT_CACHE_KEY_MAX_LEN:
        return cache_key

    digest = sha256(cache_key.encode("utf-8")).hexdigest()[:OPENAI_PROMPT_CACHE_KEY_DIGEST_LEN]
    head_len = OPENAI_PROMPT_CACHE_KEY_MAX_LEN - len(digest) - 1
    return f"{cache_key[:head_len]}:{digest}"


def normalize_openai_session_id(session_id: str) -> str:
    """Clamp ChatGPT/Codex session identifiers to the same backend-safe length."""
    return normalize_openai_prompt_cache_key(session_id)


def normalize_openai_extra_headers(extra_headers: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize OpenAI extra headers that have backend-specific limits."""
    headers = dict(extra_headers or {})
    session_id = headers.get("session_id")
    if isinstance(session_id, str) and session_id:
        headers["session_id"] = normalize_openai_session_id(session_id)
    return headers


def build_openai_extra_headers(
    base_headers: Mapping[str, Any] | None = None,
    *,
    auth_mode: str | None = None,
    session_id: str | None = None,
    extra_headers: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge OpenAI headers and inject a safe ChatGPT/Codex session header when needed."""
    headers = dict(base_headers or {})
    if extra_headers:
        headers.update(extra_headers)
    if auth_mode == "chatgpt" and session_id:
        headers.setdefault("session_id", normalize_openai_session_id(session_id))
    return normalize_openai_extra_headers(headers)


def normalize_openai_request_kwargs(kwargs: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize OpenAI request kwargs before they reach any SDK or HTTP client."""
    normalized = dict(kwargs or {})

    cache_key = normalized.get("prompt_cache_key")
    if isinstance(cache_key, str) and cache_key:
        normalized["prompt_cache_key"] = normalize_openai_prompt_cache_key(cache_key)

    if "extra_headers" in normalized:
        normalized["extra_headers"] = normalize_openai_extra_headers(normalized.get("extra_headers"))

    return normalized
