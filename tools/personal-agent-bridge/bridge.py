#!/usr/bin/env python3
"""Neutral personal-agent bridge for Illo external-agent tasks.

This MVP bridge deliberately keeps the Illo contract stable while Hermes and
OpenClaw adapters can evolve independently behind it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class BridgeError(RuntimeError):
    pass


def _json_request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    body = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, method=method.upper(), headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BridgeError(f"{method} {url} failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise BridgeError(f"{method} {url} failed: {exc}") from exc


@dataclass
class IlloClient:
    base_url: str
    token: str

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    def heartbeat(self, *, status: str = "online", capabilities: dict[str, Any] | None = None) -> dict[str, Any]:
        return _json_request(
            "POST",
            self._url("/api/agent-bridge/heartbeat"),
            token=self.token,
            payload={"status": status, "capabilities": capabilities or {}},
        )

    def claim(self, max_tasks: int = 1) -> list[dict[str, Any]]:
        response = _json_request(
            "POST",
            self._url("/api/agent-bridge/tasks/claim"),
            token=self.token,
            payload={"max_tasks": max_tasks},
        )
        return list(response.get("tasks") or [])

    def event(self, task_id: str, event_type: str, *, status: str | None = None, message: str | None = None) -> None:
        _json_request(
            "POST",
            self._url(f"/api/agent-bridge/tasks/{task_id}/events"),
            token=self.token,
            payload={"event_type": event_type, "status": status, "message": message, "payload": {}},
        )

    def complete(self, task_id: str, result_summary: str, *, artifacts: list[dict[str, Any]] | None = None) -> None:
        _json_request(
            "POST",
            self._url(f"/api/agent-bridge/tasks/{task_id}/complete"),
            token=self.token,
            payload={"result_summary": result_summary, "artifacts": artifacts or [], "payload": {}},
        )

    def fail(self, task_id: str, error: str) -> None:
        _json_request(
            "POST",
            self._url(f"/api/agent-bridge/tasks/{task_id}/fail"),
            token=self.token,
            payload={"error": error, "payload": {}},
        )

    def ask_illo(self, question: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        return _json_request(
            "POST",
            self._url("/api/agent-bridge/illo/ask"),
            token=self.token,
            payload={"question": question, "context": context or {}},
        )


class FakeAdapter:
    name = "fake"

    def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        title = task.get("title") or "Untitled task"
        instructions = task.get("instructions") or ""
        return {
            "result_summary": (
                f"Fake personal agent completed '{title}'.\n\n"
                f"Received instructions:\n{instructions}"
            ),
            "artifacts": [
                {
                    "kind": "json",
                    "title": "Task echo",
                    "content_json": {
                        "task_id": task.get("id"),
                        "source_surface": task.get("source_surface"),
                        "input_part_count": len(task.get("input_parts") or []),
                    },
                }
            ],
        }


class HttpAdapter:
    name = "http"
    env_base_url = ""
    env_api_key = ""
    env_endpoint = ""
    default_endpoint = "/api/agent/run"

    def __init__(self) -> None:
        self.base_url = os.environ.get(self.env_base_url, "").rstrip("/")
        self.api_key = os.environ.get(self.env_api_key, "")
        self.endpoint = os.environ.get(self.env_endpoint, self.default_endpoint)

    def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url:
            raise BridgeError(f"{self.env_base_url} is required for {self.name} adapter")
        response = _json_request(
            "POST",
            f"{self.base_url}{self.endpoint}",
            token=self.api_key or None,
            payload={"task": task, "protocol": "illo.external_agent_task.v1"},
            timeout=float(os.environ.get("PERSONAL_AGENT_TIMEOUT", "300")),
        )
        result = response.get("result_summary") or response.get("final") or response.get("output") or response.get("answer")
        if not result:
            result = json.dumps(response, indent=2, sort_keys=True)
        return {
            "result_summary": str(result),
            "artifacts": list(response.get("artifacts") or []),
        }

def _format_task_for_personal_agent(task: dict[str, Any]) -> str:
    title = str(task.get("title") or "Untitled Illo task").strip()
    instructions = str(task.get("instructions") or "").strip()
    source_surface = str(task.get("source_surface") or "illo").strip()
    input_parts = task.get("input_parts") or []
    try:
        input_text = json.dumps(input_parts, indent=2, sort_keys=True)
    except TypeError:
        input_text = str(input_parts)
    return "\n\n".join(
        [
            f"Illo delegated task: {title}",
            f"Source surface: {source_surface}",
            f"Task id: {task.get('id')}",
            f"Instructions:\n{instructions}",
            f"Input parts:\n{input_text}",
            (
                "Return the final result as plain text suitable for posting back to Illo. "
                "If you create files, links, or other artifacts, summarize what changed and include the paths or URLs."
            ),
        ]
    )


class HermesAdapter(HttpAdapter):
    name = "hermes"
    env_base_url = "HERMES_BASE_URL"
    env_api_key = "HERMES_API_KEY"
    env_endpoint = "HERMES_RUN_ENDPOINT"
    default_endpoint = "/v1/chat/completions"

    def __init__(self) -> None:
        super().__init__()
        self.model = os.environ.get("HERMES_MODEL", "hermes-agent")
        self.api_mode = os.environ.get("HERMES_API_MODE", "chat").strip().lower()
        if self.api_mode not in {"chat", "chat_completions", "runs"}:
            raise BridgeError("HERMES_API_MODE must be 'chat' or 'runs'")

    def _headers_for_task(self, task: dict[str, Any]) -> dict[str, str]:
        task_id = str(task.get("id") or "").strip()
        connection_id = str(task.get("connection_id") or "").strip()
        headers: dict[str, str] = {}
        if task_id:
            headers["X-Hermes-Session-Id"] = f"illo-task-{task_id}"
        if connection_id:
            headers["X-Hermes-Session-Key"] = f"illo-connection-{connection_id}"
        return headers

    def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url:
            raise BridgeError(f"{self.env_base_url} is required for {self.name} adapter")
        if self.api_mode == "runs":
            return self._run_task_with_runs_api(task)
        return self._run_task_with_chat_completions(task)

    def _run_task_with_chat_completions(self, task: dict[str, Any]) -> dict[str, Any]:
        response = _json_request(
            "POST",
            f"{self.base_url}{self.endpoint}",
            token=self.api_key or None,
            headers=self._headers_for_task(task),
            payload={
                "model": self.model,
                "stream": False,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are Hermes, a personal autonomous agent connected to Illo. "
                            "Complete the delegated work and return only the final user-facing result."
                        ),
                    },
                    {"role": "user", "content": _format_task_for_personal_agent(task)},
                ],
            },
            timeout=float(os.environ.get("PERSONAL_AGENT_TIMEOUT", "300")),
        )
        result = ""
        try:
            result = str(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            result = str(response.get("output") or response.get("answer") or "")
        if not result:
            result = json.dumps(response, indent=2, sort_keys=True)
        artifacts = list(response.get("artifacts") or [])
        metadata = {key: response.get(key) for key in ("id", "model", "usage") if response.get(key) is not None}
        if metadata:
            artifacts.append({"kind": "json", "title": "Hermes response metadata", "content_json": metadata})
        return {"result_summary": result, "artifacts": artifacts}

    def _run_task_with_runs_api(self, task: dict[str, Any]) -> dict[str, Any]:
        endpoint = os.environ.get(self.env_endpoint, "/v1/runs")
        start = _json_request(
            "POST",
            f"{self.base_url}{endpoint}",
            token=self.api_key or None,
            headers=self._headers_for_task(task),
            payload={
                "input": _format_task_for_personal_agent(task),
                "session_id": f"illo-task-{task.get('id')}",
                "instructions": "Complete the delegated work and return only the final user-facing result for Illo.",
            },
            timeout=float(os.environ.get("PERSONAL_AGENT_TIMEOUT", "300")),
        )
        run_id = str(start.get("run_id") or "").strip()
        if not run_id:
            raise BridgeError(f"Hermes runs API did not return a run_id: {start}")
        deadline = time.monotonic() + float(os.environ.get("PERSONAL_AGENT_TIMEOUT", "300"))
        poll_interval = max(0.5, float(os.environ.get("HERMES_RUN_POLL_INTERVAL", "2")))
        status: dict[str, Any] = start
        while time.monotonic() < deadline:
            status = _json_request(
                "GET",
                f"{self.base_url}{endpoint.rstrip('/')}/{run_id}",
                token=self.api_key or None,
                timeout=30,
            )
            state = str(status.get("status") or "").lower()
            if state == "completed":
                output = str(status.get("output") or "Hermes run completed.")
                return {
                    "result_summary": output,
                    "artifacts": [
                        {
                            "kind": "json",
                            "title": "Hermes run metadata",
                            "content_json": {
                                key: status.get(key)
                                for key in ("run_id", "session_id", "model", "usage", "last_event")
                                if status.get(key) is not None
                            },
                        }
                    ],
                }
            if state in {"failed", "cancelled", "canceled"}:
                raise BridgeError(f"Hermes run {run_id} ended with status {state}: {status.get('error') or status}")
            time.sleep(poll_interval)
        raise BridgeError(f"Hermes run {run_id} did not finish before timeout; last status: {status}")


class OpenClawAdapter(HttpAdapter):
    name = "openclaw"
    env_base_url = "OPENCLAW_BASE_URL"
    env_api_key = "OPENCLAW_API_KEY"
    env_endpoint = "OPENCLAW_RUN_ENDPOINT"


def adapter_for(kind: str):
    normalized = (kind or "fake").strip().lower()
    if normalized == "fake":
        return FakeAdapter()
    if normalized == "hermes":
        return HermesAdapter()
    if normalized == "openclaw":
        return OpenClawAdapter()
    raise BridgeError(f"Unknown adapter: {kind}")


def run_once(client: IlloClient, adapter, *, max_tasks: int) -> int:
    client.heartbeat(status="online", capabilities={"adapter": getattr(adapter, "name", "unknown")})
    tasks = client.claim(max_tasks=max_tasks)
    if not tasks:
        print("No tasks claimed.")
        return 0
    for task in tasks:
        task_id = str(task["id"])
        try:
            print(f"Running task {task_id}: {task.get('title')}")
            client.event(task_id, "external_task.started", status="running", message="Personal agent started work")
            result = adapter.run_task(task)
            client.complete(
                task_id,
                str(result.get("result_summary") or "Task completed."),
                artifacts=list(result.get("artifacts") or []),
            )
            print(f"Completed task {task_id}.")
        except Exception as exc:
            client.fail(task_id, str(exc))
            print(f"Failed task {task_id}: {exc}", file=sys.stderr)
    return len(tasks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Illo personal-agent bridge.")
    parser.add_argument("command", choices=["once", "run", "heartbeat", "ask"])
    parser.add_argument("--illo-base-url", default=os.environ.get("ILLO_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--token", default=os.environ.get("ILLO_BRIDGE_TOKEN", ""))
    parser.add_argument("--adapter", default=os.environ.get("PERSONAL_AGENT_ADAPTER", "fake"))
    parser.add_argument("--max-tasks", type=int, default=int(os.environ.get("ILLO_BRIDGE_MAX_TASKS", "1")))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("ILLO_BRIDGE_POLL_INTERVAL", "5")))
    parser.add_argument("--question", default="")
    args = parser.parse_args(argv)

    if not args.token:
        raise BridgeError("ILLO_BRIDGE_TOKEN or --token is required")

    client = IlloClient(base_url=args.illo_base_url, token=args.token)
    adapter = adapter_for(args.adapter)

    if args.command == "heartbeat":
        print(json.dumps(client.heartbeat(capabilities={"adapter": adapter.name}), indent=2))
        return 0
    if args.command == "ask":
        if not args.question:
            raise BridgeError("--question is required for ask")
        print(json.dumps(client.ask_illo(args.question), indent=2))
        return 0
    if args.command == "once":
        run_once(client, adapter, max_tasks=args.max_tasks)
        return 0

    while True:
        try:
            run_once(client, adapter, max_tasks=args.max_tasks)
        except Exception as exc:
            print(f"Bridge poll failed: {exc}", file=sys.stderr)
            try:
                client.heartbeat(status="error", capabilities={"adapter": adapter.name})
            except Exception:
                pass
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BridgeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
