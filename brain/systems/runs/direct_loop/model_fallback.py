"""Deterministic model fallback policy for limited-availability models."""

from __future__ import annotations

import json
from typing import Any

from brain.platform.model_catalog import (
    availability_fallback_for,
    canonical_catalog_model_id,
)
from brain.platform.providers.model_policy import infer_provider_from_model

_UNAVAILABLE_TERMS = (
    "model_not_found",
    "model is not available",
    "model is unavailable",
    "model does not exist",
    "model is not supported",
    "not supported when using codex",
    "limited preview",
    "not available on this account",
    "do not have access to the model",
)
_CONNECTION_ERROR_CLASS_NAMES = frozenset({"APIConnectionError", "ConnectError"})
_CONNECTION_ERROR_TERMS = (
    "connection refused",
    "connection error",
    "timed out",
)


def _canonical_model(model: str | None) -> str:
    value = str(model or "").strip().lower().replace(":", "/", 1)
    catalog_id = canonical_catalog_model_id(value)
    if catalog_id:
        return catalog_id
    return value if "/" in value else f"openai/{value}"


def fallback_model_for(model: str | None) -> str | None:
    """Return the next configured model for a limited-availability model."""
    return availability_fallback_for(_canonical_model(model))


def _error_text(exc: Any) -> str:
    values = [str(exc)]
    for attr in ("response_body", "body", "message", "detail"):
        value = getattr(exc, attr, None)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            values.append(json.dumps(value, default=str))
        else:
            values.append(str(value))
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("text", "content"):
            value = getattr(response, attr, None)
            if value:
                values.append(str(value))
    return " ".join(values).lower()


def _status_code(exc: Any) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None:
        value = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_connection_level_error(exc: Any) -> bool:
    current = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if current.__class__.__name__ in _CONNECTION_ERROR_CLASS_NAMES:
            return True
        current = getattr(current, "__cause__", None) or getattr(
            current,
            "__context__",
            None,
        )
    text = _error_text(exc)
    return any(term in text for term in _CONNECTION_ERROR_TERMS)


def is_model_unavailable_error(exc: Any, *, model: str | None = None) -> bool:
    """Classify model failures and Ollama-only connection unavailability."""
    status_code = _status_code(exc)
    if status_code in {400, 403, 404}:
        text = _error_text(exc)
        return any(term in text for term in _UNAVAILABLE_TERMS)
    if status_code is not None or not model:
        return False
    return (
        infer_provider_from_model(model) == "ollama"
        and _is_connection_level_error(exc)
    )


def is_missing_required_model_auth(exc: Any) -> bool:
    """Return true when the preferred subscription route is simply absent."""
    return str(exc).startswith("No OpenAI auth found.")


__all__ = [
    "fallback_model_for",
    "is_missing_required_model_auth",
    "is_model_unavailable_error",
]
