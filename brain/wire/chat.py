"""The Chat Completions adapter.

A non-streaming assistant message is kept verbatim. In a stream, group tool calls
by index, in index order. Join message content, reasoning fragments (`reasoning`,
`reasoning_content`), function names, and function arguments in delta order. All
other strings keep the first non-empty value. A later different non-empty value
is a runtime error. Merge nested objects key by key, including unknown fields at
the message and call levels. Null values yield to actual values. Other values
must agree when repeated. The call index belongs to the stream and is not part
of the assistant message.
"""

from mcp.types import Tool

from brain.errors import RunError, provider_error
from brain.loop import OUTPUT, Turn
from brain.models import RunRequest, event
from brain.providers import Provider
from brain.tools import Call, check_echo
from brain.usage import Usage


def function_tools(tools: list[Tool]) -> list[dict]:
    return [{"type": "function", "function": {"name": tool.name, "description": tool.description or "",
                                              "parameters": tool.inputSchema}} for tool in tools]


def merge(target: dict, part: dict, joined: set[tuple], path: tuple = ()) -> None:
    for key, value in part.items():
        field = (*path, key)
        previous = target.get(key)
        if previous is None:
            target[key] = value
        elif value is None:
            continue
        elif isinstance(previous, dict) and isinstance(value, dict):
            merge(previous, value, joined, field)
        elif isinstance(previous, str) and isinstance(value, str):
            if field in joined or not previous:
                target[key] = previous + value
            elif value and previous != value:
                raise RunError("runtime", "The model returned conflicting message fields")
        elif type(previous) is not type(value) or previous != value:
            raise RunError("runtime", "The model returned conflicting message fields")


def chat_error(error, status: int | None = None) -> RunError:
    error = error if isinstance(error, dict) else {}
    if status is None and type(error.get("code")) is int:
        status = error["code"]
    reason = error.get("status")
    details = error.get("details")
    key_error = reason == "INVALID_ARGUMENT" and (
        error.get("param") == "api_key" or any(
            isinstance(detail, dict) and detail.get("reason") in ("API_KEY_INVALID", "API_KEY_EXPIRED")
            for detail in (details if isinstance(details, list) else [])
        )
    )
    if status == 429 or reason == "RESOURCE_EXHAUSTED":
        category = "limit"
    elif status in (401, 403) or (status == 400 and key_error):
        category = "config"
    elif status in (500, 503) or (status is None and reason in ("INTERNAL", "UNAVAILABLE")):
        category = "transient"
    else:
        category = "runtime"
    return provider_error(error, status, category=category)


class Chat:
    reads_run_flags = False
    missing_id = "The model requested a tool outside the run catalog"

    def __init__(self, request: RunRequest, tools: list[Tool], provider: Provider):
        self.provider = provider
        self.catalog = function_tools(tools)
        content = [{"type": "text", "text": request.user_message}] + [
            {"type": "image_url", "image_url": {"url": f"data:{image.mime};base64,"
                                              f"{image.data.get_secret_value()}"}}
            for image in request.images
        ]
        self.messages = [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": content if request.images else request.user_message},
        ]

    def url(self, request) -> str:
        return self.provider.base_url.rstrip("/") + "/chat/completions"

    def headers(self, request) -> dict:
        return {"Authorization": "Bearer " + self.provider.api_key}

    def payload(self, request, *, tools_disabled):
        self.text, self.calls, self.message, self.stop = [], {}, {}, None
        self.tools_disabled = tools_disabled
        self.streaming = self.provider.stream_tools
        payload = {"model": request.model, "messages": self.messages, "stream": self.streaming}
        if self.streaming:
            payload["stream_options"] = {"include_usage": True}
        if tools_disabled:
            payload["tool_choice"] = "none"
        elif self.catalog:
            payload["tools"] = self.catalog
        if request.effort:
            payload["reasoning_effort"] = request.effort
        return payload

    @staticmethod
    def failed(chunk) -> bool:
        return "error" in chunk

    @staticmethod
    def error(request, payload, status, error):
        return chat_error(error, status)

    def drop_images(self, notice) -> bool:
        user = self.messages[1]
        if not isinstance(user["content"], list):
            return False
        user["content"] = user["content"][0]["text"] + notice
        return True

    def read(self, request, chunk):
        if self.failed(chunk):
            raise chat_error(chunk["error"])
        if chunk.get("usage"):
            yield Usage(chunk["usage"])
        choices = chunk.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        self.stop = choice.get("finish_reason") or self.stop
        part = (choice.get("delta" if self.streaming else "message") or {})
        if part.get("content") or part.get("tool_calls"):
            yield OUTPUT
        if part.get("content"):
            self.text.append(part["content"])
        if not self.streaming:
            self.message = part
            return
        merge(
            self.message,
            {k: v for k, v in part.items() if k != "tool_calls"},
            {("content",), ("reasoning",), ("reasoning_content",)},
        )
        for call in part.get("tool_calls") or []:
            index = call.get("index")
            if type(index) is not int or index < 0:
                raise RunError("runtime", "The model returned a tool call without a valid index")
            merge(self.calls.setdefault(index, {}), {k: v for k, v in call.items() if k != "index"},
                  {("function", "name"), ("function", "arguments")})

    def finish(self, request):
        if self.streaming:
            self.message.setdefault("role", "assistant")
            if self.calls:
                self.message["tool_calls"] = [self.calls[index] for index in sorted(self.calls)]
        # A credential or image can span several deltas. Check the assembled message too.
        check_echo(self.message, self.provider.api_key, request)
        for part in self.text:
            yield event("text", text=part)
        if self.stop not in ("stop", "tool_calls"):
            raise RunError("limit" if self.stop == "length" else "runtime",
                           "The model response did not complete")
        calls = self.message.get("tool_calls") or []
        if not calls and self.stop != "stop" and not self.tools_disabled:
            raise RunError("runtime", "The model returned an empty tool call")
        if calls:
            self.messages.append(self.message)
        yield Turn(self.message.get("content") or "", [
            Call(c.get("id"), (c.get("function") or {}).get("name"),
                 (c.get("function") or {}).get("arguments")) for c in calls
        ], stop=self.stop, messages=[self.message])

    def add_result(self, call: Call, text: str) -> None:
        self.messages.append({"role": "tool", "tool_call_id": call.id, "content": text})

    def add_instruction(self, text: str) -> None:
        self.messages.append({"role": "system", "content": text})
