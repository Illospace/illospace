"""Reuse the Claude options and event translation verified by the brain test."""

import json
import os
from collections.abc import AsyncIterator
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlparse

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from brain.models import BrainEvent, RunError, RunRequest, event
from brain.runtimes.limits import LIMIT_INSTRUCTION
from brain.runtimes.openai_compat import add_usage
from brain.runtimes.outside_data import data_text


def has_auth() -> bool:
    local = urlparse(os.getenv("ANTHROPIC_BASE_URL", "")).hostname in (
        "127.0.0.1",
        "localhost",
        "::1",
    )
    return bool(
        os.getenv("ANTHROPIC_API_KEY")
        or (
            os.getenv("ILLO_BRAIN_USE_HOST_PROVIDER") == "1"
            and local
            and os.getenv("ANTHROPIC_AUTH_TOKEN")
        )
    )


def options(request: RunRequest, directory: Path) -> ClaudeAgentOptions:
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
    }
    if not os.getenv("ANTHROPIC_API_KEY") and has_auth():
        env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_AUTH_TOKEN"]
    return ClaudeAgentOptions(
        cwd=str(work),
        system_prompt=request.system_prompt,
        tools=[],
        # The SDK forwards this server wildcard to --allowedTools unchanged.
        # It pre-approves every tool of illo. tools=[] removes built-in tools.
        allowed_tools=["mcp__illo__*"],
        permission_mode="dontAsk",
        mcp_servers={
            "illo": {
                "type": "http",
                "url": str(request.mcp_url),
                "headers": {"Authorization": "Bearer " + request.mcp_token.get_secret_value()},
            }
        },
        strict_mcp_config=True,
        setting_sources=[],
        plugins=[],
        skills=[],
        max_turns=request.max_turns + 1,
        model=request.model,
        effort=os.getenv("ILLO_BRAIN_EFFORT", "low"),
        include_partial_messages=True,
        enable_file_checkpointing=False,
        settings=json.dumps(
            {
                "disableAllHooks": True,
                "enableAllProjectMcpServers": False,
                "disableClaudeAiConnectors": True,
            }
        ),
        env=env,
        extra_args={
            "bare": None,
            "no-session-persistence": None,
            "disable-slash-commands": None,
            "no-chrome": None,
        },
        stderr=lambda _: None,
    )


def check_tool(name: str) -> None:
    if not name.startswith("mcp__illo__"):
        raise RunError("runtime", "The runtime exposed a tool outside Illo")


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
                check_tool(tool)
            yield event("started", catalog=catalog, model=message.data.get("model"))
        elif name == "StreamEvent":
            data = message.event
            block = data.get("content_block", {})
            if data.get("type") == "content_block_start" and block.get("type") == "tool_use":
                check_tool(block["name"])
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
                    check_tool(block.name)
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


async def run(request: RunRequest, directory: Path) -> AsyncIterator[BrainEvent]:
    if not has_auth():
        raise RunError("auth", "Set ANTHROPIC_API_KEY for the Claude runtime")
    transcript = []
    limited_usage = None
    async with ClaudeSDKClient(options=options(request, directory)) as client:
        await client.query(request.user_message)
        async for item in translate(client, transcript):
            if item["type"] == "error" and item["category"] == "limit":
                limited_usage = item.get("usage") or {}
                break
            yield item
    if limited_usage is None:
        return
    # The SDK cannot remove MCP tools from an active client. Use one client with no tools.
    final_options = replace(options(request, directory), mcp_servers={}, allowed_tools=[], max_turns=1)
    context = data_text("transcript", transcript)
    async with ClaudeSDKClient(options=final_options) as client:
        await client.query(request.user_message + "\n\n" + context + "\n\n" + LIMIT_INSTRUCTION)
        async for item in translate(client):
            if item["type"] == "final":
                add_usage(limited_usage, item.get("usage") or {})
                item["usage"] = {**limited_usage, "limited": True}
            yield item
