"""Keep provider messages out of events and logs."""

import re

import httpx

from brain.models import RunError


def identifier(value) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        return value
    return "unknown"


def provider_error(error, status: int | None = None) -> RunError:
    error = error if isinstance(error, dict) else {}
    code, kind = identifier(error.get("code")), identifier(error.get("type"))
    category = "auth" if status in (401, 403) else "runtime"
    if code in ("rate_limit_exceeded", "insufficient_quota"):
        category = "limit"
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
