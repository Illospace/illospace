"""The Responses adapter: the ChatGPT backend and the OpenAI API. It knows only the format."""

from datetime import UTC, datetime

from mcp.types import Tool

from brain.errors import RunError, provider_error
from brain.loop import Turn
from brain.models import RunRequest, event
from brain.providers import Provider
from brain.tools import Call
from brain.usage import Usage

WEB_UNAVAILABLE = "\n\nWeb search is not available in this run. Use `web_fetch` on addresses that you know."

# Workers have one run. This cache lasts only as long as the worker process.
_without_effort: set[tuple[str, str]] = set()


def subscription_limit(error, status=None) -> RunError | None:
    if not isinstance(error, dict):
        return None
    if error.get("type") != "usage_limit_reached" and error.get("code") != "usage_limit_reached":
        return None
    details = {"code": "usage_limit_reached", "provider_type": "usage_limit_reached"}
    if status is not None:
        details["http_status"] = status
    reset = error.get("resets_at")
    message = "Your ChatGPT limit is reached."
    # Never copy a provider message or an unchecked reset string into an error.
    if type(reset) is int and 0 < reset < 253402300800:
        details["resets_at"] = reset
        message += f" It resets {datetime.fromtimestamp(reset, UTC):%Y-%m-%d %H:%M UTC}."
    return RunError("limit", message, **details)


def function_tools(tools: list[Tool]) -> list[dict]:
    # Keep the MCP schema, including optional arguments, unchanged.
    return [{"type": "function", "name": tool.name, "description": tool.description or "",
             "parameters": tool.inputSchema, "strict": False} for tool in tools]


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
        fields["content"] = [{key: part.get(key) for key in ("type", "text", "refusal")}
                             for part in item.get("content", [])]
    return fields


def effort_rejected(error: dict) -> bool:
    param = error.get("param")
    # Read the message only to classify this rejection. Never emit or log it.
    message = str(error.get("message", "")).lower()
    return param in ("reasoning", "reasoning.effort") or (
        error.get("code") in ("unsupported_parameter", "unsupported_value")
        and ("reasoning" in message or "effort" in message)
    )


def web_search_rejected(error: dict) -> bool:
    param = str(error.get("param", ""))
    message = str(error.get("message", "")).lower()
    return "web_search" in message or "web search" in message or param.startswith("tools")


def arguments(call: dict):
    """Empty arguments mean no arguments on this wire."""
    value = call.get("arguments")
    return (value.strip() or "{}") if isinstance(value, str) else value


class Responses:
    records_payer = True
    checks_echo = True
    reads_run_flags = True
    missing_id = "The model returned an invalid function call"

    def __init__(self, request: RunRequest, tools: list[Tool], provider: Provider):
        self.provider = provider
        self.tools = tools
        self.names = {tool.name for tool in tools}
        content = [{"type": "input_text", "text": request.user_message}] + [
            {"type": "input_image", "image_url": f"data:{i.mime};base64,{i.data.get_secret_value()}"}
            for i in request.images]
        self.items = [{"role": "user", "content": content if request.images else request.user_message}]
        self.searches = set()

    def url(self, request) -> str:
        return self.provider.base_url + "/responses"

    def headers(self, request) -> dict:
        auth = request.provider_auth
        if auth.kind == "chatgpt":
            return {"Authorization": "Bearer " + auth.access_token.get_secret_value(),
                    "chatgpt-account-id": auth.account_id.get_secret_value(),
                    "originator": "illo-brain"}
        return {"Authorization": "Bearer " + self.provider.api_key}

    def payload(self, request, *, tools_disabled):
        self.deltas, self.done, self.completed = [], {}, None
        self.retries = {"effort", "web_search"}
        payload = build_request(request, self.tools, self.items, tools_disabled=tools_disabled)
        if request.effort and (self.provider.base_url, request.model) not in _without_effort:
            payload["reasoning"] = {"effort": request.effort}
        return payload

    def rejected(self, request, payload, status, error) -> bool:
        """Raise a final rejection, or remove a rejected optional part and give True."""
        if request.provider_auth.kind == "chatgpt" and (limit := subscription_limit(error, status)):
            raise limit
        if (status == 400 and "effort" in self.retries and "reasoning" in payload
                and effort_rejected(error)):
            self.retries.discard("effort")
            _without_effort.add((self.provider.base_url, request.model))
            del payload["reasoning"]
            return True
        if (400 <= status < 500 and "web_search" in self.retries and request.web_search
                and web_search_rejected(error)):
            self.retries.discard("web_search")
            request.web_search = False
            payload["tools"] = [tool for tool in payload["tools"] if tool["type"] != "web_search"]
            content = self.items[0]["content"]
            if isinstance(content, list):
                content.append({"type": "input_text", "text": WEB_UNAVAILABLE})
            else:
                self.items[0]["content"] += WEB_UNAVAILABLE
            return True
        return False

    def drop_images(self, notice) -> bool:
        """Change the first user turn to text only. The payload holds the same list."""
        content = self.items[0].get("content") if self.items else None
        if not isinstance(content, list) or not any(p.get("type") == "input_image" for p in content):
            return False
        text = "".join(p.get("text", "") for p in content if p.get("type") == "input_text")
        self.items[0]["content"] = text + notice
        return True

    @staticmethod
    def failed(chunk) -> bool:
        return chunk.get("type") in ("error", "response.failed")

    def searched(self, request, item):
        check_item(item, self.names, web_search=request.web_search)
        if item.get("type") == "web_search_call" and item["id"] not in self.searches:
            self.searches.add(item["id"])
            yield event("web_search", call_id=item["id"])

    def read(self, request, chunk):
        """Text waits until the response completes, so a failed attempt shows no text."""
        kind = chunk.get("type")
        if kind == "response.output_text.delta":
            self.deltas.append(chunk["delta"])
        elif kind in ("response.output_item.added", "response.output_item.done"):
            yield from self.searched(request, chunk["item"])
            if kind == "response.output_item.done":
                self.done[chunk["output_index"]] = chunk["item"]
        elif kind in ("error", "response.failed"):
            failed = chunk.get("response") or {}
            yield Usage(failed.get("usage") or {})
            error = failed.get("error") if kind == "response.failed" else chunk.get("error", chunk)
            limit = subscription_limit(error) if request.provider_auth.kind == "chatgpt" else None
            raise limit or provider_error(error)
        elif kind == "response.incomplete":
            yield Usage((chunk.get("response") or {}).get("usage") or {})
            yield Turn(stop="length")
        elif kind == "response.completed":
            self.completed = chunk["response"]

    def finish(self, request):
        completed = self.completed
        if completed is None or completed.get("status") != "completed":
            raise RunError("runtime", "The model stream ended without a complete response")
        for delta in self.deltas:
            yield event("text", text=delta)
        yield Usage(completed.get("usage") or {})
        observed = [self.done[index] for index in sorted(self.done)]
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
            yield from self.searched(request, item)
        calls = [item for item in output if item["type"] == "function_call"]
        if calls:
            # Replay all output, including encrypted reasoning, without a response ID.
            self.items.extend(output)
        answer = "".join(
            part.get("text", "")
            if part.get("type") == "output_text"
            else part.get("refusal", "The model refused this request.")
            for item in output
            if item["type"] == "message"
            for part in item.get("content", [])
            if part.get("type") in {"output_text", "refusal"}
        )
        yield Turn(answer, [Call(c.get("call_id"), c["name"], arguments(c)) for c in calls])

    def add_result(self, call: Call, text: str) -> None:
        self.items.append({"type": "function_call_output", "call_id": call.id, "output": text})

    def add_instruction(self, text: str) -> None:
        self.items.append({"role": "developer", "content": text})

    def new_payer(self, provider: Provider) -> None:
        """Another sign-in cannot read the encrypted reasoning or the item IDs of the first."""
        self.provider = provider
        self.items = [
            {key: value for key, value in item.items()
             if key != "id" or item.get("type") == "web_search_call"}
            for item in self.items if item.get("type") != "reasoning"
        ]
