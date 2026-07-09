"""Deterministic model fallback policy for limited-availability models."""

from __future__ import annotations

import json
from typing import Any


_MODEL_FALLBACKS = {
    "openai/gpt-5.6": "openai/gpt-5.5",
    "openai/gpt-5.6-sol": "openai/gpt-5.5",
}
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


def _canonical_model(model: str | None) -> str:
    value = str(model or "").strip().lower().replace(":", "/", 1)
    return value if "/" in value else f"openai/{value}"


def fallback_model_for(model: str | None) -> str | None:
    """Return the next configured model for a limited-availability model."""
    return _MODEL_FALLBACKS.get(_canonical_model(model))


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


def is_model_unavailable_error(exc: Any) -> bool:
    """Classify entitlement/model-catalog failures without hiding auth errors."""
    if _status_code(exc) not in {400, 403, 404}:
        return False
    text = _error_text(exc)
    return any(term in text for term in _UNAVAILABLE_TERMS)


def is_missing_required_model_auth(exc: Any) -> bool:
    """Return true when the preferred subscription route is simply absent."""
    return str(exc).startswith("No OpenAI auth found.")


__all__ = [
    "fallback_model_for",
    "is_missing_required_model_auth",
    "is_model_unavailable_error",
]
