#!/usr/bin/env python3
"""
MCP Brain Server — Exposes brain services as MCP tools.

Instead of pre-loading 5K tokens of context into every agent prompt,
this server lets agents pull context on demand via tool calls.
Only the information the agent actually needs gets loaded.

Usage:
    python3 mcp_brain_server.py                    # stdio transport (for MCP clients)
    python3 mcp_brain_server.py --http --port 9877  # HTTP transport (for testing)

MCP Tools Exposed:
    brain_recall(query, limit?)       — semantic memory search
    brain_guardrails(skill?)          — skill-specific guardrails + recent failures
    brain_skills(task)                — task planning + skill catalog recommendation
    skill_view(name, section?)        — load a skill card/summary/procedure section
    skill_asset(name, path)           — load a versioned skill bundle asset
    brain_encode(content, type, salience?) — record a memory
    vault_inventory()                 — list safe vault metadata for agent reasoning
    brain_vault(key)                  — retrieve a secret from the vault
    vault_secret_prompt(key_name)     — open a guided vault prompt for missing keys

Public release note: internal issue links were removed from source comments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.app.mcp.tools.common import (
    json_safe as _json_safe,
    maybe_await as _maybe_await,
    session_execute as _session_execute,
    session_flush as _session_flush,
)
from brain.app.mcp.tools.encode import brain_encode_tool
from brain.app.mcp.tools.guardrails import brain_guardrails_tool
from brain.app.mcp.tools.recall import (
    add_attribution as _add_attribution,
    brain_recall_tool,
    finalize_recall_response as _finalize_recall_response_impl,
    log_retrieval as _async_log_retrieval_impl,
)
from brain.app.mcp.tools.runtime import runtime_settings_tool
from brain.app.mcp.tools.skills import (
    SKILL_VIEW_SECTIONS,
    brain_skills_tool,
    skill_asset_tool,
    skill_view_tool,
)
from brain.app.mcp.tools.vault import (
    VAULT_PROMPT_CATEGORIES as _VAULT_PROMPT_CATEGORIES,
    brain_vault_tool,
    clean_vault_prompt_text as _clean_vault_prompt_text,
    normalize_vault_key_name as _normalize_vault_key_name,
    safe_vault_secret_summary as _safe_vault_secret_summary,
    vault_inventory_tool,
    vault_prompt_url as _vault_prompt_url,
    vault_secret_prompt_tool,
)
from brain.systems.memory.attention_controller import AttentionController, observe_retrieval
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.db.repositories.memory_write_context import MemoryWriteContext
from brain.platform.db.repositories.memory_visibility import MemoryVisibilityContext

logger = logging.getLogger("mcp_brain")


async def _async_log_retrieval(query: str, results: list) -> None:
    return await _async_log_retrieval_impl(
        query,
        results,
        unit_of_work_cls=UnitOfWork,
        maybe_await=_maybe_await,
        session_flush=_session_flush,
        logger=logger,
    )


async def async_tool_brain_recall(
    query: str,
    limit: int = 3,
    user_id: str | None = None,
    org_id: str | None = None,
    attention_debug: bool = False,
    expand_lazy_load: bool | None = None,
    service_retrieval: bool = False,
) -> dict:
    """Graph-augmented memory search — vector similarity + relationship traversal.

    Multiplayer: pass user_id + org_id for visibility-scoped recall.
    Without viewer context, recall intentionally returns no memories.
    """
    return await brain_recall_tool(
        query,
        limit=limit,
        user_id=user_id,
        org_id=org_id,
        attention_debug=attention_debug,
        expand_lazy_load=expand_lazy_load,
        service_retrieval=service_retrieval,
        unit_of_work_cls=UnitOfWork,
        maybe_await=_maybe_await,
        session_flush=_session_flush,
        visibility_context_cls=MemoryVisibilityContext,
        observe_retrieval_fn=observe_retrieval,
        attention_controller_cls=AttentionController,
        logger=logger,
    )


async def _finalize_recall_response(
    *,
    query: str,
    memories: list[dict],
    limit: int,
    user_id: str | None,
    org_id: str | None,
    attention_debug: bool,
    expand_lazy_load: bool | None,
    service_retrieval: bool = False,
) -> dict:
    return await _finalize_recall_response_impl(
        query=query,
        memories=memories,
        limit=limit,
        user_id=user_id,
        org_id=org_id,
        attention_debug=attention_debug,
        expand_lazy_load=expand_lazy_load,
        service_retrieval=service_retrieval,
        unit_of_work_cls=UnitOfWork,
        maybe_await=_maybe_await,
        session_flush=_session_flush,
        observe_retrieval_fn=observe_retrieval,
        attention_controller_cls=AttentionController,
        logger=logger,
    )


async def async_tool_brain_guardrails(skill: str | None = None) -> dict:
    """Get guardrails: recent failures, high-salience warnings, and skill-specific pitfalls."""
    return await brain_guardrails_tool(
        skill,
        unit_of_work_cls=UnitOfWork,
        maybe_await=_maybe_await,
        session_execute=_session_execute,
    )


async def async_tool_brain_skills(task: str) -> dict:
    """Task planning — recommend skills, guardrails, and strategy for a task.

    Returns the same structure as `skills.py plan` but without the subprocess overhead.
    """
    return await brain_skills_tool(
        task,
        unit_of_work_cls=UnitOfWork,
        maybe_await=_maybe_await,
        session_execute=_session_execute,
        logger=logger,
    )


async def async_tool_skill_view(
    name: str,
    section: str = "procedure",
    max_chars: int = 12000,
) -> dict:
    """Load a specific skill section on demand."""
    return await skill_view_tool(
        name,
        section=section,
        max_chars=max_chars,
        unit_of_work_cls=UnitOfWork,
        maybe_await=_maybe_await,
    )


async def async_tool_skill_asset(
    name: str,
    path: str,
    max_chars: int = 12000,
) -> dict:
    """Load a specific asset from the installed skill bundle version."""
    return await skill_asset_tool(
        name,
        path,
        max_chars=max_chars,
        unit_of_work_cls=UnitOfWork,
        maybe_await=_maybe_await,
    )


async def async_tool_brain_encode(
    content: str,
    memory_type: str = "episode",
    salience: float = 5.0,
    source: str = "agent_run",
    user_id: str | None = None,
    org_id: str | None = None,
    visibility: str = "private",
    conversation_id: str | None = None,
    idea_id: str | None = None,
    run_id: int | str | None = None,
    session_id: str | None = None,
    confidence: float | None = None,
    evidence: dict | None = None,
) -> dict:
    """Encode a new memory into the brain, scoped to the current user."""
    return await brain_encode_tool(
        content,
        memory_type=memory_type,
        salience=salience,
        source=source,
        user_id=user_id,
        org_id=org_id,
        visibility=visibility,
        conversation_id=conversation_id,
        idea_id=idea_id,
        run_id=run_id,
        session_id=session_id,
        confidence=confidence,
        evidence=evidence,
        unit_of_work_cls=UnitOfWork,
        maybe_await=_maybe_await,
        write_context_cls=MemoryWriteContext,
    )


async def tool_brain_vault(
    key: str,
    reason: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    run_id: int | None = None,
    idea_id: str | None = None,
    requested_by: str = "agent",
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict:
    """Retrieve a secret from the vault."""
    return await brain_vault_tool(
        key,
        reason=reason,
        user_id=user_id,
        org_id=org_id,
        run_id=run_id,
        idea_id=idea_id,
        requested_by=requested_by,
        project_slug=project_slug,
        project_slugs=project_slugs,
        target_registry_id=target_registry_id,
        json_safe=_json_safe,
    )


async def tool_vault_inventory(
    category: str | None = None,
    access_level: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """List safe Vault metadata so the agent can choose an existing key."""
    return await vault_inventory_tool(
        category=category,
        access_level=access_level,
        user_id=user_id,
        org_id=org_id,
    )


async def tool_vault_secret_prompt(
    key_name: str,
    description: str | None = None,
    category: str = "api",
    reason: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    run_id: int | None = None,
    idea_id: str | None = None,
    requested_by: str = "agent",
) -> dict:
    """Open a guided Vault form for a user-supplied secret value."""
    return await vault_secret_prompt_tool(
        key_name,
        description=description,
        category=category,
        reason=reason,
        user_id=user_id,
        org_id=org_id,
        run_id=run_id,
        idea_id=idea_id,
        requested_by=requested_by,
    )


async def tool_runtime_settings(
    provider: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """Inspect active runtime/provider/auth settings for the current user."""
    return await runtime_settings_tool(
        provider=provider,
        user_id=user_id,
        org_id=org_id,
        unit_of_work_cls=UnitOfWork,
    )


# ── MCP Protocol Layer ───────────────────────────────────────

TOOLS = {
    "brain_recall": {
        "function": async_tool_brain_recall,
        "description": "Search brain memories semantically. Returns the most relevant memories for the query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for in brain memories"},
                "limit": {"type": "integer", "description": "Max results (default 3)", "default": 3},
                "attention_debug": {"type": "boolean", "description": "Include controller debug breakdown", "default": False},
                "expand_lazy_load": {"type": "boolean", "description": "Fetch deferred lazy-load candidates", "default": False},
            },
            "required": ["query"],
        },
    },
    "brain_guardrails": {
        "function": async_tool_brain_guardrails,
        "description": "Get guardrails: recent failures, high-salience warnings, and skill pitfalls.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "Optional skill name to get specific guardrails for"},
            },
        },
    },
    "brain_skills": {
        "function": async_tool_brain_skills,
        "description": "Plan a task: recommend lightweight skill cards, guardrails, and execution strategy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task description to plan for"},
            },
            "required": ["task"],
        },
    },
    "skill_view": {
        "function": async_tool_skill_view,
        "description": "Progressively load one section of an installed skill, from a small card to a full procedure.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Installed skill name"},
                "section": {
                    "type": "string",
                    "enum": [
                        "card",
                        "summary",
                        "procedure",
                        "pitfalls",
                        "triggers",
                        "guardrails",
                        "graduated_steps",
                        "metadata",
                    ],
                    "default": "procedure",
                },
                "max_chars": {"type": "integer", "description": "Maximum text chars to return", "default": 12000},
            },
            "required": ["name"],
        },
    },
    "skill_asset": {
        "function": async_tool_skill_asset,
        "description": "Progressively load a specific versioned skill bundle asset by path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Installed skill name"},
                "path": {"type": "string", "description": "Relative bundle asset path, for example examples/happy.md"},
                "max_chars": {"type": "integer", "description": "Maximum text chars to return", "default": 12000},
            },
            "required": ["name", "path"],
        },
    },
    "brain_encode": {
        "function": async_tool_brain_encode,
        "description": "Record a new memory (lesson, pattern, fact, or episode) into the brain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Memory content (min 20 chars)"},
                "type": {"type": "string", "enum": ["lesson", "pattern", "fact", "episode"], "default": "episode"},
                "salience": {"type": "number", "description": "Importance 1-10 (default 5)", "default": 5.0},
            },
            "required": ["content"],
        },
    },
    "vault_inventory": {
        "function": tool_vault_inventory,
        "description": (
            "List metadata-only Vault secrets for agent reasoning. Returns key names, descriptions, "
            "categories, and agent access levels, never secret values. Use this before requesting a "
            "credential so the agent can choose an exact existing key or ask the user when ambiguous."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "general",
                        "api",
                        "aws",
                        "auth",
                        "analytics",
                        "database",
                        "messaging",
                        "monitoring",
                        "payments",
                        "service",
                    ],
                    "description": "Optional Vault category filter.",
                },
                "access_level": {
                    "type": "string",
                    "enum": ["available", "ask", "manual"],
                    "description": "Optional agent access level filter.",
                },
            },
        },
    },
    "brain_vault": {
        "function": tool_brain_vault,
        "description": "Request task-scoped access to a secret from the encrypted vault.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Secret key name"},
                "reason": {"type": "string", "description": "Why this active task needs this exact secret"},
            },
            "required": ["key", "reason"],
        },
    },
    "vault_secret_prompt": {
        "function": tool_vault_secret_prompt,
        "description": (
            "Open a guided Vault form for the user to add a missing secret value. Call vault_inventory "
            "first; use this only when no suitable existing secret exists or the user explicitly asked "
            "to add a new key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key_name": {"type": "string", "description": "Secret key name to prefill"},
                "description": {"type": "string", "description": "Vault description to prefill"},
                "category": {
                    "type": "string",
                    "enum": [
                        "general",
                        "api",
                        "aws",
                        "auth",
                        "analytics",
                        "database",
                        "messaging",
                        "monitoring",
                        "payments",
                        "service",
                    ],
                    "default": "api",
                },
                "reason": {"type": "string", "description": "Why this active task needs the secret"},
            },
            "required": ["key_name"],
        },
    },
    "runtime_settings": {
        "function": tool_runtime_settings,
        "description": "Inspect the current runtime provider, auth status, and provider model mappings for the active user/workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["anthropic", "openai"],
                    "description": "Optional provider to focus on; defaults to the effective provider.",
                },
            },
        },
    },
}


async def async_handle_request(request: dict) -> dict:
    """Handle a single MCP JSON-RPC request."""
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "illo-brain", "version": "1.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None  # notification, no response

    if method == "tools/list":
        tools_list = []
        for name, spec in TOOLS.items():
            tools_list.append({
                "name": name,
                "description": spec["description"],
                "inputSchema": spec["inputSchema"],
            })
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tools_list},
        }

    if method == "tools/call":
        params = request.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                    "isError": True,
                },
            }

        try:
            # Map MCP argument names to function parameter names
            func = TOOLS[tool_name]["function"]
            arguments = dict(arguments)
            # Handle the 'type' → 'memory_type' rename for brain_encode
            if tool_name == "brain_encode" and "type" in arguments:
                arguments["memory_type"] = arguments.pop("type")
            result = await _maybe_await(func(**arguments))
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, default=str)}],
                },
            }
        except Exception as e:
            logger.exception(f"Tool {tool_name} failed")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            }

    # Unknown method
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def handle_request(request: dict) -> dict:
    """Sync MCP protocol boundary for stdio/stdlib HTTP transports."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("handle_request cannot run inside an active event loop; await async_handle_request")
    with asyncio.Runner() as runner:
        return runner.run(async_handle_request(request))


def run_stdio():
    """Run as MCP server over stdio (standard MCP transport)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,  # logs go to stderr, protocol goes to stdout
    )
    logger.info("MCP Brain Server starting (stdio transport)")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(request)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON: {line[:100]}")
        except Exception as e:
            logger.exception(f"Request handling failed: {e}")


def run_http(port: int = 9877):
    """Run as HTTP server (for testing/debugging)."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                request = json.loads(body)
                response = handle_request(request)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if response:
                    self.wfile.write(json.dumps(response).encode())
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())

        def log_message(self, format, *args):
            logger.info(format % args)

    logging.basicConfig(level=logging.INFO)
    server = HTTPServer(("127.0.0.1", port), Handler)
    logger.info(f"MCP Brain Server listening on http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP Brain Server")
    parser.add_argument("--http", action="store_true", help="Run as HTTP server instead of stdio")
    parser.add_argument("--port", type=int, default=9877, help="HTTP port (default 9877)")
    args = parser.parse_args()

    if args.http:
        run_http(args.port)
    else:
        run_stdio()
