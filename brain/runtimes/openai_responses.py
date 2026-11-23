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
from brain.models import ApiKeyAuth, BrainEvent, ChatGPTAuth, RunError, RunRequest, event
from brain.runtimes import chatgpt
from brain.runtimes.limits import LIMIT_INSTRUCTION, LIMIT_RESULT, NO_FINAL_ANSWER
from brain.runtimes.openai_compat import add_usage, chunks
from brain.runtimes.provider_errors import error_body, provider_error
from brain.runtimes.tool_results import model_text

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


def build_request(
    request: RunRequest, tools: list[Tool], items: list[dict], *, tools_disabled=False
) -> dict:
    catalog = function_tools(tools)
    names = {tool.name for tool in tools}
    assert all(tool["type"] == "function" and tool["name"] in names for tool in catalog)
    payload = {
        "model": request.model,
        "instructions": request.system_prompt,
        "input": items,
        "tools": catalog,
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": request.max_output_tokens or 4096,
        "parallel_tool_calls": True,
    }
    if request.web_search:
        catalog.append({"type": "web_search"})
    if request.provider_auth.kind == "chatgpt":
        del payload["max_output_tokens"]
        payload["tool_choice"] = "auto"
    if tools_disabled:
        payload["tools"], payload["tool_choice"] = [], "none"
    return payload


def address(request):
    if request.provider_auth.kind == "chatgpt":
        return chatgpt.BASE_URL
    return (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")


def auth_headers(request):
    if isinstance(request.provider_auth, ChatGPTAuth):
        return {
            "Authorization": "Bearer " + request.provider_auth.access_token.get_secret_value(),
            "chatgpt-account-id": request.provider_auth.account_id.get_secret_value(),
            "originator": "illo-brain",
        }
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RunError("auth", "Set OPENAI_API_KEY for the Responses runtime")
    return {"Authorization": "Bearer " + key}


def check_item(item: dict, names: set[str], *, web_search=False) -> None:
    kind = item.get("type")
    if kind == "web_search_call" and web_search:
        return
    if kind not in ("message", "reasoning", "function_call"):
        raise RunError("runtime", "The model returned a tool outside the run catalog")
    if kind == "function_call" and item.get("name") not in names:
        raise RunError("runtime", "The model requested a tool outside the run catalog")


def used_fields(item):
    """Compare the fields that decide a tool call or the answer text.

    Provider metadata can differ between events. A reasoning item is opaque: the provider can
    encode it again in the final event, so only its type takes part in the comparison.
    """
    if item.get("type") == "web_search_call":
        return item
    fields = {key: item.get(key) for key in ("type", "call_id", "name", "arguments")}
    if item.get("type") == "message":
        fields["content"] = [
            {key: part.get(key) for key in ("type", "text", "refusal")}
            for part in item.get("content", [])
        ]
    return fields


def run_effort(request) -> str:
    """The request names the effort of its run. The environment is only the default."""
    if "effort" in request.model_fields_set:
        return request.effort
    return os.getenv("ILLO_BRAIN_EFFORT") or request.effort


def effort_rejected(error: dict) -> bool:
    param = error.get("param")
    # Read the message only to classify this rejection. Never emit or log it.
    message = str(error.get("message", "")).lower()
    return param in ("reasoning", "reasoning.effort") or (
        error.get("code") in ("unsupported_parameter", "unsupported_value")
        and ("reasoning" in message or "effort" in message)
    )


IMAGES_REFUSED = "\n\nThe images could not be shown. The model provider refused them."


def drop_images(items: list[dict]) -> bool:
    """Change the first user turn to text only. The payload holds the same list."""
    content = items[0].get("content") if items else None
    if not isinstance(content, list) or not any(p.get("type") == "input_image" for p in content):
        return False
    text = "".join(p.get("text", "") for p in content if p.get("type") == "input_text")
    items[0]["content"] = text + IMAGES_REFUSED
    return True


WEB_UNAVAILABLE = "\n\nWeb search is not available in this run. Use `web_fetch` on addresses that you know."


def drop_web_search(request, payload, items):
    request.web_search = False
    payload["tools"] = [tool for tool in payload["tools"] if tool["type"] != "web_search"]
    content = items[0]["content"]
    if isinstance(content, list):
        content.append({"type": "input_text", "text": WEB_UNAVAILABLE})
    else:
        items[0]["content"] += WEB_UNAVAILABLE


def web_search_rejected(error):
    param = str(error.get("param", ""))
    message = str(error.get("message", "")).lower()
    return "web_search" in message or "web search" in message or param.startswith("tools")


async def response_chunks(request, tools, items, http, *, tools_disabled=False):
    base = address(request)
    cache_key = (base, request.model)
    payload = build_request(request, tools, items, tools_disabled=tools_disabled)
    if cache_key not in _without_effort:
        payload["reasoning"] = {"effort": run_effort(request)}
    effort_retry = image_retry = web_retry = True
    while True:
        async with http.stream(
            "POST", base + "/responses", json=payload, headers=auth_headers(request)
        ) as response:
            if response.is_error:
                error = await error_body(response)
                limited = (
                    chatgpt.limit_error(error, response.status_code)
                    if request.provider_auth.kind == "chatgpt"
                    else None
                )
                if limited:
                    raise limited
                if (
                    response.status_code == 400
                    and effort_retry
                    and "reasoning" in payload
                    and effort_rejected(error)
                ):
                    effort_retry = False
                    _without_effort.add(cache_key)
                    del payload["reasoning"]
                    continue
                if (400 <= response.status_code < 500 and web_retry and request.web_search
                        and web_search_rejected(error)):
                    web_retry = False
                    drop_web_search(request, payload, items)
                    continue
                if response.status_code in (400, 413, 415, 422) and image_retry and drop_images(items):
                    # The answer matters more than the images. The model tells the person.
                    image_retry = False
                    continue
                raise provider_error(error, response.status_code)
            response.raise_for_status()
            async for chunk in chunks(response):
                # Reject credential echoes before a function argument can reach MCP.
                encoded = json.dumps(chunk)
                if any(image.data.get_secret_value() in encoded for image in request.images):
                    raise RunError("runtime", "The provider returned private image data")
                values = [os.getenv("OPENAI_API_KEY")]
                if isinstance(request.provider_auth, ChatGPTAuth):
                    values.append(request.provider_auth.access_token.get_secret_value())
                if chunk.get("type") not in {"error", "response.failed"} and any(
                    value and json.dumps(value)[1:-1] in encoded for value in values
                ):
                    raise RunError("runtime", "The provider returned a credential")
                yield chunk
            return


async def conversation(
    request: RunRequest, session: ClientSession, tools: list[Tool], http: httpx.AsyncClient
) -> AsyncIterator[BrainEvent]:
    names = {tool.name for tool in tools}
    yield event("started", catalog=sorted(names))
    content = [{"type": "input_text", "text": request.user_message}]
    content.extend(
        {"type": "input_image", "image_url": f"data:{image.mime};base64,{image.data.get_secret_value()}"}
        for image in request.images
    )
    items = [{"role": "user", "content": content if request.images else request.user_message}]
    usage = {}
    fallback_notice = ""
    subscription_limit = None
    tool_calls = 0
    limited = False
    searches_seen = set()
    while True:
        completed = None
        for attempt in range(2):
            text_deltas = []
            done_items = {}
            try:
                async for chunk in response_chunks(
                    request, tools, items, http, tools_disabled=limited
                ):
                    kind = chunk.get("type")
                    if kind == "response.output_text.delta":
                        text_deltas.append(chunk["delta"])
                    elif kind in ("response.output_item.added", "response.output_item.done"):
                        check_item(chunk["item"], names, web_search=request.web_search)
                        search = chunk["item"]
                        if search.get("type") == "web_search_call" and search["id"] not in searches_seen:
                            searches_seen.add(search["id"])
                            yield event("web_search", call_id=search["id"])
                        if kind == "response.output_item.done":
                            done_items[chunk["output_index"]] = chunk["item"]
                    elif kind in ("error", "response.failed"):
                        failed = chunk.get("response") or {}
                        add_usage(usage, failed.get("usage") or {})
                        error = (
                            failed.get("error")
                            if kind == "response.failed"
                            else chunk.get("error", chunk)
                        )
                        limited = (
                            chatgpt.limit_error(error)
                            if request.provider_auth.kind == "chatgpt"
                            else None
                        )
                        raise limited or provider_error(error)
                    elif kind == "response.incomplete":
                        add_usage(usage, (chunk.get("response") or {}).get("usage") or {})
                        raise RunError("limit", "The model response did not complete", usage=usage)
                    elif kind == "response.completed":
                        completed = chunk["response"]
                break
            except Exception as error:
                if subscription_limit is not None:
                    raise RunError(
                        "limit",
                        str(subscription_limit) + " The API fallback failed.",
                        **subscription_limit.details,
                        api_key_fallback_failed=True,
                    ) from None
                if (
                    not isinstance(error, RunError)
                    or attempt
                    or request.provider_auth.kind != "chatgpt"
                    or not request.api_key_fallback
                    or error.details.get("code") != "usage_limit_reached"
                ):
                    raise
                items = [
                    {key: value for key, value in item.items() if key != "id" or item.get("type") == "web_search_call"}
                    for item in items if item.get("type") != "reasoning"
                ]
                # Resume the current turn with its tool results. Never replay an MCP write.
                fallback_notice = str(error) + " I used the API key for this request.\n\n"
                subscription_limit = error
                request = request.model_copy(
                    update={"provider_auth": ApiKeyAuth(), "api_key_fallback": False}
                )
                # The next request checks for an API key. Failure still reports the subscription limit.
        if completed is None or completed.get("status") != "completed":
            raise RunError("runtime", "The model stream ended without a complete response")
        for delta in text_deltas:
            yield event("text", text=delta)
        add_usage(usage, completed.get("usage") or {})
        observed = [done_items[index] for index in sorted(done_items)]
        output = completed.get("output") or observed
        if observed and [used_fields(i) for i in output] != [used_fields(i) for i in observed]:
            # Name the fields only. Their values can hold private text.
            differs = sorted({
                f"{a.get('type')}.{key}"
                for a, b in zip(map(used_fields, output), map(used_fields, observed), strict=False)
                for key in a.keys() | b.keys() if a.get(key) != b.get(key)
            }) or ["count"]
            raise RunError(
                "runtime",
                "The model returned conflicting output items: " + ", ".join(differs)[:80],
            )
        for item in output:
            check_item(item, names, web_search=request.web_search)
            if item.get("type") == "web_search_call" and item["id"] not in searches_seen:
                searches_seen.add(item["id"])
                yield event("web_search", call_id=item["id"])
        calls = [item for item in output if item["type"] == "function_call"]
        if not calls or limited:
            answer = "".join(
                part.get("text", "")
                if part.get("type") == "output_text"
                else part.get("refusal", "The model refused this request.")
                for item in output
                if item["type"] == "message"
                for part in item.get("content", [])
                if part.get("type") in {"output_text", "refusal"}
            )
            if limited:
                usage["limited"] = True
                answer = NO_FINAL_ANSWER if calls else answer
            yield event(
                "final",
                text=fallback_notice + answer,
                # The sign-in that made the last model request. After a fallback this is the
                # API key. Add it after the provider counts, so a provider field cannot change it.
                usage={**usage, "auth_kind": request.provider_auth.kind},
                **({"auth_kind": request.provider_auth.kind} if request.smoke else {}),
            )
            return
        # Replay all output, including encrypted reasoning, without a response ID.
        items.extend(output)
        for call in calls:
            if tool_calls >= request.max_turns:
                limited = True
                if not call.get("call_id"):
                    raise RunError("runtime", "The model returned an invalid function call")
                items.append({"type": "function_call_output", "call_id": call["call_id"],
                              "output": LIMIT_RESULT})
                continue
            tool_calls += 1
            if not call.get("call_id"):
                raise RunError("runtime", "The model returned an invalid function call")
            yield event("tool_call", name=call["name"])
            try:
                arguments = json.loads(call["arguments"].strip() or "{}")
                if not isinstance(arguments, dict):
                    raise TypeError
            except (ValueError, TypeError, KeyError, AttributeError):
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
            flags = result.get("structuredContent", {}).get("run_flags", {})
            # Missing metadata must not reopen a run after a lost tool response.
            if flags.get("customer_read") is not False or flags.get("personal_read") is not False:
                request.web_search = False
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": model_text(result),
                }
            )

        if limited:
            items.append({"role": "developer", "content": LIMIT_INSTRUCTION})


async def run(request: RunRequest, directory: Path, *, transport=None) -> AsyncIterator[BrainEvent]:
    auth_headers(request)
    if request.smoke:
        from brain.smoke import TOOLS, SmokeSession

        session = SmokeSession()
        async with httpx.AsyncClient(
            timeout=request.timeout_s, follow_redirects=False, transport=transport
        ) as http:
            async for item in conversation(request, session, TOOLS, http):
                if item["type"] == "final":
                    item["tool_round_trip"] = session.calls == 1
                yield item
        return
    async with connect(request) as session:
        tools = await list_tools(session)
        async with httpx.AsyncClient(
            timeout=request.timeout_s,
            follow_redirects=False,
            transport=transport,
        ) as http:
            async for item in conversation(request, session, tools, http):
                yield item
