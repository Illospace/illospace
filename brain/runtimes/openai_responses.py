"""A stateless Responses loop with only the tools of the Illo run."""

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
from brain.runtimes.openai_compat import add_usage, chunks
from brain.runtimes.provider_errors import error_body, provider_error

# Workers have one run. This cache lasts only as long as the worker process.
_without_effort: set[tuple[str, str]] = set()


def function_tools(tools: list[Tool]) -> list[dict]:
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
            # Keep the MCP schema, including optional arguments, unchanged.
            "strict": False,
        }
        for tool in tools
    ]


def build_request(request: RunRequest, tools: list[Tool], items: list[dict]) -> dict:
    catalog = function_tools(tools)
    names = {tool.name for tool in tools}
    assert all(tool["type"] == "function" and tool["name"] in names for tool in catalog)
    return {
        "model": request.model,
        "instructions": request.system_prompt,
        "input": items,
        "tools": catalog,
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": 4096,
        "parallel_tool_calls": True,
    }


def check_item(item: dict, names: set[str]) -> None:
    kind = item.get("type")
    if kind not in ("message", "reasoning", "function_call"):
        raise RunError("runtime", "The model returned a tool outside the run catalog")
    if kind == "function_call" and item.get("name") not in names:
        raise RunError("runtime", "The model requested a tool outside the run catalog")


def effort_rejected(error: dict) -> bool:
    param = error.get("param")
    # Read the message only to classify this rejection. Never emit or log it.
    message = str(error.get("message", "")).lower()
    return param in ("reasoning", "reasoning.effort") or (
        error.get("code") in ("unsupported_parameter", "unsupported_value")
        and ("reasoning" in message or "effort" in message)
    )


async def conversation(
    request: RunRequest, session: ClientSession, tools: list[Tool], http: httpx.AsyncClient
) -> AsyncIterator[BrainEvent]:
    names = {tool.name for tool in tools}
    yield event("started", catalog=sorted(names))
    items = [{"role": "user", "content": request.user_message}]
    usage = {}
    base = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    cache_key = (base, request.model)
    for _ in range(request.max_turns):
        payload = build_request(request, tools, items)
        if cache_key not in _without_effort:
            payload["reasoning"] = {"effort": os.getenv("ILLO_BRAIN_EFFORT") or "low"}
        completed = None
        for attempt in range(2):
            async with http.stream("POST", base + "/responses", json=payload) as response:
                if response.is_error:
                    error = await error_body(response)
                    if (
                        response.status_code == 400
                        and attempt == 0
                        and "reasoning" in payload
                        and effort_rejected(error)
                    ):
                        _without_effort.add(cache_key)
                        del payload["reasoning"]
                        continue
                    raise provider_error(error, response.status_code)
                response.raise_for_status()
                async for chunk in chunks(response):
                    kind = chunk.get("type")
                    if kind == "response.output_text.delta":
                        yield event("text", text=chunk["delta"])
                    elif kind in ("response.output_item.added", "response.output_item.done"):
                        check_item(chunk["item"], names)
                    elif kind == "error":
                        raise provider_error(chunk)
                    elif kind in ("response.failed", "response.incomplete"):
                        failed = chunk.get("response") or {}
                        add_usage(usage, failed.get("usage") or {})
                        if kind == "response.incomplete":
                            raise RunError(
                                "limit", "The model response did not complete", usage=usage
                            )
                        raise provider_error(failed.get("error"))
                    elif kind == "response.completed":
                        completed = chunk["response"]
                break
        if completed is None or completed.get("status") != "completed":
            raise RunError("runtime", "The model stream ended without a complete response")
        add_usage(usage, completed.get("usage") or {})
        output = completed.get("output") or []
        for item in output:
            check_item(item, names)
        calls = [item for item in output if item["type"] == "function_call"]
        if not calls:
            answer = "".join(
                part["text"]
                for item in output
                if item["type"] == "message"
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            )
            yield event("final", text=answer, usage=usage)
            return
        # Replay all output, including encrypted reasoning, without a response ID.
        items.extend(output)
        for call in calls:
            if not call.get("call_id"):
                raise RunError("runtime", "The model returned an invalid function call")
            yield event("tool_call", name=call["name"])
            try:
                arguments = json.loads(call["arguments"])
                if not isinstance(arguments, dict):
                    raise TypeError
            except (ValueError, TypeError, KeyError):
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": "Invalid tool arguments"}],
                }
            else:
                try:
                    result = (await session.call_tool(call["name"], arguments)).model_dump(
                        mode="json", exclude_none=True
                    )
                except McpError:
                    result = {
                        "isError": True,
                        "content": [{"type": "text", "text": "The MCP tool failed"}],
                    }
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": json.dumps(result),
                }
            )
    yield event("error", category="limit", message="The run reached its turn limit", usage=usage)


async def run(request: RunRequest, directory: Path) -> AsyncIterator[BrainEvent]:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RunError("auth", "Set OPENAI_API_KEY for the Responses runtime")
    async with connect(request) as session:
        tools = await list_tools(session)
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + key},
            timeout=request.timeout_s,
            follow_redirects=False,
        ) as http:
            async for item in conversation(request, session, tools, http):
                yield item
