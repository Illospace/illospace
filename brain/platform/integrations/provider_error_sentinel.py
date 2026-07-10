"""Detection and safe serialization for provider errors returned as model text."""

from __future__ import annotations

import re
from typing import Any

PROVIDER_ERROR_SENTINEL_PREFIX = "upstream_provider_error:"

_PROVIDER_ERROR_KINDS = (
    "server_error",
    "overloaded_error",
    "rate_limit_error",
)
_PROVIDER_ERROR_KIND_VALUES = frozenset((*_PROVIDER_ERROR_KINDS, "provider_error"))


def provider_error_kind(
    candidate: Any,
    *,
    provider_exception: BaseException | str | None = None,
) -> str | None:
    """Classify known provider-error text without retaining its request details."""

    text = str(candidate or "").strip()
    normalized = text.lower()
    if (
        normalized.startswith("cycle run degraded: mission_contract_failed.")
        and "help.openai.com" not in normalized
    ):
        return None
    if normalized.startswith(PROVIDER_ERROR_SENTINEL_PREFIX):
        sentinel_kind = normalized.removeprefix(PROVIDER_ERROR_SENTINEL_PREFIX).strip()
        return sentinel_kind if sentinel_kind in _PROVIDER_ERROR_KIND_VALUES else "provider_error"
    for kind in _PROVIDER_ERROR_KINDS:
        if re.search(rf"\b{re.escape(kind)}\b", normalized):
            return kind
    if "help.openai.com" in normalized:
        return "provider_error"
    if text:
        return None
    if provider_exception is None:
        return None

    exception_text = str(provider_exception or "").strip().lower()
    if not exception_text:
        return None
    for kind in _PROVIDER_ERROR_KINDS:
        if re.search(rf"\b{re.escape(kind)}\b", exception_text):
            return kind
    return "provider_error"


def safe_provider_error_sentinel(kind: str | None) -> str:
    """Return a request-ID-free marker safe for internal persistence."""

    normalized_kind = str(kind or "provider_error").strip().lower()
    if normalized_kind not in _PROVIDER_ERROR_KIND_VALUES:
        normalized_kind = "provider_error"
    return f"{PROVIDER_ERROR_SENTINEL_PREFIX} {normalized_kind}"


__all__ = [
    "PROVIDER_ERROR_SENTINEL_PREFIX",
    "provider_error_kind",
    "safe_provider_error_sentinel",
]
