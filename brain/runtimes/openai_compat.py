"""A small Chat Completions loop. Only MCP tools enter the model catalog."""

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp.types import Tool

from brain.mcp_client import connect, list_tools
from brain.models import BrainEvent, RunError, RunRequest, event
from brain.runtimes.provider_errors import error_body, provider_error


def function_tools(tools: list[Tool]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema,
            },
        }
        for tool in tools
    ]


async def chunks(response: httpx.Response) -> AsyncIterator[dict]:
    """Read SSE data fields, including events with more than one data line."""
    lines = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            lines.append(line[5:].lstrip(" "))
        elif not line and lines:
            data = "\n".join(lines)
            lines = []
            if data == "[DONE]":
                return
            yield json.loads(data)
    if lines and "\n".join(lines) != "[DONE]":
        yield json.loads("\n".join(lines))


def add_usage(total: dict, usage: dict) -> None:
    for key, value in usage.items():
        if isinstance(value, dict):
            add_usage(total.setdefault(key, {}), value)
        elif isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value


async def conversation(
    request: RunRequest, session: ClientSession, tools: list[Tool], http: httpx.AsyncClient
) -> AsyncIterator[BrainEvent]:
    catalog = function_tools(tools)
    names = {tool.name for tool in tools}
    yield event("started", catalog=sorted(names))
    messages = [
        {"role": "system", "content": request.system_prompt},
        {"role": "user", "content": request.user_message},
    ]
    usage = {}
    # Compose passes an empty value when the operator sets none. Treat empty as not set.
    base = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    for _ in range(request.max_turns):
        payload = {
            "model": request.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if catalog:
            payload["tools"] = catalog
        if effort := os.getenv("ILLO_BRAIN_OPENAI_COMPAT_REASONING_EFFORT"):
            payload["reasoning_effort"] = effort
        calls = {}
        text = []
        finish = None
        async with http.stream("POST", base + "/chat/completions", json=payload) as response:
            if response.is_error:
                raise provider_error(await error_body(response), response.status_code)
            response.raise_for_status()
            async for chunk in chunks(response):
                if "error" in chunk:
                    raise provider_error(chunk["error"])
                add_usage(usage, chunk.get("usage") or {})
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish = choice.get("finish_reason") or finish
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text.append(delta["content"])
                    yield event("text", text=delta["content"])
                for part in delta.get("tool_calls") or []:
                    call = calls.setdefault(
                        part["index"],
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if part.get("id"):
                        call["id"] += part["id"]
                    function = part.get("function") or {}
                    for key in ("name", "arguments"):
                        call["function"][key] += function.get(key) or ""
        if finish not in ("stop", "tool_calls"):
            category = "limit" if finish == "length" else "runtime"
            raise RunError(category, "The model response did not complete")
        answer = "".join(text)
        if not calls:
            if finish != "stop":
                raise RunError("runtime", "The model returned an empty tool call")
            yield event("final", text=answer, usage=usage)
            return
        messages.append(
            {"role": "assistant", "content": answer or None, "tool_calls": list(calls.values())}
        )
        for call in calls.values():
            name = call["function"]["name"]
            if name not in names or not call["id"]:
                raise RunError("runtime", "The model requested a tool outside the run catalog")
            yield event("tool_call", name=name)
            try:
                arguments = json.loads(call["function"]["arguments"])
                if not isinstance(arguments, dict):
                    raise TypeError("Tool arguments must be an object")
            except (ValueError, TypeError):
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": "Invalid tool arguments"}],
                }
            else:
                # MCP tool failures remain tool results. The model can explain them.
                try:
                    result = (await session.call_tool(name, arguments)).model_dump(
                        mode="json", exclude_none=True
                    )
                except McpError:
                    result = {
                        "isError": True,
                        "content": [{"type": "text", "text": "The MCP tool failed"}],
                    }
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}
            )
    yield event("error", category="limit", message="The run reached its turn limit", usage=usage)


async def run(request: RunRequest, directory: Path) -> AsyncIterator[BrainEvent]:
    key = os.getenv("OPENAI_API_KEY")
    if not key and not os.getenv("OPENAI_BASE_URL"):
        raise RunError("auth", "Set OPENAI_API_KEY for the OpenAI-compatible runtime")
    headers = {"Authorization": "Bearer " + key} if key else {}
    async with connect(request) as session:
        tools = await list_tools(session)
        async with httpx.AsyncClient(
            headers=headers, timeout=request.timeout_s, follow_redirects=False
        ) as http:
            async for item in conversation(request, session, tools, http):
                yield item
