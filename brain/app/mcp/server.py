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
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from sqlalchemy import text

from brain.app.mcp.tools.common import (
    json_safe as _json_safe,
    maybe_await as _maybe_await,
    session_execute as _session_execute,
    session_flush as _session_flush,
)
from brain.app.mcp.tools.encode import brain_encode_tool
from brain.app.mcp.tools.recall import (
    add_attribution as _add_attribution,
    brain_recall_tool,
    finalize_recall_response as _finalize_recall_response_impl,
    log_retrieval as _async_log_retrieval_impl,
)
from brain.app.mcp.tools.skills import (
    SKILL_VIEW_SECTIONS,
    brain_skills_tool,
    skill_asset_tool,
    skill_view_tool,
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
    result = {"guardrails": [], "warnings": [], "pitfalls": []}

    async with UnitOfWork() as uow:
        # Recent failures (last 7 days)
        rows_result = await _session_execute(uow.session, text("""
            SELECT s.name, se.outcome_details, se.error_analysis, se.started_at
            FROM skill_executions se
            JOIN skills s ON s.id = se.skill_id
            WHERE se.outcome = 'failure'
              AND se.started_at > NOW() - INTERVAL '7 days'
            ORDER BY se.started_at DESC
            LIMIT 5
        """))
        rows = rows_result.mappings().all()
        for row in rows:
            result["guardrails"].append({
                "skill": row["name"],
                "failure": (row["error_analysis"] or row["outcome_details"] or "Unknown")[:200],
                "when": str(row["started_at"]),
            })

        # High-salience warnings (lessons with salience >= 9)
        if skill:
            from brain.systems.memory.embedding_service import EmbeddingService

            embedding_service = await EmbeddingService.from_session(uow.session)
            skill_emb = embedding_service.query(skill)
            result["warnings"].extend(
                await _maybe_await(uow.memories.high_salience_warnings_for_skill(skill_embedding=skill_emb))
            )

        # Skill-specific pitfalls
        if skill:
            from brain.platform.db.models.skill import Skill as SkillModel
            from sqlalchemy import select, or_
            stmt = select(SkillModel.pitfalls).where(
                SkillModel.name == skill,
                or_(SkillModel.archived == False, SkillModel.archived.is_(None)),  # noqa: E712
            )
            row_result = await _session_execute(uow.session, stmt)
            row = row_result.scalar()
            if row:
                pitfalls = row if isinstance(row, list) else json.loads(row)
                result["pitfalls"] = [
                    {"text": p["text"][:200], "severity": p.get("severity", "medium")}
                    for p in pitfalls[-5:]  # latest 5
                ]

    return result


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
    from brain.systems.cortex.events import publish_safe
    from brain.systems.vault import authorize_agent_secret_read, get_secret
    if not user_id:
        return {"error": "Vault access requires an authenticated user context"}
    target_user_id = str(user_id).strip()
    if not target_user_id:
        return {"error": "Vault access requires an authenticated user context"}
    authorization = await authorize_agent_secret_read(
        key,
        actor_user_id=target_user_id,
        org_id=org_id,
        run_id=run_id,
        reason=reason,
        requested_by=requested_by,
        project_slug=project_slug,
        project_slugs=project_slugs,
        target_registry_id=target_registry_id,
    )
    if not authorization.get("allowed"):
        grant = _json_safe(authorization.get("grant") or {})
        grant_user_id = str(grant.get("requested_by_user_id") or target_user_id).strip() or target_user_id
        if authorization.get("status") == "pending":
            normalized_idea_id = (str(idea_id).strip() if idea_id else "") or None
            prompt = None
            if normalized_idea_id:
                prompt = {
                    "id": f"vault-grant-{grant.get('id') or run_id or 'thread'}",
                    "idea_id": normalized_idea_id,
                    "org_id": org_id,
                    "target_user_id": grant_user_id,
                    "run_id": grant.get("run_id") or run_id,
                    "grant_id": grant.get("id"),
                    "key_name": grant.get("key_name") or key,
                    "requested_by": grant.get("requested_by") or requested_by,
                    "reason": grant.get("reason") or reason,
                    "requested_at": grant.get("requested_at"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                publish_safe("vault_agent_grant_prompt", {
                    "idea_id": normalized_idea_id,
                    "org_id": org_id,
                    "target_user_id": grant_user_id,
                    "run_id": grant.get("run_id") or run_id,
                    "grant": grant,
                    "prompt": prompt,
                })
            response = {
                "error": "Vault grant required before this agent can read the secret",
                "grant_id": grant.get("id"),
                "key_name": grant.get("key_name") or key,
                "reason": grant.get("reason") or reason,
                "requested_by": grant.get("requested_by") or requested_by,
                "run_id": grant.get("run_id") or run_id,
                "status": "pending",
                "target_user_id": grant_user_id,
            }
            if prompt:
                response["prompt"] = prompt
            return response
        return {"error": authorization.get("reason") or "Vault grant denied"}
    value = await get_secret(
        key,
        actor_user_id=target_user_id,
        org_id=org_id,
        accessed_by="agent",
    )
    if value is None:
        return {"error": f"Secret '{key}' not found in vault"}
    return {"key": key, "value": value}


_VAULT_PROMPT_CATEGORIES = {
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
}


def _clean_vault_prompt_text(value: str | None, *, max_chars: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _normalize_vault_key_name(key_name: str) -> str:
    cleaned = str(key_name or "").strip()
    if not cleaned:
        raise ValueError("key_name is required")
    return cleaned.upper()


def _safe_vault_secret_summary(secret: dict[str, Any]) -> dict[str, Any]:
    return {
        "key_name": str(secret.get("key_name") or ""),
        "description": str(secret.get("description") or ""),
        "category": str(secret.get("category") or "general"),
        "agent_access_level": str(secret.get("agent_access_level") or "ask"),
    }


def _vault_prompt_url(
    *,
    key_name: str,
    description: str,
    category: str,
) -> str:
    return "/vault?" + urlencode({
        "add_secret": key_name,
        "description": description,
        "category": category,
    })


async def tool_vault_inventory(
    category: str | None = None,
    access_level: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """List safe Vault metadata so the agent can choose an existing key."""
    from brain.systems.vault import async_list_secrets

    if not user_id:
        return {"error": "Vault inventory requires an authenticated user context"}
    normalized_user_id = str(user_id).strip()
    if not normalized_user_id:
        return {"error": "Vault inventory requires an authenticated user context"}
    normalized_org_id = (str(org_id).strip() if org_id else "") or None
    normalized_category = str(category or "").strip().lower() or None
    normalized_access_level = str(access_level or "").strip().lower() or None

    secrets = [
        _safe_vault_secret_summary(secret)
        for secret in await async_list_secrets(
            actor_user_id=normalized_user_id,
            org_id=normalized_org_id,
            category=normalized_category,
        )
    ]
    if normalized_access_level:
        secrets = [
            secret
            for secret in secrets
            if secret["agent_access_level"].strip().lower() == normalized_access_level
        ]
    secrets.sort(key=lambda secret: (secret["category"], secret["key_name"]))
    return {
        "secrets": secrets,
        "count": len(secrets),
        "metadata_only": True,
        "guidance": (
            "Use these names/descriptions/categories to decide which exact key to request with brain_vault. "
            "If no suitable key exists, ask the user or call vault_secret_prompt."
        ),
    }


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
    from brain.systems.cortex.events import publish_safe
    from brain.systems.vault import record_missing_request

    if not user_id:
        return {"error": "Vault secret prompts require an authenticated user context"}

    normalized_user_id = str(user_id).strip()
    if not normalized_user_id:
        return {"error": "Vault secret prompts require an authenticated user context"}
    normalized_org_id = (str(org_id).strip() if org_id else "") or None
    normalized_idea_id = (str(idea_id).strip() if idea_id else "") or None

    try:
        normalized_key = _normalize_vault_key_name(key_name)
    except ValueError as exc:
        return {"error": str(exc)}

    normalized_category = str(category or "api").strip().lower() or "api"
    if normalized_category not in _VAULT_PROMPT_CATEGORIES:
        normalized_category = "general"
    clean_description = _clean_vault_prompt_text(
        description or f"Credential requested by Illo for {normalized_key}.",
    )
    clean_reason = _clean_vault_prompt_text(reason or clean_description, max_chars=360)
    clean_requested_by = _clean_vault_prompt_text(requested_by or "agent", max_chars=80) or "agent"

    prompt = {
        "id": f"vault-secret-{run_id or 'thread'}-{uuid.uuid4().hex[:10]}",
        "idea_id": normalized_idea_id,
        "org_id": normalized_org_id,
        "run_id": run_id,
        "key_name": normalized_key,
        "description": clean_description,
        "category": normalized_category,
        "reason": clean_reason,
        "requested_by": clean_requested_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    await record_missing_request(normalized_key, actor_user_id=normalized_user_id, org_id=normalized_org_id)

    if normalized_idea_id:
        publish_safe("vault_secret_prompt", {
            "idea_id": normalized_idea_id,
            "org_id": normalized_org_id,
            "run_id": run_id,
            "prompt": prompt,
            "key_name": normalized_key,
            "description": clean_description,
            "category": normalized_category,
            "reason": clean_reason,
            "requested_by": clean_requested_by,
        })

    response = {
        "prompted": bool(normalized_idea_id),
        "status": "opened" if normalized_idea_id else "recorded",
        "key_name": normalized_key,
        "description": clean_description,
        "category": normalized_category,
        "prompt": prompt,
        "vault_url": _vault_prompt_url(
            key_name=normalized_key,
            description=clean_description,
            category=normalized_category,
        ),
    }
    if not normalized_idea_id:
        response["warning"] = (
            "No current Cortex thread was bound, so the missing key was recorded for Vault."
        )
    return response


async def tool_runtime_settings(
    provider: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """Inspect active runtime/provider/auth settings for the current user."""
    from brain.systems.services.runtime_introspection import async_get_runtime_settings_snapshot

    async with UnitOfWork() as uow:
        return await async_get_runtime_settings_snapshot(
            uow.session,
            user_id=user_id,
            org_id=org_id,
            provider=provider,
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
