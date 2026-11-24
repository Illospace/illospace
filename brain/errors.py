"""Error categories. Provider messages never enter an event or a log."""

import re
from typing import Any

import httpx

from brain.models import ErrorCategory


class RunError(Exception):
    def __init__(self, category: ErrorCategory, message: str, **details: Any) -> None:
        super().__init__(message)
        self.category = category
        self.details = details


def identifier(value) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        return value
    return "unknown"


def provider_error(error, status: int | None = None, *, category: ErrorCategory | None = None) -> RunError:
    error = error if isinstance(error, dict) else {}
    code, kind = identifier(error.get("code")), identifier(error.get("type"))
    if category is None:
        category = {401: "auth", 403: "auth", 500: "transient", 503: "transient"}.get(
            status, "runtime")
        if code in ("rate_limit_exceeded", "insufficient_quota"):
            category = "limit"
        elif status is None and code in ("server_error", "service_unavailable"):
            category = "transient"
    details = {"code": code, "provider_type": kind}
    if status is not None:
        details["http_status"] = status
    return RunError(category, f"Provider error: HTTP {status or 'stream'} {code} {kind}", **details)


async def error_body(response: httpx.Response) -> dict:
    await response.aread()
    try:
        body = response.json()
        error = body.get("error", {}) if isinstance(body, dict) else {}
        return error if isinstance(error, dict) else {}
    except ValueError:
        return {}


def error_category(error: BaseException) -> str:
    if isinstance(error, RunError):
        return error.category
    if isinstance(error, BaseExceptionGroup):
        categories = [error_category(child) for child in error.exceptions]
        return next((c for c in ("auth", "timeout", "limit", "config", "transient", "runtime")
                     if c in categories), "internal")
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
        return "auth"
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    return "internal"


def failure_event(error: BaseException) -> dict:
    """The error event of a failed run. Only a RunError gives its own message."""
    if isinstance(error, RunError):
        return {"type": "error", "category": error.category, "message": str(error), **error.details}
    if isinstance(error, BaseExceptionGroup):
        failures = [failure_event(child) for child in error.exceptions]
        return next((item for item in failures if item.get("code") is not None), failures[0])
    return {"type": "error", "category": error_category(error), "message": "The brain runtime failed"}
