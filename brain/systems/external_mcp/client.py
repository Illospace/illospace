"""Small, bounded MCP Streamable HTTP client. No transport retries."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
from typing import Awaitable, Callable

import httpx

from .config import validate_url

PROTOCOL_VERSION = "2025-06-18"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOOL_PAGES = 20
MAX_TOOLS = 500
_sensitive_http = ContextVar("external_mcp_sensitive_http", default=False)


class _HttpLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # httpcore DEBUG includes response headers. A server can echo credentials there.
        return not _sensitive_http.get()


@contextmanager
def quiet_http_logs():
    """Keep this request's HTTP headers and bodies out of third-party DEBUG logs."""
    for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _HttpLogFilter) for item in logger.filters):
            logger.addFilter(_HttpLogFilter())
    token = _sensitive_http.set(True)
    try:
        yield
    finally:
        _sensitive_http.reset(token)


class McpError(Exception):
    kind = "protocol_error"
    outcome = "error"


class McpUnavailable(McpError):
    kind = "unavailable"
    outcome = "unavailable"


class McpTimeout(McpUnavailable):
    kind = "timeout"
    outcome = "timeout"


class McpAuthError(McpError):
    kind = "auth_failed"
    outcome = "auth_failed"


class McpProtocolError(McpError):
    pass


class McpRemoteToolError(McpError):
    kind = "remote_tool_error"


class McpMissingSecret(McpUnavailable):
    kind = "missing_secret"


AuthProvider = Callable[[bool], Awaitable[str | None]]


class McpClient:
    def __init__(self, url: str, *, timeout_seconds: float = 30, auth_provider: AuthProvider | None = None,
                 refresh_on_401: bool = False):
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.auth_provider = auth_provider
        self.refresh_on_401 = refresh_on_401
        self.session_id: str | None = None
        self.initialized = False
        self._next_id = 0
        self._http: httpx.AsyncClient | None = None
        self._sensitive_values: set[str] = set()

    async def __aenter__(self):
        self._http = httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False, trust_env=False)
        return self

    async def __aexit__(self, *_):
        if self._http:
            with quiet_http_logs():
                await self._http.aclose()
        self._sensitive_values.clear()

    def safe_message(self, value) -> str:
        message = str(value)
        for secret in sorted(self._sensitive_values, key=len, reverse=True):
            message = message.replace(secret, "[redacted]")
        return message[:1000]

    def _redact(self, value):
        if isinstance(value, str):
            for secret in sorted(self._sensitive_values, key=len, reverse=True):
                value = value.replace(secret, "[redacted]")
        elif isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        elif isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    async def _post(self, payload: dict, request_id: int | None) -> dict:
        assert self._http is not None
        for attempt in range(2):
            headers = {"Accept": "application/json, text/event-stream"}
            if self.initialized:
                headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
            if self.session_id:
                headers["Mcp-Session-Id"] = self.session_id
            if self.auth_provider:
                token = await self.auth_provider(attempt == 1)
                if token:
                    self._sensitive_values.add(token)
                    headers["Authorization"] = f"Bearer {token}"
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    await validate_url(self.url)
                    with quiet_http_logs():
                        async with self._http.stream("POST", self.url, json=payload, headers=headers) as response:
                            if response.status_code == 401 and self.refresh_on_401 and attempt == 0:
                                continue
                            if response.status_code in {401, 403}:
                                raise McpAuthError(f"MCP server returned HTTP {response.status_code}")
                            if not response.is_success:
                                raise McpUnavailable(f"MCP server returned HTTP {response.status_code}")
                            session_id = response.headers.get("Mcp-Session-Id")
                            if session_id:
                                if len(session_id) > 1024 or not session_id.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in session_id):
                                    raise McpProtocolError("Invalid MCP session id")
                                self.session_id = session_id
                            if request_id is None:
                                return {}
                            if "text/event-stream" in response.headers.get("content-type", "").lower():
                                return await self._read_sse(response, request_id)
                            raw = bytearray()
                            async for chunk in response.aiter_bytes():
                                raw.extend(chunk)
                                if len(raw) > MAX_RESPONSE_BYTES:
                                    raise McpProtocolError("MCP response is too large")
                            return self._matching_response(json.loads(raw), request_id)
            except (TimeoutError, httpx.TimeoutException):
                raise McpTimeout(f"MCP request timed out after {self.timeout_seconds:g} s") from None
            except httpx.HTTPError:
                raise McpUnavailable("MCP server is unavailable") from None
            except (ValueError, UnicodeError):
                raise McpProtocolError("Invalid MCP URL or JSON response") from None
        raise McpAuthError("MCP authentication failed")

    async def _read_sse(self, response: httpx.Response, request_id: int) -> dict:
        # Parse bytes incrementally so a single unterminated SSE line is also bounded.
        buffer = ""
        data: list[str] = []
        size = 0
        async for chunk in response.aiter_text():
            size += len(chunk.encode("utf-8"))
            if size > MAX_RESPONSE_BYTES:
                raise McpProtocolError("MCP response is too large")
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.rstrip("\r")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
                elif not line and data:
                    value = json.loads("\n".join(data))
                    data = []
                    if isinstance(value, dict) and value.get("id") == request_id:
                        return self._matching_response(value, request_id)
        raise McpProtocolError("MCP stream ended without the matching response")

    def _matching_response(self, value, request_id: int) -> dict:
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0" or value.get("id") != request_id:
            raise McpProtocolError("MCP response id or JSON-RPC version does not match")
        if "error" in value:
            error = value["error"]
            message = error.get("message", "Remote MCP error") if isinstance(error, dict) else "Remote MCP error"
            raise McpRemoteToolError(self.safe_message(message))
        result = value.get("result")
        if not isinstance(result, dict):
            raise McpProtocolError("MCP result must be an object")
        return self._redact(result)

    async def request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        return await self._post({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}, self._next_id)

    async def initialize(self) -> None:
        result = await self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                                    "clientInfo": {"name": "illo", "version": "1"}})
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise McpProtocolError("Unsupported MCP protocol version")
        self.initialized = True
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)

    async def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        seen = set()
        for _ in range(MAX_TOOL_PAGES):
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools")
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise McpProtocolError("MCP tools must be a list of objects")
            tools.extend(page)
            if len(tools) > MAX_TOOLS:
                raise McpProtocolError("MCP tool count exceeds the limit")
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            if not isinstance(cursor, str) or cursor in seen:
                raise McpProtocolError("Invalid MCP tool cursor")
            seen.add(cursor)
        raise McpProtocolError("MCP tool page count exceeds the limit")

    async def call_tool(self, name: str, arguments: dict) -> dict:
        result = await self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            content = result.get("content", [])
            message = "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("type") == "text") if isinstance(content, list) else "Remote tool failed"
            raise McpRemoteToolError(self.safe_message(message or "Remote tool failed"))
        return result
