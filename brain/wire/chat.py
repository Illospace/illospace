"""The Chat Completions adapter. It knows only the format. This wire sends text only."""

import os

from mcp.types import Tool

from brain.errors import RunError, provider_error
from brain.loop import Turn
from brain.models import RunRequest, event
from brain.tools import Call
from brain.usage import Usage


def function_tools(tools: list[Tool]) -> list[dict]:
    return [{"type": "function", "function": {"name": tool.name, "description": tool.description or "",
                                              "parameters": tool.inputSchema}} for tool in tools]


class Chat:
    records_payer = False
    # Kept from the earlier Chat loop. It has no echo check: the service redacts the stream.
    checks_echo = False
    missing_id = "The model requested a tool outside the run catalog"

    def __init__(self, request: RunRequest, tools: list[Tool]):
        self.catalog = function_tools(tools)
        self.messages = [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.user_message},
        ]

    @staticmethod
    def url(request) -> str:
        # Compose passes an empty value when the operator sets none. Treat empty as not set.
        base = os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        return base.rstrip("/") + "/chat/completions"

    @staticmethod
    def check(request) -> None:
        """A local server can need no key."""
        if not os.getenv("OPENAI_API_KEY") and not os.getenv("OPENAI_BASE_URL"):
            raise RunError("auth", "Set OPENAI_API_KEY for the OpenAI-compatible runtime")

    @staticmethod
    def headers(request) -> dict:
        key = os.getenv("OPENAI_API_KEY")
        return {"Authorization": "Bearer " + key} if key else {}

    def payload(self, request, *, tools_disabled):
        self.text, self.calls, self.stop, self.tools_disabled = [], {}, None, tools_disabled
        payload = {
            "model": request.model,
            "messages": self.messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools_disabled:
            payload["tool_choice"] = "none"
        elif self.catalog:
            payload["tools"] = self.catalog
        if effort := os.getenv("ILLO_BRAIN_OPENAI_COMPAT_REASONING_EFFORT"):
            payload["reasoning_effort"] = effort
        return payload

    def rejected(self, request, payload, status, error) -> bool:
        return False  # This wire has no optional request part to remove.

    def drop_images(self, notice) -> bool:
        return False  # The service removes images before a text-only wire.

    def read(self, request, chunk):
        if "error" in chunk:
            raise provider_error(chunk["error"])
        if chunk.get("usage"):
            yield Usage(chunk["usage"])
        choices = chunk.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        self.stop = choice.get("finish_reason") or self.stop
        delta = choice.get("delta") or {}
        if delta.get("content"):
            self.text.append(delta["content"])
            yield event("text", text=delta["content"])
        for part in delta.get("tool_calls") or []:
            call = self.calls.setdefault(
                part["index"],
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            if part.get("id"):
                call["id"] += part["id"]
            function = part.get("function") or {}
            for key in ("name", "arguments"):
                call["function"][key] += function.get(key) or ""

    def finish(self, request):
        if self.stop not in ("stop", "tool_calls"):
            # Kept from the earlier Chat loop: this limit error has no usage.
            raise RunError("limit" if self.stop == "length" else "runtime",
                           "The model response did not complete")
        answer = "".join(self.text)
        calls = list(self.calls.values())
        if not calls and self.stop != "stop" and not self.tools_disabled:
            raise RunError("runtime", "The model returned an empty tool call")
        if calls:
            self.messages.append({"role": "assistant", "content": answer or None, "tool_calls": calls})
        yield Turn(answer, [
            Call(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in calls
        ])

    def add_result(self, call: Call, text: str) -> None:
        self.messages.append({"role": "tool", "tool_call_id": call.id, "content": text})

    def add_instruction(self, text: str) -> None:
        self.messages.append({"role": "system", "content": text})
