#!/usr/bin/env python3
"""MCP server for personal-agent collaboration with Illo."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


logger = logging.getLogger("illo_personal_agent_mcp")
DEFAULT_TIMEOUT_SECONDS = 60.0
TOOL_SUBMIT = "illo_submit"
TOOL_READ = "illo_read"
TOOL_ACT = "illo_act"
TOOL_GET_RESULT = "illo_get_result"
TOOL_NAMES = (TOOL_SUBMIT, TOOL_READ, TOOL_ACT, TOOL_GET_RESULT)


class IlloBridgeError(RuntimeError):
    """Raised when the Illo bridge API cannot satisfy a tool call."""


@dataclass(frozen=True)
class IlloBridgeConfig:
    base_url: str
    token: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "IlloBridgeConfig":
        base_url = os.environ.get("ILLO_BASE_URL", "").strip()
        token = os.environ.get("ILLO_BRIDGE_TOKEN", "").strip()
        timeout = float(os.environ.get("ILLO_MCP_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS)))
        if not base_url:
            raise IlloBridgeError("ILLO_BASE_URL is required")
        if not token:
            raise IlloBridgeError("ILLO_BRIDGE_TOKEN is required")
        return cls(base_url=base_url, token=token, timeout_seconds=timeout)


def _json_request(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise IlloBridgeError(f"{method} {url} failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise IlloBridgeError(f"{method} {url} failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise IlloBridgeError(f"{method} {url} returned invalid JSON") from exc


def _clean_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def _clean_string_list(values: list[str] | None) -> list[str]:
    return [str(value).strip() for value in values or [] if str(value or "").strip()]


def _drop_empty(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _decode_mcp_tool_result(response: dict[str, Any]) -> dict[str, Any]:
    if "error" in response:
        raise IlloBridgeError(str(response["error"]))
    result = response.get("result")
    if not isinstance(result, dict):
        return response
    if result.get("isError"):
        content = result.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            raise IlloBridgeError(str(content[0].get("text") or "MCP tool failed"))
        raise IlloBridgeError("MCP tool failed")
    content = result.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = str(content[0].get("text") or "")
        if text:
            try:
                value = json.loads(text)
                return value if isinstance(value, dict) else {"value": value}
            except json.JSONDecodeError as exc:
                raise IlloBridgeError("MCP tool returned invalid JSON text") from exc
    return result


class IlloBridgeClient:
    """Thin HTTP client for the scoped Illo personal-agent bridge API."""

    def __init__(self, config: IlloBridgeConfig):
        self.config = config

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self.config.base_url.rstrip('/')}{path}"
        if not query:
            return url
        clean_query = {
            key: str(value)
            for key, value in query.items()
            if value is not None and str(value).strip()
        }
        if not clean_query:
            return url
        return f"{url}?{urllib.parse.urlencode(clean_query)}"

    def _request(self, method: str, path: str, *, payload: dict[str, Any] | None = None, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return _json_request(
            method,
            self._url(path, query=query),
            token=self.config.token,
            payload=payload,
            timeout=self.config.timeout_seconds,
        )

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in TOOL_NAMES:
            raise IlloBridgeError(f"Unsupported Illo tool: {tool_name}")
        response = self._request(
            "POST",
            "/api/mcp",
            payload={
                "jsonrpc": "2.0",
                "id": tool_name,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": _drop_empty(arguments)},
            },
        )
        return _decode_mcp_tool_result(response)

    def submit(
        self,
        message: str,
        *,
        origin: str = "codex.submit",
        parts: list[dict[str, Any]] | None = None,
        source: dict[str, Any] | None = None,
        constraints: dict[str, Any] | None = None,
        correlation: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        source_tool: str = "codex",
        repo: str | None = None,
        branch: str | None = None,
        task_title: str | None = None,
        files_touched: list[str] | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        return self.call_tool(
            TOOL_SUBMIT,
            {
                **extra,
                "message": str(message),
                "origin": str(origin or "codex.submit"),
                "parts": list(parts or []),
                "source": dict(source or {}),
                "constraints": dict(constraints or {}),
                "correlation": dict(correlation or {}),
                "response": dict(response or {}),
                "idempotency_key": idempotency_key,
                "source_tool": source_tool,
                "repo": repo,
                "branch": branch,
                "task_title": task_title,
                "files_touched": _clean_string_list(files_touched),
                "session_id": session_id,
                "run_id": run_id,
                "metadata": _clean_metadata(metadata),
            },
        )

    def read(
        self,
        capability: str,
        *,
        arguments: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        return self.call_tool(
            TOOL_READ,
            {
                **extra,
                "capability": str(capability),
                "arguments": dict(arguments or {}),
            },
        )

    def act(
        self,
        capability: str,
        *,
        arguments: dict[str, Any] | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        return self.call_tool(
            TOOL_ACT,
            {
                **extra,
                "capability": str(capability),
                "arguments": dict(arguments or {}),
                "reason": reason,
                "idempotency_key": idempotency_key,
                "metadata": _clean_metadata(metadata),
            },
        )

    def get_result(
        self,
        result_id: str | None = None,
        *,
        event_id: str | None = None,
        submission_id: str | None = None,
        include_payload: bool | None = None,
        limit: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        return self.call_tool(
            TOOL_GET_RESULT,
            {
                **extra,
                "event_id": event_id,
                "submission_id": submission_id,
                "result_id": result_id,
                "include_payload": include_payload,
                "limit": limit,
            },
        )


def _client() -> IlloBridgeClient:
    return IlloBridgeClient(IlloBridgeConfig.from_env())


def tool_illo_submit(
    message: str,
    origin: str = "codex.submit",
    parts: list[dict[str, Any]] | None = None,
    source: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    correlation: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    source_tool: str = "codex",
    repo: str | None = None,
    branch: str | None = None,
    task_title: str | None = None,
    files_touched: list[str] | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return _client().submit(
        message=message,
        origin=origin,
        parts=parts,
        source=source,
        constraints=constraints,
        correlation=correlation,
        response=response,
        idempotency_key=idempotency_key,
        source_tool=source_tool,
        repo=repo,
        branch=branch,
        task_title=task_title,
        files_touched=files_touched,
        session_id=session_id,
        run_id=run_id,
        metadata=metadata,
        **extra,
    )


def tool_illo_read(
    capability: str,
    arguments: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return _client().read(
        capability=capability,
        arguments=arguments,
        **extra,
    )


def tool_illo_act(
    capability: str,
    arguments: dict[str, Any] | None = None,
    reason: str | None = None,
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return _client().act(
        capability=capability,
        arguments=arguments,
        reason=reason,
        idempotency_key=idempotency_key,
        metadata=metadata,
        **extra,
    )


def tool_illo_get_result(
    result_id: str | None = None,
    event_id: str | None = None,
    submission_id: str | None = None,
    include_payload: bool | None = None,
    limit: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return _client().get_result(
        result_id=result_id,
        event_id=event_id,
        submission_id=submission_id,
        include_payload=include_payload,
        limit=limit,
        **extra,
    )


ToolFunction = Callable[..., dict[str, Any]]


TOOLS: dict[str, dict[str, Any]] = {
    TOOL_SUBMIT: {
        "function": tool_illo_submit,
        "description": (
            "Submit instructions, context, traces, decisions, or work material to Illo. "
            "Use this when the request needs Illo's judgment, memory, or coordination. "
            "The call is async-first: Illo stores an inbound event, queues headless handling, "
            "and returns an event id that can be read with illo_get_result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Natural-language instruction or context for Illo to handle.",
                },
                "origin": {"type": "string", "description": "Stable event name.", "default": "codex.submit"},
                "parts": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Ordered source context parts.",
                    "default": [],
                },
                "source": {
                    "type": "object",
                    "description": "Optional provenance about the personal agent, repo, branch, model, or session.",
                    "default": {},
                },
                "constraints": {
                    "type": "object",
                    "description": "Optional privacy, urgency, visibility, or notification boundaries.",
                    "default": {},
                },
                "correlation": {
                    "type": "object",
                    "description": "Optional thread_id, thread_url, external_session_id, or prior submission reference.",
                    "default": {},
                },
                "response": {
                    "type": "object",
                    "description": "Optional callback or webhook routing hints.",
                    "default": {},
                },
                "idempotency_key": {"type": "string", "description": "Optional dedupe key."},
                "source_tool": {"type": "string", "description": "Personal tool name.", "default": "codex"},
                "repo": {"type": "string", "description": "Repository or workspace hint."},
                "branch": {"type": "string", "description": "Branch/worktree hint."},
                "task_title": {"type": "string", "description": "Task title or short objective."},
                "files_touched": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files touched during the work session.",
                    "default": [],
                },
                "session_id": {"type": "string", "description": "Optional tool session id."},
                "run_id": {"type": "string", "description": "Optional tool run id."},
                "metadata": {"type": "object", "description": "Optional metadata.", "default": {}},
            },
            "required": ["message"],
        },
    },
    TOOL_READ: {
        "function": tool_illo_read,
        "description": (
            "Read deterministic Illo workspace information through a named capability. "
            "Use this for direct lookup and search; use illo_submit when the request needs "
            "Illo's interpretation or decision."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "description": "Read capability name, such as workspace.search, project_contexts.search, thread.get, handoff.get, team.members.list, domain.inspect, or capabilities.",
                },
                "arguments": {"type": "object", "description": "Capability-specific arguments.", "default": {}},
            },
            "required": ["capability"],
        },
    },
    TOOL_ACT: {
        "function": tool_illo_act,
        "description": (
            "Execute a deterministic external-agent action as the user's delegate through "
            "a named capability. Use illo_submit when the action should be decided by Illo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "description": "Action capability name, such as thread.create, thread.post_message, handoff.create, domain.record.write, or capabilities.",
                },
                "arguments": {
                    "type": "object",
                    "description": "Capability-specific arguments.",
                    "default": {},
                },
                "reason": {"type": "string", "description": "Optional natural-language reason for audit/provenance."},
                "idempotency_key": {"type": "string", "description": "Optional dedupe key."},
                "metadata": {"type": "object", "description": "Optional machine-readable metadata.", "default": {}},
            },
            "required": ["capability"],
        },
    },
    TOOL_GET_RESULT: {
        "function": tool_illo_get_result,
        "description": (
            "Retrieve or poll the outcome of an asynchronous Illo operation returned by "
            "illo_submit, illo_read, or illo_act. Use this for result_id receipts instead of "
            "repeating the original request."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "result_id": {"type": "string", "description": "Result, receipt, task, or operation id to retrieve."},
                "event_id": {"type": "string", "description": "Inbound event id returned by illo_submit."},
                "submission_id": {"type": "string", "description": "Alias for event_id."},
                "include_payload": {"type": "boolean", "description": "Whether to include stored payloads.", "default": True},
                "limit": {"type": "integer", "description": "Maximum decision receipts to return.", "default": 25},
            },
            "required": [],
        },
    },
}


def _tool_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, default=str),
            }
        ]
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one MCP JSON-RPC request."""
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "illo-personal-agent-mcp", "version": "0.1.0"},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": name,
                        "description": spec["description"],
                        "inputSchema": spec["inputSchema"],
                    }
                    for name, spec in TOOLS.items()
                ]
            },
        }

    if method == "tools/call":
        params = request.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        if tool_name not in TOOLS:
            return {"jsonrpc": "2.0", "id": req_id, "result": _tool_error(f"Unknown tool: {tool_name}")}
        try:
            function: ToolFunction = TOOLS[tool_name]["function"]
            result = function(**arguments)
            return {"jsonrpc": "2.0", "id": req_id, "result": _tool_result(result)}
        except Exception as exc:
            logger.exception("Tool %s failed", tool_name)
            return {"jsonrpc": "2.0", "id": req_id, "result": _tool_error(f"Error: {exc}")}

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def run_stdio() -> None:
    logging.basicConfig(
        level=os.environ.get("ILLO_MCP_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    logger.info("Illo Personal Agent MCP starting")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            response = handle_request(json.loads(line))
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            logger.warning("Invalid JSON-RPC request: %s", line[:100])
        except Exception as exc:
            logger.exception("Request handling failed: %s", exc)


def run_http(port: int = 9878) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            try:
                response = handle_request(json.loads(body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if response is not None:
                    self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))
            except Exception as exc:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(exc).encode("utf-8"))

        def log_message(self, format, *args):
            logger.info(format, *args)

    logging.basicConfig(level=os.environ.get("ILLO_MCP_LOG_LEVEL", "INFO"))
    server = HTTPServer(("127.0.0.1", port), Handler)
    logger.info("Illo Personal Agent MCP listening on http://127.0.0.1:%s", port)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Illo Personal Agent MCP")
    parser.add_argument("--http", action="store_true", help="Run HTTP debug transport instead of stdio")
    parser.add_argument("--port", type=int, default=9878, help="HTTP debug port")
    args = parser.parse_args(argv)
    if args.http:
        run_http(args.port)
    else:
        run_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
