from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import anthropic
import httpx

logger = logging.getLogger("brain.platform.integrations.anthropic")

_OAUTH_BETA_FLAGS_FALLBACK = [
    "claude-code-20250219",
    "oauth-2025-04-20",
    "prompt-caching-2024-07-31",
    "interleaved-thinking-2025-05-14",
    "fine-grained-tool-streaming-2025-05-14",
]
_DEBUG_DIR = Path("/tmp/anthropic-debug")


@dataclass(frozen=True)
class AnthropicAuthAdapter:
    client: anthropic.Anthropic
    is_oauth: bool
    extra_headers: dict[str, str]
    token_prefix: str
    token_suffix: str


def _mask_header_value(name: str, value: str) -> str:
    if not value:
        return value
    lower = name.lower()
    if lower == "authorization":
        secret = value.removeprefix("Bearer ").strip()
        return f"Bearer {secret[:18]}...{secret[-16:]}" if len(secret) > 34 else "Bearer ***"
    if lower in {"x-api-key", "anthropic-api-key"}:
        return f"{value[:18]}...{value[-16:]}" if len(value) > 34 else "***"
    return value


def _sanitize_headers(headers: httpx.Headers) -> dict[str, str]:
    return {key: _mask_header_value(key, value) for key, value in headers.items()}


def _write_debug_payload(prefix: str, payload: dict) -> Path:
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = _DEBUG_DIR / f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def _request_debug_hook(request: httpx.Request) -> None:
    body_text = ""
    body_json = None
    try:
        if request.content:
            body_text = request.content.decode("utf-8", errors="replace")
            if request.headers.get("content-type", "").startswith("application/json"):
                body_json = json.loads(body_text)
    except Exception as exc:
        body_text = f"<unavailable: {exc}>"
    payload = {
        "method": request.method,
        "url": str(request.url),
        "headers": _sanitize_headers(request.headers),
        "json": body_json,
        "body": body_text if body_json is None else None,
    }
    path = _write_debug_payload("request", payload)
    request.extensions["illo_debug_path"] = str(path)
    logger.warning("anthropic.http_request_debug path=%s", path)


def _response_debug_hook(response: httpx.Response) -> None:
    body_preview = ""
    try:
        response.read()
        body_preview = response.text[:2000]
    except Exception as exc:
        body_preview = f"<unavailable: {exc}>"
    payload = {
        "status_code": response.status_code,
        "url": str(response.request.url),
        "headers": dict(response.headers),
        "request_id": response.headers.get("request-id") or response.headers.get("x-request-id"),
        "request_debug_path": response.request.extensions.get("illo_debug_path"),
        "body_preview": body_preview,
    }
    path = _write_debug_payload("response", payload)
    logger.warning(
        "anthropic.http_response_debug status=%s request_id=%s path=%s request_path=%s",
        response.status_code,
        payload["request_id"],
        path,
        payload["request_debug_path"],
    )


_OAUTH_USER_AGENT = "claude-cli/2.1.62"


def _oauth_request_hook(request: httpx.Request) -> None:
    """Fix headers for OAuth/setup-token transport.
    1. Force user-agent to claude-cli (API rejects non-Claude-Code user-agents)
    2. Remove x-api-key (SDK sends it even when api_key=None, and it shadows Authorization)
    """
    request.headers["user-agent"] = _OAUTH_USER_AGENT
    if "x-api-key" in request.headers:
        del request.headers["x-api-key"]


def _build_http_client(*, oauth: bool = False) -> httpx.Client:
    request_hooks = [_request_debug_hook]
    if oauth:
        request_hooks.insert(0, _oauth_request_hook)
    return anthropic.DefaultHttpxClient(
        event_hooks={"request": request_hooks, "response": [_response_debug_hook]},
    )


def get_oauth_betas() -> list[str]:
    return list(_OAUTH_BETA_FLAGS_FALLBACK)


def is_oauth_token(token: str) -> bool:
    return "sk-ant-oat" in (token or "")


def build_auth_adapter(token: str, timeout: float = 600.0) -> AnthropicAuthAdapter:
    oauth = is_oauth_token(token)
    extra_headers: dict[str, str] = {}
    if oauth:
        betas = get_oauth_betas()
        client = anthropic.Anthropic(
            api_key=None,
            auth_token=token,
            timeout=timeout,
            default_headers={"x-app": "cli"},
            http_client=_build_http_client(oauth=True),
        )
        if betas:
            extra_headers["anthropic-beta"] = ",".join(betas)
    else:
        client = anthropic.Anthropic(api_key=token, timeout=timeout, http_client=_build_http_client())
    return AnthropicAuthAdapter(
        client=client,
        is_oauth=oauth,
        extra_headers=extra_headers,
        token_prefix=token[:18] if token else "",
        token_suffix=token[-16:] if token else "",
    )


def create_message_with_token(token: str, *, timeout: float = 600.0, **kwargs):
    adapter = build_auth_adapter(token, timeout=timeout)
    extra_headers = dict(kwargs.pop("extra_headers", {}) or {})
    extra_headers.update(adapter.extra_headers)
    if extra_headers:
        kwargs["extra_headers"] = extra_headers
    return adapter.client.messages.create(**kwargs)
