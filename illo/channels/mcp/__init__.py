"""Stateless agent MCP transport. Client tokens never become brain tokens."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from illo.brain.server import _error, initialize_result, tool_result
from illo.channels.signals import submit as signal_submit
from illo.gateway import InboundEvent
from illo.ledger import audit, tokens
from illo.ledger import mcp as ledger
from illo.tools.registry import Tool

from . import asks, lanes, reads, schemas, submissions

PROFILES = frozenset({"agent_request"})
COLLECTOR_TOOLS = frozenset(
    {
        "lane_report",
        "lane_search",
        "ask_create",
        "ask_pull",
        "ask_ack",
        "ask_cancel",
        "signal_submit",
    }
)
AGENT_TOOLS = COLLECTOR_TOOLS | {"illo_submit", "illo_read", "illo_get_result"}


def allowed_tools(token: dict) -> frozenset[str]:
    if token["scopes"] == ["agent"]:
        return AGENT_TOOLS
    if token["scopes"] == ["collector"]:
        return COLLECTOR_TOOLS
    return frozenset()


def agent_tool(name, arguments, handler, risk, description):
    return Tool(
        name=name,
        family="agents",
        risk=risk,
        destination=lambda _: "ledger",
        personal=False,
        arguments=arguments,
        handler=handler,
        profiles=PROFILES,
        description=description,
    )


TOOLS = {
    t.name: t
    for t in (
        agent_tool(
            "illo_submit",
            schemas.Submit,
            submissions.submit,
            "write_internal",
            "Save knowledge or submit a request to Illo. Poll with illo_get_result.",
        ),
        agent_tool(
            "illo_get_result",
            schemas.GetResult,
            submissions.get_result,
            "read",
            "Read the status and evidence of your submission.",
        ),
        agent_tool(
            "illo_read",
            schemas.Read,
            reads.read,
            "read",
            "Read knowledge.search, knowledge.get, or workspace.search.",
        ),
        agent_tool(
            "lane_report",
            schemas.LaneReport,
            lanes.report,
            "write_internal",
            "Report one lane or a batch. Also report collector liveness.",
        ),
        agent_tool(
            "lane_search", schemas.LaneSearch, lanes.search, "read", "Search visible lanes."
        ),
        agent_tool(
            "ask_create",
            schemas.AskCreate,
            asks.create,
            "write_internal",
            "Ask a person one short question with two to five options.",
        ),
        agent_tool(
            "ask_pull",
            schemas.AskPull,
            asks.pull,
            "write_internal",
            "Pull unacked answers. Scope is token or person.",
        ),
        agent_tool("ask_ack", schemas.AskAck, asks.ack, "write_internal", "Acknowledge an answer."),
        agent_tool(
            "ask_cancel", schemas.AskCancel, asks.cancel, "write_internal", "Cancel an open ask."
        ),
        agent_tool(
            "signal_submit",
            schemas.Signal,
            signal_submit,
            "write_internal",
            "Submit a machine signal. Repeated source and dedupe_key return the first receipt.",
        ),
    )
}


@dataclass
class ClientContext:
    conn: Any
    token: dict
    app: Any
    bearer: str = field(repr=False)
    events: list[InboundEvent] = field(default_factory=list)

    @property
    def principal(self) -> str:
        return f"person:{self.token['person_key']}"


async def dispatch(ctx: ClientContext, body) -> tuple[Response, str, dict, str, str]:
    """Return an audit decision even for a malformed authenticated request."""
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return (
            _error(None, -32600, "Invalid JSON-RPC request"),
            "<invalid>",
            {},
            "deny",
            "Invalid request",
        )
    request_id, method, params = body.get("id"), body.get("method"), body.get("params", {})
    if not isinstance(method, str) or not isinstance(params, dict):
        return (
            _error(request_id, -32602, "Params must be an object"),
            "<invalid>",
            {},
            "deny",
            "Invalid params",
        )
    if method in {"notifications/initialized", "notifications/cancelled"} and "id" not in body:
        return Response(status_code=202), method, {}, "allow", "The notice is accepted"
    if "id" not in body or isinstance(request_id, (dict, list, bool)):
        return (
            _error(None, -32600, "A request id is required"),
            method,
            {},
            "deny",
            "Invalid request id",
        )
    name, args, decision, reason = method, params, "allow", "The request is allowed"
    if method == "initialize":
        result = initialize_result(params, "illo-agents")
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {
            "tools": [
                tool.declaration()
                for tool in TOOLS.values()
                if tool.name in allowed_tools(ctx.token)
            ]
        }
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(args, dict):
            return (
                _error(request_id, -32602, "Give a tool name and an argument object"),
                "<invalid>",
                {},
                "deny",
                "Invalid tool call",
            )
        tool = TOOLS.get(name)
        if not tool:
            return _error(request_id, -32602, "Unknown tool"), name, args, "deny", "Unknown tool"
        try:
            if name not in allowed_tools(ctx.token):
                raise ValueError("The token scope does not allow this tool")
            validated = tool.arguments.model_validate(args)
            # Reports are snapshots: omitted fields must not reset a stored lane.
            clean = validated.model_dump(exclude_unset=name == "lane_report")
            async with ctx.conn.transaction():
                value = await tool.handler(ctx, clean)
            result = tool_result(value)
        except ValidationError:
            decision, reason = "deny", "The tool arguments do not match its schema"
            ctx.events.clear()
            result = tool_result({"error": {"message": reason}}, is_error=True)
        except ValueError as exc:
            decision, reason = "deny", str(exc)
            ctx.events.clear()
            result = tool_result({"error": {"message": reason}}, is_error=True)
        except Exception:
            decision, reason = "deny", "The tool failed"
            ctx.events.clear()
            result = tool_result({"error": {"message": reason}}, is_error=True)
    else:
        return (
            _error(request_id, -32601, "Method not found"),
            method,
            params,
            "deny",
            "Unknown method",
        )
    response = JSONResponse(
        jsonable_encoder({"jsonrpc": "2.0", "id": request_id, "result": result})
    )
    return response, name, args, decision, reason


async def endpoint(request: Request):
    app = request.app.state.ctx
    header = request.headers.get("authorization", "")
    scheme, _, bearer = header.partition(" ")
    if header and (scheme.lower() != "bearer" or not bearer):
        bearer = ""
    elif not header:
        bearer = request.headers.get("x-illo-bridge-token", "")
    async with app.pool.connection() as conn:
        token = await tokens.verify(conn, bearer) if bearer else None
        if not token:
            raise HTTPException(
                401,
                "The client token is missing, unknown, or revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
        ctx = ClientContext(conn, token, app, bearer)
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            body = None
            response, name, args, decision, reason = (
                _error(None, -32700, "Invalid JSON"),
                "<invalid>",
                {},
                "deny",
                "Invalid JSON",
            )
        else:
            response, name, args, decision, reason = await dispatch(ctx, body)
        await audit.write(
            conn,
            token_id=token["id"],
            principal=ctx.principal,
            tool=name,
            args=args,
            decision=decision,
            reason=reason,
        )
    # The pool commits before any gateway task can observe the receipt.
    for event in ctx.events:
        task = asyncio.create_task(deliver_event(app, event))
        # The app cancels and joins these tasks before it closes the database pool.
        app.scheduler.tasks.append(task)
        task.add_done_callback(app.scheduler.tasks.remove)
    return response


async def deliver_event(app, event):
    """A token can be revoked after intake but before the gateway verifies it."""
    result = await app.gateway.receive(event)
    if event.channel == "mcp" and result["decision"] == "ignore":
        await app.gateway.adapters["mcp"].deliver_error(event)


async def setup(ctx) -> None:
    async with ctx.pool.connection() as conn:
        await ledger.recover_submissions(conn)
    ctx.gateway.register("mcp", submissions.Adapter(ctx))
    ctx.app.add_api_route("/mcp", endpoint, methods=["POST"])
    ctx.app.add_api_route("/api/mcp", endpoint, methods=["POST"])
