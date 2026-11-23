"""The Claude Agent SDK adapter. It loops by itself, because the SDK runs the tools.

The SDK starts the Claude Code CLI with its built-in tools off. It sees only the Illo MCP
server. The SDK counts its own turns, so the adapter gives it `max_tool_calls + 1`.
"""

import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import asdict, replace
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from brain.errors import RunError
from brain.loop import self_looping
from brain.models import BrainEvent, RunRequest, event
from brain.providers import Provider
from brain.tools import SDK_PREFIX, SDK_SERVER, check_sdk_tool


def options(request: RunRequest, provider: Provider, directory: Path) -> ClaudeAgentOptions:
    work = directory / "work"
    state = directory / "state"
    cache = directory / "cache"
    tmp = directory / "tmp"
    for path in (work, state, cache, tmp):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {
        "CLAUDE_CONFIG_DIR": str(state),
        "XDG_CACHE_HOME": str(cache),
        "TMPDIR": str(tmp),
        "CLAUDE_CODE_TMPDIR": str(tmp),
        "HOME": str(state),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "DISABLE_AUTOUPDATER": "1",
        "ANTHROPIC_API_KEY": provider.api_key,
        **({"ANTHROPIC_BASE_URL": provider.base_url} if provider.base_url else {}),
    }
    return ClaudeAgentOptions(
        cwd=str(work),
        system_prompt=request.system_prompt,
        tools=[],
        # The SDK forwards this server wildcard to --allowedTools unchanged.
        # It pre-approves every tool of illo. tools=[] removes built-in tools.
        allowed_tools=[SDK_PREFIX + "*"],
        permission_mode="dontAsk",
        mcp_servers={
            SDK_SERVER: {
                "type": "http",
                "url": str(request.mcp_url),
                "headers": {"Authorization": "Bearer " + request.mcp_token.get_secret_value()},
            }
        },
        strict_mcp_config=True,
        setting_sources=[],
        plugins=[],
        skills=[],
        max_turns=request.max_tool_calls + 1,
        model=request.model,
        effort=request.effort,
        include_partial_messages=True,
        enable_file_checkpointing=False,
        settings=json.dumps({"disableAllHooks": True, "enableAllProjectMcpServers": False,
                             "disableClaudeAiConnectors": True}),
        env=env,
        extra_args={"bare": None, "no-session-persistence": None, "disable-slash-commands": None,
                    "no-chrome": None},
        stderr=lambda _: None,
    )


async def translate(client: ClaudeSDKClient, transcript=None) -> AsyncIterator[BrainEvent]:
    seen = set()
    text_parts = []
    partial = False
    async for message in client.receive_response():
        name = type(message).__name__
        if transcript is not None and name in {"AssistantMessage", "UserMessage"}:
            content = message.content
            blocks = ([{"text": content}] if isinstance(content, str) else [
                {"type": type(block).__name__, **asdict(block)}
                for block in content
                if type(block).__name__ in {"TextBlock", "ToolUseBlock", "ToolResultBlock"}
            ])
            transcript.append({"role": name, "content": blocks})
        if name == "SystemMessage" and message.subtype == "init":
            catalog = message.data.get("tools", [])
            for tool in catalog:
                check_sdk_tool(tool)
            yield event("started", catalog=catalog, model=message.data.get("model"))
        elif name == "StreamEvent":
            data = message.event
            block = data.get("content_block", {})
            if data.get("type") == "content_block_start" and block.get("type") == "tool_use":
                check_sdk_tool(block["name"])
                seen.add(block["id"])
                yield event("tool_call", name=block["name"])
            elif (
                data.get("type") == "content_block_delta"
                and data.get("delta", {}).get("type") == "text_delta"
            ):
                partial = True
                yield event("text", text=data["delta"]["text"])
        elif name == "AssistantMessage":
            if message.error:
                category = "auth" if message.error == "authentication_failed" else "runtime"
                raise RunError(category, "The model provider rejected the request")
            for block in message.content:
                if type(block).__name__ == "ToolUseBlock" and block.id not in seen:
                    check_sdk_tool(block.name)
                    seen.add(block.id)
                    yield event("tool_call", name=block.name)
                if type(block).__name__ == "TextBlock":
                    text_parts.append(block.text)
                    if not partial:
                        yield event("text", text=block.text)
            partial = False
        elif name == "ResultMessage":
            if message.is_error:
                category = "limit" if "max_turns" in message.subtype else "runtime"
                if message.api_error_status in (401, 403):
                    category = "auth"
                yield event(
                    "error", category=category, message="The model run failed", usage=message.usage
                )
            else:
                yield event(
                    "final",
                    text=message.result or "\n".join(text_parts),
                    usage=message.usage or {},
                    turns=message.num_turns,
                )
            return


async def run(request: RunRequest, provider: Provider, directory: Path, *,
              fallback: Provider | None = None) -> AsyncIterator[BrainEvent]:
    transcript = []

    async def steps():
        async with ClaudeSDKClient(options=options(request, provider, directory)) as client:
            await client.query(request.user_message)
            async for item in translate(client, transcript):
                yield item

    async def without_tools(prompt):
        # The SDK cannot remove MCP tools from an active client. Use one client with no tools.
        final = replace(options(request, provider, directory), mcp_servers={}, allowed_tools=[],
                        max_turns=1)
        async with ClaudeSDKClient(options=final) as client:
            await client.query(prompt)
            async for item in translate(client):
                yield item

    events = self_looping(request, steps(), transcript, without_tools)
    async with aclosing(events):
        async for item in events:
            yield item
