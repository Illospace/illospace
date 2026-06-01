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
        intent: str,
        *,
        origin: str = "codex.context",
        parts: list[dict[str, Any]] | None = None,
        source: dict[str, Any] | None = None,
        constraints: dict[str, Any] | None = None,
        correlation: dict[str, Any] | None = None,
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
                "intent": str(intent),
                "origin": str(origin or "codex.context"),
                "parts": list(parts or []),
                "source": dict(source or {}),
                "constraints": dict(constraints or {}),
                "correlation": dict(correlation or {}),
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
        request: str,
        *,
        resource: str | None = None,
        query: str | None = None,
        target_id: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        context: dict[str, Any] | None = None,
        constraints: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        return self.call_tool(
            TOOL_READ,
            {
                **extra,
                "request": str(request),
                "resource": resource,
                "query": query,
                "target_id": target_id,
                "limit": limit,
                "cursor": cursor,
                "context": dict(context or {}),
                "constraints": dict(constraints or {}),
                "metadata": _clean_metadata(metadata),
            },
        )

    def act(
        self,
        intent: str,
        *,
        action: str | None = None,
        target: dict[str, Any] | None = None,
        content: Any | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        teammate_user_ids: list[str] | None = None,
        constraints: dict[str, Any] | None = None,
        correlation: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        return self.call_tool(
            TOOL_ACT,
            {
                **extra,
                "intent": str(intent),
                "action": action,
                "target": dict(target or {}),
                "content": content,
                "artifacts": list(artifacts or []),
                "teammate_user_ids": _clean_string_list(teammate_user_ids),
                "constraints": dict(constraints or {}),
                "correlation": dict(correlation or {}),
                "idempotency_key": idempotency_key,
                "metadata": _clean_metadata(metadata),
            },
        )

    def get_result(
        self,
        result_id: str,
        *,
        wait_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        return self.call_tool(
            TOOL_GET_RESULT,
            {
                **extra,
                "result_id": str(result_id),
                "wait_ms": wait_ms,
                "metadata": _clean_metadata(metadata),
            },
        )


def _client() -> IlloBridgeClient:
    return IlloBridgeClient(IlloBridgeConfig.from_env())


def tool_illo_submit(
    intent: str,
    origin: str = "codex.context",
    parts: list[dict[str, Any]] | None = None,
    source: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    correlation: dict[str, Any] | None = None,
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
        intent=intent,
        origin=origin,
        parts=parts,
        source=source,
        constraints=constraints,
        correlation=correlation,
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
    request: str,
    resource: str | None = None,
    query: str | None = None,
    target_id: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    context: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return _client().read(
        request=request,
        resource=resource,
        query=query,
        target_id=target_id,
        limit=limit,
        cursor=cursor,
        context=context,
        constraints=constraints,
        metadata=metadata,
        **extra,
    )


def tool_illo_act(
    intent: str,
    action: str | None = None,
    target: dict[str, Any] | None = None,
    content: Any | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    teammate_user_ids: list[str] | None = None,
    constraints: dict[str, Any] | None = None,
    correlation: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    metadata: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return _client().act(
        intent=intent,
        action=action,
        target=target,
        content=content,
        artifacts=artifacts,
        teammate_user_ids=teammate_user_ids,
        constraints=constraints,
        correlation=correlation,
        idempotency_key=idempotency_key,
        metadata=metadata,
        **extra,
    )


def tool_illo_get_result(
    result_id: str,
    wait_ms: int | None = None,
    metadata: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return _client().get_result(result_id=result_id, wait_ms=wait_ms, metadata=metadata, **extra)


ToolFunction = Callable[..., dict[str, Any]]


TOOLS: dict[str, dict[str, Any]] = {
    TOOL_SUBMIT: {
        "function": tool_illo_submit,
        "description": (
            "Submit ordered context or a work handoff from a personal agent to Illo, the user's "
            "team agent. Use this when Illo or the team should receive the current thread, trace, "
            "artifacts, files, links, diffs, or other source material. The personal agent supplies "
            "context and provenance; Illo decides routing and may return a receipt, result_id, or "
            "thread_url."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "Natural-language reason this context is being submitted.",
                },
                "origin": {"type": "string", "description": "Stable event name.", "default": "codex.context"},
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
                    "description": "Optional thread_id, external_session_id, or prior submission reference.",
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
            "required": ["intent"],
        },
    },
    TOOL_READ: {
        "function": tool_illo_read,
        "description": (
            "Read Illo workspace context without mutating team-visible state. Use this for "
            "searching workspace context, reading a known Thread or record, resolving teammates, "
            "or asking Illo for private context before deciding whether to act. If the read runs "
            "asynchronously, poll the returned result_id with illo_get_result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request": {"type": "string", "description": "Natural-language read request."},
                "resource": {
                    "type": "string",
                    "description": "Optional resource hint, for example workspace, thread, team_members, project, or memory.",
                },
                "query": {"type": "string", "description": "Optional search terms or filter text."},
                "target_id": {"type": "string", "description": "Optional Thread, project, teammate, task, or result id."},
                "limit": {"type": "integer", "description": "Optional maximum records to return."},
                "cursor": {"type": "string", "description": "Optional pagination cursor."},
                "context": {
                    "type": "object",
                    "description": "Optional current task context to help Illo answer privately.",
                    "default": {},
                },
                "constraints": {
                    "type": "object",
                    "description": "Optional privacy, scope, freshness, or visibility boundaries.",
                    "default": {},
                },
                "metadata": {"type": "object", "description": "Optional machine-readable metadata.", "default": {}},
            },
            "required": ["request"],
        },
    },
    TOOL_ACT: {
        "function": tool_illo_act,
        "description": (
            "Ask Illo to take an explicit, user-authorized action in the team workspace. "
            "Use this for visible coordination such as creating or updating a Thread, notifying "
            "teammates, or asking Illo to actively respond. For passive context handoff use "
            "illo_submit; for non-mutating context lookup use illo_read. Long-running actions may "
            "return result_id for illo_get_result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "description": "Natural-language reason for the action."},
                "action": {
                    "type": "string",
                    "description": "Optional action hint, for example create_thread, post_message, notify, or trigger_illo.",
                },
                "target": {
                    "type": "object",
                    "description": "Optional target descriptor such as a Thread, teammate, project, or workspace entity.",
                    "default": {},
                },
                "content": {
                    "description": "Action-specific body, message, or structured payload.",
                },
                "artifacts": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional structured artifacts, links, or files to attach.",
                    "default": [],
                },
                "teammate_user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional Illo user ids to notify.",
                    "default": [],
                },
                "constraints": {
                    "type": "object",
                    "description": "Optional privacy, urgency, visibility, or notification boundaries.",
                    "default": {},
                },
                "correlation": {
                    "type": "object",
                    "description": "Optional correlation such as thread_id, external_session_id, or prior result id.",
                    "default": {},
                },
                "idempotency_key": {"type": "string", "description": "Optional dedupe key."},
                "metadata": {"type": "object", "description": "Optional machine-readable metadata.", "default": {}},
            },
            "required": ["intent"],
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
                "wait_ms": {
                    "type": "integer",
                    "description": "Optional long-poll wait time in milliseconds.",
                    "default": 0,
                },
                "metadata": {"type": "object", "description": "Optional machine-readable metadata.", "default": {}},
            },
            "required": ["result_id"],
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
