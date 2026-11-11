"""Stateless JSON-RPC MCP subset with per-request run authentication."""

import json

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from illo.ledger import audit, runs
from illo.tools.registry import ToolContext

PROTOCOL_VERSION = "2025-06-18"


def _error(request_id, code: int, message: str):
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def initialize_result(params: dict, name: str) -> dict:
    """Both MCP endpoints use the same stateless protocol negotiation."""
    version = params.get("protocolVersion", PROTOCOL_VERSION)
    if not isinstance(version, str) or version not in {
        "2024-11-05",
        "2025-03-26",
        "2025-06-18",
        "2025-11-25",
    }:
        version = PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": name, "version": "0.1.0"},
    }


def tool_result(value: dict, *, is_error: bool = False, brain: bool = False) -> dict:
    """Keep agent contracts. Brain results have only one data channel."""
    value = jsonable_encoder(value)
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, separators=(",", ":"))}],
        "structuredContent": {k: value[k] for k in ("decision", "reason")} if brain else value,
        "isError": is_error,
    }


async def endpoint(request: Request):
    """A bearer token determines both the tool list and the handler context."""
    ctx = request.app.state.ctx
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            401, "A run bearer token is required", headers={"WWW-Authenticate": "Bearer"}
        )
    async with ctx.pool.connection() as conn:
        async with conn.transaction():
            run = await runs.verify_token(conn, token, lock=True)
            if not run or not await runs.verify_token(conn, token):
                raise HTTPException(
                    401,
                    "The run token is unknown or expired",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            try:
                body = await request.json()
            except (ValueError, UnicodeError):
                return _error(None, -32700, "Invalid JSON")
            if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
                return _error(None, -32600, "Invalid JSON-RPC request")
            method, request_id, params = body.get("method"), body.get("id"), body.get("params", {})
            if not isinstance(params, dict):
                return _error(request_id, -32602, "Params must be an object")
            if method == "notifications/initialized":
                return Response(status_code=202)
            if method == "initialize":
                result = initialize_result(params, "illo-brain-tools")
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": await ctx.registry.list_tools_for(ToolContext(conn, run, ctx))}
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments", {})
                if not isinstance(name, str) or not isinstance(args, dict):
                    await audit.write(
                        conn,
                        run_id=run["id"],
                        principal=run["principal"],
                        tool=name if isinstance(name, str) else "<invalid>",
                        args=params,
                        decision="deny",
                        reason="The tool request is invalid",
                    )
                    return _error(
                        request_id, -32602, "A tool name and argument object are required"
                    )
                count = run["usage"].get("tool_calls", 0)
                if count >= ctx.workspace.config.brain.max_turns:
                    # Count and audit denied calls without running a handler.
                    await audit.write(
                        conn,
                        run_id=run["id"],
                        principal=run["principal"],
                        tool=name,
                        args=args,
                        decision="deny",
                        reason="The run reached its turn limit",
                    )
                    called = {
                        "decision": "deny",
                        "reason": "The run reached its turn limit",
                        "result": None,
                    }
                else:
                    await conn.execute(
                        "UPDATE runs SET usage=jsonb_set(usage,'{tool_calls}',to_jsonb(%s::int)) WHERE id=%s",
                        (count + 1, run["id"]),
                    )
                    called = await ctx.registry.call(ToolContext(conn, run, ctx), name, args)
                result = tool_result(called, is_error=called["decision"] == "deny", brain=True)
            else:
                return _error(request_id, -32601, "Method not found")
            return JSONResponse(
                jsonable_encoder({"jsonrpc": "2.0", "id": request_id, "result": result})
            )
