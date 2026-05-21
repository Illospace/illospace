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
CONTEXT_TOOL_NAME = "illo_submit_context"


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

    def search_workspace(self, query: str, limit: int = 10) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/agent-bridge/workspace/search",
            payload={"query": str(query), "limit": int(limit)},
        )

    def get_thread(self, idea_id: str, limit: int = 100) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/agent-bridge/workspace/threads/{urllib.parse.quote(str(idea_id), safe='')}",
            query={"limit": int(limit)},
        )

    def get_team_members(self) -> dict[str, Any]:
        return self._request("GET", "/api/agent-bridge/workspace/team-members")

    def submit_context(
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
    ) -> dict[str, Any]:
        arguments = _drop_empty(
            {
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
            }
        )
        response = self._request(
            "POST",
            "/api/mcp",
            payload={
                "jsonrpc": "2.0",
                "id": CONTEXT_TOOL_NAME,
                "method": "tools/call",
                "params": {"name": CONTEXT_TOOL_NAME, "arguments": arguments},
            },
        )
        return _decode_mcp_tool_result(response)

    def create_thread(
        self,
        title: str,
        body: str,
        *,
        teammate_user_ids: list[str] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        trigger_illo: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/agent-bridge/illo/threads",
            payload={
                "title": str(title),
                "body": str(body),
                "teammate_user_ids": _clean_string_list(teammate_user_ids),
                "artifacts": list(artifacts or []),
                "trigger_illo": bool(trigger_illo),
                "metadata": _clean_metadata(metadata),
            },
        )

    def post_thread_message(
        self,
        idea_id: str,
        body: str,
        *,
        teammate_user_ids: list[str] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        trigger_illo: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/agent-bridge/illo/threads/{urllib.parse.quote(str(idea_id), safe='')}/messages",
            payload={
                "body": str(body),
                "teammate_user_ids": _clean_string_list(teammate_user_ids),
                "artifacts": list(artifacts or []),
                "trigger_illo": bool(trigger_illo),
                "metadata": _clean_metadata(metadata),
            },
        )

    def ask_illo(
        self,
        question: str,
        *,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/agent-bridge/illo/ask",
            payload={
                "question": str(question),
                "context": dict(context or {}),
                "metadata": _clean_metadata(metadata),
            },
        )

    def get_ask(self, ask_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/agent-bridge/illo/ask/{urllib.parse.quote(str(ask_id), safe='')}",
        )


def _client() -> IlloBridgeClient:
    return IlloBridgeClient(IlloBridgeConfig.from_env())


def tool_illo_search_workspace(query: str, limit: int = 10) -> dict[str, Any]:
    return _client().search_workspace(query=query, limit=limit)


def tool_illo_get_thread(idea_id: str, limit: int = 100) -> dict[str, Any]:
    return _client().get_thread(idea_id=idea_id, limit=limit)


def tool_illo_get_team_members() -> dict[str, Any]:
    return _client().get_team_members()


def tool_illo_submit_context(
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
) -> dict[str, Any]:
    return _client().submit_context(
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
    )


def tool_illo_create_thread(
    title: str,
    body: str,
    teammate_user_ids: list[str] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    trigger_illo: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _client().create_thread(
        title=title,
        body=body,
        teammate_user_ids=teammate_user_ids,
        artifacts=artifacts,
        trigger_illo=trigger_illo,
        metadata=metadata,
    )


def tool_illo_post_thread_message(
    idea_id: str,
    body: str,
    teammate_user_ids: list[str] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    trigger_illo: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _client().post_thread_message(
        idea_id=idea_id,
        body=body,
        teammate_user_ids=teammate_user_ids,
        artifacts=artifacts,
        trigger_illo=trigger_illo,
        metadata=metadata,
    )


def tool_illo_ask(
    question: str,
    context: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _client().ask_illo(question=question, context=context, metadata=metadata)


def tool_illo_get_ask(ask_id: str) -> dict[str, Any]:
    return _client().get_ask(ask_id=ask_id)


ToolFunction = Callable[..., dict[str, Any]]


TOOLS: dict[str, dict[str, Any]] = {
    CONTEXT_TOOL_NAME: {
        "function": tool_illo_submit_context,
        "description": (
            "Submit ordered context from a personal agent to Illo, the user's team agent. "
            "Use this when the user wants Illo or the team to have the current AI thread, "
            "trace, artifacts, files, links, diffs, or other source material. The personal "
            "agent supplies context and provenance; Illo coordinates the team workspace."
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
    "illo_search_workspace": {
        "function": tool_illo_search_workspace,
        "description": (
            "Search the Illo workspace for related ideas, threads, and shared work. "
            "Use this before creating a new thread when the user asks to share work, "
            "continue prior work, or avoid duplicating an existing Illo discussion."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms for Illo workspace context."},
                "limit": {"type": "integer", "description": "Maximum results to return, 1-25.", "default": 10},
            },
            "required": ["query"],
        },
    },
    "illo_get_thread": {
        "function": tool_illo_get_thread,
        "description": (
            "Read messages from an existing Illo idea/thread. Use this before posting "
            "an update so replies preserve team context and avoid repeating prior work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "idea_id": {"type": "string", "description": "Illo idea/thread id."},
                "limit": {"type": "integer", "description": "Maximum messages to return.", "default": 100},
            },
            "required": ["idea_id"],
        },
    },
    "illo_create_thread": {
        "function": tool_illo_create_thread,
        "description": (
            "Advanced compatibility tool for creating a visible Illo thread from "
            "personal-agent work. Use only when the user explicitly asks to share work "
            "with teammates, publish findings into Illo, or start a team-visible "
            "discussion. For automatic hooks and routine progress, prefer "
            f"{CONTEXT_TOOL_NAME} so IloSpace can route the context. Set trigger_illo "
            "only when the user wants Ilo to actively respond or the message explicitly "
            "mentions Ilo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Thread title visible in Illo."},
                "body": {"type": "string", "description": "Thread body/message visible to the team."},
                "teammate_user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional Illo user ids to notify.",
                    "default": [],
                },
                "artifacts": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional structured artifacts, links, or files to attach.",
                    "default": [],
                },
                "trigger_illo": {
                    "type": "boolean",
                    "description": "Whether Illo should actively respond to this new thread.",
                    "default": False,
                },
                "metadata": {"type": "object", "description": "Optional machine-readable metadata.", "default": {}},
            },
            "required": ["title", "body"],
        },
    },
    "illo_post_thread_message": {
        "function": tool_illo_post_thread_message,
        "description": (
            "Advanced compatibility tool for posting a visible update into an existing "
            "Illo thread. Use only when the user explicitly names or provides the thread "
            "destination. For automatic hooks and routine progress, prefer "
            f"{CONTEXT_TOOL_NAME} so IloSpace can route the context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "idea_id": {"type": "string", "description": "Existing Illo idea/thread id."},
                "body": {"type": "string", "description": "Message body to post into the thread."},
                "teammate_user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional Illo user ids to notify.",
                    "default": [],
                },
                "artifacts": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional structured artifacts, links, or files to attach.",
                    "default": [],
                },
                "trigger_illo": {
                    "type": "boolean",
                    "description": "Whether Illo should actively respond to this message.",
                    "default": False,
                },
                "metadata": {"type": "object", "description": "Optional machine-readable metadata.", "default": {}},
            },
            "required": ["idea_id", "body"],
        },
    },
    "illo_ask": {
        "function": tool_illo_ask,
        "description": (
            "Ask Illo for private workspace context without creating a visible thread. "
            "Use when the personal agent needs Illo's workspace knowledge, team memory, "
            "or project context before doing work. This is read/context mode, not "
            "team-visible coordination; create or post to a visible thread with "
            "trigger_illo=true when Illo should coordinate or hand off work. Poll "
            "with illo_get_ask."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Question for Illo's headless context agent."},
                "context": {
                    "type": "object",
                    "description": "Optional context about the current personal-agent task.",
                    "default": {},
                },
                "metadata": {"type": "object", "description": "Optional machine-readable metadata.", "default": {}},
            },
            "required": ["question"],
        },
    },
    "illo_get_ask": {
        "function": tool_illo_get_ask,
        "description": (
            "Poll a headless Illo ask created by illo_ask. Use this to retrieve "
            "status, events, and final answer artifacts without creating team-visible noise."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ask_id": {"type": "string", "description": "Task id returned by illo_ask."},
            },
            "required": ["ask_id"],
        },
    },
    "illo_get_team_members": {
        "function": tool_illo_get_team_members,
        "description": (
            "List visible Illo team members. Use before sharing work with named "
            "teammates so illo_create_thread or illo_post_thread_message can notify "
            "the right user ids."
        ),
        "inputSchema": {"type": "object", "properties": {}},
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
