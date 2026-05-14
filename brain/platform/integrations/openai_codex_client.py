"""Thin client for the ChatGPT/Codex OpenAI Responses backend."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterator

import httpx

from brain.platform.async_io import sync_http_client

from brain.platform.integrations.openai_cache import normalize_openai_request_kwargs

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
logger = logging.getLogger("brain.platform.integrations.openai_codex_client")


class OpenAICodexError(RuntimeError):
    """Base error for ChatGPT/Codex backend failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.headers = headers or {}


class OpenAICodexRetryableError(OpenAICodexError):
    """Retryable transport/backend failure."""


def _extract_error_message(body: str) -> str:
    if not body:
        return ""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body[:400]

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail", "code"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for key in ("message", "detail", "error"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return body[:400]


def _build_error(response: httpx.Response) -> OpenAICodexError:
    try:
        body = response.text
    except Exception:
        body = ""
    message = _extract_error_message(body) or f"OpenAI Codex request failed ({response.status_code})"
    error_cls = (
        OpenAICodexRetryableError
        if response.status_code in (408, 409, 429) or response.status_code >= 500
        else OpenAICodexError
    )
    return error_cls(
        message,
        status_code=response.status_code,
        response_body=body,
        headers=dict(response.headers),
    )


class _ResponsesEventStream:
    """SSE iterator returned by `responses.create(..., stream=True)`."""

    def __init__(self, client: httpx.Client, payload: dict[str, Any], headers: dict[str, str]):
        self._stream_cm = client.stream(
            "POST",
            "/responses",
            json=payload,
            headers=headers,
        )
        self._response = self._stream_cm.__enter__()
        if self._response.status_code >= 400:
            try:
                self._response.read()
                raise _build_error(self._response)
            finally:
                self.close()

    def __iter__(self) -> Iterator[dict[str, Any]]:
        event_name: str | None = None
        data_lines: list[str] = []
        try:
            for raw_line in self._response.iter_lines():
                line = raw_line.strip()
                if not line:
                    if data_lines:
                        parsed = self._parse_event(event_name, "\n".join(data_lines))
                        if parsed is not None:
                            yield parsed
                    event_name = None
                    data_lines.clear()
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip() or None
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                parsed = self._parse_event(event_name, "\n".join(data_lines))
                if parsed is not None:
                    yield parsed
        finally:
            self.close()

    def _parse_event(self, event_name: str | None, data: str) -> dict[str, Any] | None:
        if not data or data == "[DONE]":
            return None
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("OpenAI Codex SSE received non-JSON event: event=%s data=%s", event_name, data[:500])
            return {"type": event_name or "message", "data": data}
        if isinstance(payload, dict):
            if event_name and "type" not in payload:
                payload["type"] = event_name
            event_type = payload.get("type", event_name or "message")
            if event_type not in {
                "response.output_text.delta",
                "response.output_text.done",
                "response.text.delta",
                "response.content_part.added",
                "response.content_part.done",
                "response.output_item.added",
                "response.output_item.done",
                "response.completed",
                "response.failed",
                "response.incomplete",
                "response.created",
                "response.in_progress",
                "response.reasoning_text.delta",
                "response.reasoning_text.done",
                "response.reasoning_summary_text.delta",
                "response.reasoning_summary_text.done",
                "response.reasoning_summary_part.added",
                "response.reasoning_summary_part.done",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
                "error",
            }:
                logger.warning(
                    "OpenAI Codex SSE unrecognized event type=%s payload=%s",
                    event_type,
                    json.dumps(payload, default=str)[:1200],
                )
            return payload
        return {"type": event_name or "message", "data": payload}

    def close(self) -> None:
        if self._stream_cm is not None:
            self._stream_cm.__exit__(None, None, None)
            self._stream_cm = None


class _ResponsesAPI:
    def __init__(self, client: "OpenAICodexClient"):
        self._client = client

    def create(self, **kwargs):
        return self._client._create_response(kwargs)


class OpenAICodexClient:
    """Small client shim that mimics the OpenAI SDK `responses` surface."""

    def __init__(
        self,
        access_token: str,
        account_id: str,
        *,
        base_url: str | None = None,
        originator: str = "illo-brain",
        timeout: float = 300.0,
    ):
        resolved_base_url = (
            base_url
            or os.environ.get("OPENAI_CODEX_BASE_URL")
            or DEFAULT_CODEX_BASE_URL
        ).rstrip("/")
        self._client = sync_http_client(
            base_url=resolved_base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "chatgpt-account-id": account_id,
                "originator": originator,
            },
        )
        self.responses = _ResponsesAPI(self)

    def close(self) -> None:
        self._client.close()

    def list_models(self, *, client_version: str = "illo-brain") -> dict[str, Any]:
        response = self._client.get(
            "/models",
            params={"client_version": client_version},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            raise _build_error(response)
        data = response.json()
        if not isinstance(data, dict):
            raise OpenAICodexError("OpenAI Codex models response was not a JSON object")
        return data

    def _create_response(self, kwargs: dict[str, Any]):
        payload = normalize_openai_request_kwargs(kwargs)
        stream = bool(payload.pop("stream", False))
        extra_headers = dict(payload.pop("extra_headers", {}) or {})
        # ChatGPT/Codex backend accepts a Responses-like shape but rejects
        # some OpenAI API-only fields like max_output_tokens, while still
        # requiring store=false to be sent explicitly.
        payload.pop("max_output_tokens", None)
        payload["store"] = False
        payload.setdefault("include", ["reasoning.encrypted_content"])
        headers = {"Content-Type": "application/json", **extra_headers}

        if stream:
            payload["stream"] = True
            return _ResponsesEventStream(self._client, payload, headers)

        response = self._client.post("/responses", json=payload, headers=headers)
        if response.status_code >= 400:
            raise _build_error(response)
        data = response.json()
        if not isinstance(data, dict):
            raise OpenAICodexError("OpenAI Codex response was not a JSON object")
        return data


__all__ = [
    "DEFAULT_CODEX_BASE_URL",
    "OpenAICodexClient",
    "OpenAICodexError",
    "OpenAICodexRetryableError",
]
