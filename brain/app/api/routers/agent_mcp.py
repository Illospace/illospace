"""Hosted MCP endpoint for personal agents connecting to Illo."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from brain.app.api.deps import get_db, rate_limit
from brain.app.api.routers.agent_bridge import (
    _bearer_token,
    _broadcast_thread_result,
    _commit_for_live_fanout,
    _run_trigger_if_requested,
    _thread_payload,
)
from brain.app.api.routers.external_agent_errors import raise_external_agent_http_error
from brain.app.mentions import classify_mention_intent
from brain.systems.external_agents import service as external_agents


router = APIRouter(tags=["agent-mcp"], dependencies=[Depends(rate_limit)])


ToolHandler = Callable[
    [AsyncSession, external_agents.AgentBridgePrincipal, dict[str, Any]],
    Awaitable[dict[str, Any]],
]


def _tool_schema(description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


MCP_TOOLS: dict[str, dict[str, Any]] = {
    "illo_search_workspace": {
        **_tool_schema(
            (
                "Search the Illo workspace for related ideas, threads, and shared work. "
                "Use this before creating a new thread when the user asks to share work, "
                "continue prior work, or avoid duplicating an existing Illo discussion."
            ),
            {
                "query": {"type": "string", "description": "Search terms for Illo workspace context."},
                "limit": {"type": "integer", "description": "Maximum results to return, 1-25.", "default": 10},
            },
            ["query"],
        ),
        "scope": external_agents.SCOPE_WORKSPACE_READ,
    },
    "illo_get_thread": {
        **_tool_schema(
            (
                "Read messages from an existing Illo idea/thread. Use this before posting "
                "an update so replies preserve team context and avoid repeating prior work."
            ),
            {
                "idea_id": {"type": "string", "description": "Illo idea/thread id."},
                "limit": {"type": "integer", "description": "Maximum messages to return.", "default": 100},
            },
            ["idea_id"],
        ),
        "scope": external_agents.SCOPE_WORKSPACE_READ,
    },
    "illo_get_team_members": {
        **_tool_schema(
            (
                "List visible Illo team members. Use before sharing work with named "
                "teammates so thread tools can notify the right user ids."
            ),
            {},
        ),
        "scope": external_agents.SCOPE_WORKSPACE_READ,
    },
    "illo_create_thread": {
        **_tool_schema(
            (
                "Create a visible Illo thread from personal-agent work. Use when the user "
                "asks to share work with teammates, publish findings into Illo, or start "
                "a team-visible discussion. Set trigger_illo only when the user wants Illo "
                "to actively respond or the message explicitly mentions Illo."
            ),
            {
                "title": {"type": "string", "description": "Thread title visible in Illo."},
                "body": {"type": "string", "description": "Thread body/message visible to the team."},
                "teammate_user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional Illo user ids to notify.",
                    "default": [],
                },
                "artifacts": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional structured artifacts, links, or files to attach.",
                    "default": [],
                },
                "trigger_illo": {
                    "type": "boolean",
                    "description": "Whether Illo should actively respond to this new thread.",
                    "default": False,
                },
                "metadata": {"type": "object", "description": "Optional machine-readable metadata.", "default": {}},
            },
            ["title", "body"],
        ),
        "scope": external_agents.SCOPE_ILLO_THREAD_CREATE,
        "mutates_thread": True,
    },
    "illo_post_thread_message": {
        **_tool_schema(
            (
                "Post a visible update into an existing Illo thread. Use for follow-ups, "
                "status updates, final answers, or sharing new artifacts after reading "
                "the thread with illo_get_thread."
            ),
            {
                "idea_id": {"type": "string", "description": "Existing Illo idea/thread id."},
                "body": {"type": "string", "description": "Message body to post into the thread."},
                "teammate_user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional Illo user ids to notify.",
                    "default": [],
                },
                "artifacts": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional structured artifacts, links, or files to attach.",
                    "default": [],
                },
                "trigger_illo": {
                    "type": "boolean",
                    "description": "Whether Illo should actively respond to this message.",
                    "default": False,
                },
                "metadata": {"type": "object", "description": "Optional machine-readable metadata.", "default": {}},
            },
            ["idea_id", "body"],
        ),
        "scope": external_agents.SCOPE_ILLO_THREAD_WRITE,
        "mutates_thread": True,
    },
    "illo_ask": {
        **_tool_schema(
            (
                "Ask Illo for private workspace context without creating a visible thread. "
                "Use when the personal agent needs Illo's workspace knowledge, team memory, "
                "or project context before doing work. This is read/context mode, not "
                "team-visible coordination; create or post to a visible thread with "
                "trigger_illo=true when Illo should coordinate or hand off work. Poll "
                "with illo_get_ask."
            ),
            {
                "question": {"type": "string", "description": "Question for Illo's headless context agent."},
                "context": {
                    "type": "object",
                    "description": "Optional context about the current personal-agent task.",
                    "default": {},
                },
                "metadata": {"type": "object", "description": "Optional machine-readable metadata.", "default": {}},
            },
            ["question"],
        ),
        "scope": external_agents.SCOPE_ILLO_ASK,
    },
    "illo_get_ask": {
        **_tool_schema(
            (
                "Poll a headless Illo ask created by illo_ask. Use this to retrieve "
                "status, events, and final answer artifacts without creating team-visible noise."
            ),
            {"ask_id": {"type": "string", "description": "Task id returned by illo_ask."}},
            ["ask_id"],
        ),
        "scope": external_agents.SCOPE_ILLO_ASK,
    },
}


def _has_scope(principal: external_agents.AgentBridgePrincipal, scope: str | None) -> bool:
    return not scope or "*" in principal.scopes or scope in principal.scopes


def _clean_string_list(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]
    return [str(value).strip() for value in values if str(value or "").strip()]


def _clean_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _clean_artifacts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _tool_metadata(arguments: dict[str, Any], *, tool_name: str, trigger_illo: bool | None = None) -> dict[str, Any]:
    metadata = {**_clean_dict(arguments.get("metadata")), "mcp_tool": tool_name}
    if trigger_illo is not None:
        metadata["trigger_illo"] = trigger_illo
    return metadata


async def require_mcp_principal(
    request: Request,
    x_illo_bridge_token: str | None = Header(default=None, alias="X-Illo-Bridge-Token"),
    db: AsyncSession = Depends(get_db),
) -> external_agents.AgentBridgePrincipal:
    token = _bearer_token(request, x_illo_bridge_token)
    try:
        return await external_agents.authenticate_bridge_token(db, token)
    except Exception as exc:
        raise_external_agent_http_error(exc)


def _list_tools(principal: external_agents.AgentBridgePrincipal) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for name, spec in MCP_TOOLS.items():
        if not _has_scope(principal, spec.get("scope")):
            continue
        tools.append(
            {
                "name": name,
                "description": spec["description"],
                "inputSchema": spec["inputSchema"],
            }
        )
    return tools


async def _tool_search_workspace(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return await external_agents.search_workspace(
        db,
        principal,
        query=str(arguments.get("query") or ""),
        limit=int(arguments.get("limit") or 10),
    )


async def _tool_get_thread(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return await external_agents.get_thread(
        db,
        principal,
        idea_id=str(arguments.get("idea_id") or ""),
        limit=int(arguments.get("limit") or 100),
    )


async def _tool_get_team_members(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    _arguments: dict[str, Any],
) -> dict[str, Any]:
    return await external_agents.get_team_members(db, principal)


async def _tool_create_thread(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    body = str(arguments.get("body") or "")
    should_trigger = bool(arguments.get("trigger_illo") or classify_mention_intent(body).should_invoke_illo)
    metadata = _tool_metadata(arguments, tool_name="illo_create_thread", trigger_illo=should_trigger)
    idea, message, notified = await external_agents.create_thread_from_agent(
        db,
        principal,
        title=str(arguments.get("title") or ""),
        body=body,
        teammate_user_ids=_clean_string_list(arguments.get("teammate_user_ids")),
        artifacts=_clean_artifacts(arguments.get("artifacts")),
        trigger_illo=should_trigger,
        metadata=metadata,
    )
    result = _thread_payload(idea, message, notified)
    result["_trigger_idea"] = idea
    return result


async def _tool_post_thread_message(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    body = str(arguments.get("body") or "")
    should_trigger = bool(arguments.get("trigger_illo") or classify_mention_intent(body).should_invoke_illo)
    metadata = _tool_metadata(arguments, tool_name="illo_post_thread_message", trigger_illo=should_trigger)
    idea, message, notified = await external_agents.post_thread_message_from_agent(
        db,
        principal,
        idea_id=str(arguments.get("idea_id") or ""),
        body=body,
        teammate_user_ids=_clean_string_list(arguments.get("teammate_user_ids")),
        artifacts=_clean_artifacts(arguments.get("artifacts")),
        trigger_illo=should_trigger,
        metadata=metadata,
    )
    result = _thread_payload(idea, message, notified)
    result["_trigger_idea"] = idea
    return result


async def _tool_ask(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    task = await external_agents.create_headless_ask(
        db,
        principal,
        question=str(arguments.get("question") or ""),
        context=_clean_dict(arguments.get("context")),
        metadata=_tool_metadata(arguments, tool_name="illo_ask"),
    )
    return await external_agents.serialize_task(task, include_events=True, session=db)


async def _tool_get_ask(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return await external_agents.get_headless_ask(
        db,
        principal,
        ask_id=str(arguments.get("ask_id") or ""),
    )


async def _add_thread_trigger_result_if_needed(
    db: AsyncSession,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    tool_payload: dict[str, Any],
    principal: external_agents.AgentBridgePrincipal,
) -> None:
    if tool_name not in {"illo_create_thread", "illo_post_thread_message"}:
        return
    trigger_idea = tool_payload.pop("_trigger_idea", None)
    if trigger_idea is None:
        return
    body = str(arguments.get("body") or "")
    should_trigger = bool(arguments.get("trigger_illo") or classify_mention_intent(body).should_invoke_illo)
    metadata = _tool_metadata(arguments, tool_name=tool_name, trigger_illo=should_trigger)
    tool_payload["trigger"] = await _run_trigger_if_requested(
        db,
        idea=trigger_idea,
        body=body,
        metadata=metadata,
        principal=principal,
    )


TOOL_HANDLERS: dict[str, ToolHandler] = {
    "illo_search_workspace": _tool_search_workspace,
    "illo_get_thread": _tool_get_thread,
    "illo_get_team_members": _tool_get_team_members,
    "illo_create_thread": _tool_create_thread,
    "illo_post_thread_message": _tool_post_thread_message,
    "illo_ask": _tool_ask,
    "illo_get_ask": _tool_get_ask,
}


def _result(req_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": payload}


def _tool_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, ensure_ascii=False, default=str),
            }
        ]
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


async def _handle_mcp_request(
    message: dict[str, Any],
    *,
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
) -> dict[str, Any] | None:
    method = str(message.get("method") or "")
    req_id = message.get("id")

    if method == "initialize":
        requested_version = str((message.get("params") or {}).get("protocolVersion") or "2025-06-18")
        return _result(
            req_id,
            {
                "protocolVersion": requested_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "illo", "version": "0.1.0"},
            },
        )

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return _result(req_id, {"tools": _list_tools(principal)})

    if method != "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    params = message.get("params") or {}
    tool_name = str(params.get("name") or "")
    arguments = params.get("arguments") or {}
    if tool_name not in MCP_TOOLS or tool_name not in TOOL_HANDLERS:
        return _result(req_id, _tool_error(f"Unknown tool: {tool_name}"))
    spec = MCP_TOOLS[tool_name]
    scope = spec.get("scope")
    if not _has_scope(principal, scope):
        return _result(req_id, _tool_error(f"Bridge token is missing scope: {scope}"))
    if not isinstance(arguments, dict):
        return _result(req_id, _tool_error("Tool arguments must be an object"))

    try:
        try:
            tool_payload = await TOOL_HANDLERS[tool_name](db, principal, arguments)
        except Exception as exc:
            raise_external_agent_http_error(exc)
        if spec.get("mutates_thread"):
            await _add_thread_trigger_result_if_needed(
                db,
                tool_name=tool_name,
                arguments=arguments,
                tool_payload=tool_payload,
                principal=principal,
            )
            await _commit_for_live_fanout(db)
            await _broadcast_thread_result(tool_payload, org_id=principal.org_id)
        return _result(req_id, _tool_result(tool_payload))
    except Exception as exc:
        await db.rollback()
        return _result(req_id, _tool_error(str(exc)))


async def _mcp_endpoint(
    request: Request,
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
):
    payload = await request.json()
    if isinstance(payload, list):
        responses: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            response = await _handle_mcp_request(item, db=db, principal=principal)
            if response is not None:
                responses.append(response)
        return JSONResponse(responses)
    if not isinstance(payload, dict):
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
            status_code=400,
        )
    response = await _handle_mcp_request(payload, db=db, principal=principal)
    if response is None:
        return Response(status_code=202)
    return JSONResponse(response)


@router.post("/mcp")
async def hosted_mcp(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(require_mcp_principal),
):
    return await _mcp_endpoint(request, db, principal)


@router.post("/api/mcp")
async def hosted_api_mcp(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: external_agents.AgentBridgePrincipal = Depends(require_mcp_principal),
):
    return await _mcp_endpoint(request, db, principal)
