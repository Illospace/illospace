"""The one agent loop. A wire adapter knows only the provider format; the loop has the rules.

An adapter builds one request and reads its stream. It gives back parts: events (text and
web search), `Usage` reports, and one `Turn` with the answer, the tool calls and the stop.
"""

import json
from asyncio import sleep
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from mcp.types import Tool

from brain.errors import RunError, error_body, identifier
from brain.models import ApiKeyAuth, BrainEvent, RunRequest, event
from brain.outside_data import data_text
from brain.providers import Provider
from brain.tools import (
    SMOKE_TOOL,
    Call,
    SmokeSession,
    check_call,
    check_echo,
    connect,
    execute,
    list_tools,
)
from brain.usage import Usage, add_usage

LIMIT_INSTRUCTION = (
    "You reached the tool limit of this run. Answer now from the results that you have. "
    "Say plainly what you did not check."
)
LIMIT_RESULT = "The tool limit is reached. This tool did not run."
NO_FINAL_ANSWER = 'I reached my step limit before I had an answer. Ask again with a narrower question.'
IMAGES_REFUSED = "\n\nThe images could not be shown. The model provider refused them."
# A wire can see output before it is safe to release it as an event.
OUTPUT = object()
IMAGES_DROPPED = object()


@dataclass
class Turn:
    """The result of one model response. The stop `length` means that the output limit cut it."""

    answer: str = ""
    calls: list[Call] = field(default_factory=list)
    stop: str = "done"
    messages: list[dict] = field(default_factory=list)


def response_log(stop, messages, usage) -> BrainEvent:
    """Give field names and numeric usage for the smoke check."""
    def fields(value, prefix=""):
        if isinstance(value, dict):
            for name, child in value.items():
                path = prefix + name
                yield path
                yield from fields(child, path + ".")
        elif isinstance(value, list):
            for child in value:
                yield from fields(child, prefix)

    counts = {}
    add_usage(counts, usage or {})
    return event("response", stop=identifier(stop), fields=sorted(set(fields(messages))), usage=counts)


async def retry_transient(send):
    """Repeat one request after 2 seconds if it failed before text or a tool call."""
    for attempt in range(2):
        output = False
        try:
            async with aclosing(send()) as parts:
                async for part in parts:
                    output |= part is OUTPUT or (
                        isinstance(part, dict) and part.get("type") in {"text", "tool_call", "web_search"}
                    )
                    yield part
            return
        except RunError as error:
            if error.category != "transient" or attempt or output:
                raise
            await sleep(2)


def last_answer(text: str, usage: dict, *, asked_for_tools: bool = False) -> str:
    """The answer of the last request, which has no tools. Mark the run usage as limited."""
    usage["limited"] = True
    return NO_FINAL_ANSWER if asked_for_tools else text


async def self_looping(request: RunRequest, steps: AsyncIterator[BrainEvent], transcript: list,
                       without_tools: Callable[[str], AsyncIterator[BrainEvent]]):
    """Run an adapter that loops by itself. The adapter gives its events and fills `transcript`.

    At a `limit` error, close `steps` and send one request with no tools through `without_tools`.
    """
    usage = None
    async with aclosing(steps):
        async for item in steps:
            if item["type"] == "error" and item["category"] == "limit":
                usage = item.get("usage") or {}
                break
            yield item
    if usage is None:
        return
    context = data_text("transcript", transcript)
    prompt = request.user_message + "\n\n" + context + "\n\n" + LIMIT_INSTRUCTION
    final = without_tools(prompt)
    async with aclosing(final):
        async for item in final:
            if item["type"] == "final":
                add_usage(usage, item.get("usage") or {})
                item["text"] = last_answer(item["text"], usage)
                item["usage"] = usage
            yield item


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


async def body_chunks(response: httpx.Response, *, streaming: bool) -> AsyncIterator[dict]:
    if streaming:
        async with aclosing(chunks(response)) as stream:
            async for chunk in stream:
                yield chunk
    else:
        await response.aread()
        yield response.json()


def take(part, usage: dict, turn_usage: dict):
    """Add a usage report to the run sum. Stop a response that its output limit cut."""
    if isinstance(part, Usage):
        add_usage(usage, part)
        add_usage(turn_usage, part)
        return None
    if isinstance(part, Turn) and part.stop == "length":
        raise RunError("limit", "The model response did not complete", usage=usage)
    return part


async def send(wire, request: RunRequest, http: httpx.AsyncClient, *, tools_disabled: bool):
    """Send one model request and give the parts of its stream."""
    payload = wire.payload(request, tools_disabled=tools_disabled)
    while True:
        async with http.stream(
            "POST", wire.url(request), json=payload, headers=wire.headers(request)
        ) as response:
            if response.is_error:
                error = wire.error(request, payload, response.status_code, await error_body(response))
                if error is None:
                    continue
                if (error.category == "runtime" and response.status_code in (400, 413, 415, 422)
                        and wire.drop_images(IMAGES_REFUSED)):
                    # The answer matters more than the images. The model tells the person.
                    yield IMAGES_DROPPED
                    continue
                raise error
            response.raise_for_status()
            async with aclosing(body_chunks(response, streaming=payload["stream"])) as stream:
                async for chunk in stream:
                    check_echo(chunk, wire.provider.api_key, request, error=wire.failed(chunk))
                    for part in wire.read(request, chunk):
                        yield part
            return


async def conversation(
    wire_type, request: RunRequest, provider: Provider, session, tools: list[Tool],
    http: httpx.AsyncClient, fallback: Provider | None = None,
) -> AsyncIterator[BrainEvent]:
    names = {tool.name for tool in tools}
    yield event("started", catalog=sorted(names))
    wire = wire_type(request, tools, provider)
    usage = {}
    fallback_notice = ""
    subscription_limit = None
    tool_calls = 0
    limited = False
    images_used = bool(request.images)
    while True:
        turn_usage = {}
        for attempt in range(2):
            try:
                async with aclosing(retry_transient(
                    lambda: send(wire, request, http, tools_disabled=limited)
                )) as parts:
                    async for part in parts:
                        if part is IMAGES_DROPPED:
                            images_used = False
                        elif part is OUTPUT:
                            continue
                        elif (item := take(part, usage, turn_usage)) is not None:
                            yield item
                break
            except Exception as error:
                if isinstance(error, RunError) and error.category == "transient":
                    raise
                if subscription_limit is not None:
                    raise RunError("limit", str(subscription_limit) + " The API fallback failed.",
                                   **subscription_limit.details, fallback_failed=True) from None
                if (
                    not isinstance(error, RunError)
                    or attempt
                    or request.provider_auth.kind != "chatgpt"
                    or fallback is None
                    or error.details.get("code") != "usage_limit_reached"
                ):
                    raise
                # Resume the current turn with its tool results. Never replay an MCP write.
                wire.new_payer(fallback)
                fallback_notice = str(error) + " I used the API key for this request.\n\n"
                subscription_limit = error
                # A failure of the fallback request still reports the limit.
                request = request.model_copy(update={
                    "provider_auth": ApiKeyAuth(), "provider": fallback.name,
                    "fallback_provider": None})
                # The app records the payer at once, so a later failure or timeout keeps it.
                yield event("payer", provider=fallback.name, auth_kind="api_key")
        turn = None
        for part in wire.finish(request):
            if isinstance(part := take(part, usage, turn_usage), Turn):
                turn = part
            elif part is not None:
                yield part
        if request.smoke:
            yield response_log(turn.stop, turn.messages, turn_usage)
            session.observe_response(call.id for call in turn.calls)
        if not turn.calls or limited:
            answer = turn.answer
            if limited:
                answer = last_answer(answer, usage, asked_for_tools=bool(turn.calls))
            final = event("final", text=answer if request.smoke else fallback_notice + answer,
                          usage=usage)
            if request.smoke:
                final.update(smoke_steps=session.completed_steps(), smoke_image_used=images_used,
                             stop=identifier(turn.stop))
            yield final
            return
        # The limit counts tool calls, not model turns. Each call of a batch counts.
        for call in turn.calls:
            check_call(call, names, wire.missing_id)
            if tool_calls >= request.max_tool_calls:
                limited = True
                wire.add_result(call, LIMIT_RESULT)
                continue
            tool_calls += 1
            yield event("tool_call", name=call.name)
            wire.add_result(call, await execute(session, request, call, run_flags=wire.reads_run_flags))
        if limited:
            wire.add_instruction(LIMIT_INSTRUCTION)


async def run(
    wire_type, request: RunRequest, provider: Provider, directory: Path, *,
    fallback: Provider | None = None, transport=None,
) -> AsyncIterator[BrainEvent]:
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx.AsyncClient(
            timeout=request.timeout_s, follow_redirects=False, transport=transport))
        if request.smoke:
            session, tools = SmokeSession(), [SMOKE_TOOL]
        else:
            session = await stack.enter_async_context(connect(request))
            tools = await list_tools(session)
        async for item in conversation(wire_type, request, provider, session, tools, http, fallback):
            yield item
