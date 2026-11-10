"""Small client helpers. These programs do not import Illo."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx

VERSION = "0.1.0"


class CollectorError(Exception):
    """An error whose message contains no remote response or credential."""


def read_token(path: Path) -> str:
    if path.stat().st_mode & 0o077:
        raise CollectorError("The token file must have mode 600.")
    token = path.read_text().strip()
    if not token or "\n" in token or "\r" in token:
        raise CollectorError("The token file must contain one token.")
    return token


def clean(value: Any, limit: int = 500, secrets: tuple[str, ...] = ()) -> str:
    """Omit code blocks and common credentials before text leaves the machine."""
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"```[\s\S]*?(?:```|$)", "[code omitted]", text)
    text = re.sub(r"-----BEGIN [\s\S]*?(?:-----END [^-]+-----|$)", "[redacted]", text)
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|authorization)"
        r"\s*[:=]\s*[\"']?[^\s,;\"']+",
        "[redacted]",
        text,
    )
    text = re.sub(
        r"\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{12,}|github_pat_[\w]+|AKIA[A-Z0-9]{16}"
        r"|xox[bp]-[\w-]+|AIza[\w-]{35}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b",
        "[redacted]",
        text,
    )
    text = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>]+", _clean_url, text)
    return text[:limit]


def _clean_url(match: re.Match) -> str:
    from urllib.parse import urlsplit, urlunsplit

    try:
        url = urlsplit(match.group())
        return urlunsplit((url.scheme, url.netloc.split("@")[-1], url.path, "", ""))
    except ValueError:
        return "[URL omitted]"


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".illo-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class MCPClient:
    """Use JSON or SSE replies without an MCP SDK dependency."""

    def __init__(self, url: str, token: str, client: httpx.Client | None = None):
        self.url = url
        self.token = token
        self.client = client or httpx.Client(timeout=15, follow_redirects=False, trust_env=False)
        self.session: str | None = None
        self.protocol = "2025-03-26"
        self.ready = False
        self.sequence = 0

    def rpc(self, method: str, params: dict, notification: bool = False) -> Any:
        self.sequence += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = self.sequence
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        try:
            response = self.client.post(self.url, headers=headers, json=body)
            if not response.is_success:
                if response.status_code in (401, 404):
                    self.ready = False
                    self.session = None
                raise CollectorError(f"The MCP request failed (HTTP {response.status_code}).")
            self.session = response.headers.get("mcp-session-id", self.session)
            if notification:
                return None
            if "text/event-stream" in response.headers.get("content-type", ""):
                entries = []
                for event in response.text.replace("\r\n", "\n").split("\n\n"):
                    data = "\n".join(
                        line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
                    )
                    if data:
                        entries.append(json.loads(data))
            else:
                value = response.json()
                entries = value if isinstance(value, list) else [value]
            result = next(entry for entry in entries if entry.get("id") == self.sequence)
            if result.get("error"):
                raise CollectorError("The MCP server rejected the request.")
            return result["result"]
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            StopIteration,
            TypeError,
        ) as from_error:
            raise CollectorError("The MCP reply is unavailable or invalid.") from from_error

    def call(self, name: str, arguments: dict) -> Any:
        if not self.ready:
            result = self.rpc(
                "initialize",
                {
                    "protocolVersion": self.protocol,
                    "capabilities": {},
                    "clientInfo": {"name": "illo-collector", "version": VERSION},
                },
            )
            self.protocol = result["protocolVersion"]
            self.rpc("notifications/initialized", {}, notification=True)
            self.ready = True
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise CollectorError(f"The MCP tool {name} failed.")
        if "structuredContent" in result:
            return result["structuredContent"]
        try:
            return json.loads(
                next(part["text"] for part in result["content"] if part["type"] == "text")
            )
        except (ValueError, KeyError, StopIteration, TypeError) as error:
            raise CollectorError("The MCP tool reply is invalid.") from error
